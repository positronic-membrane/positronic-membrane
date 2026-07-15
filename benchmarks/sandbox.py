"""Bounded, DB-isolated 'autonomous-week' sandbox run for the behavioral
evaluation harness (issue #112).

Combines two existing precedents rather than introducing a new mechanism:
- The DB-copy idiom from src/sandbox_session.py::_create_evolution_sandbox()
  (shutil.copy2 the live DB to a scratch path) -- but not that file's
  subprocess/env-var wiring, since this runs the daemon loop in-process.
- The bounded-run idiom repeated ~10x in tests/test_daemon.py:
  asyncio.create_task(run_heartbeat_loop()) -> asyncio.sleep(duration) ->
  task.cancel() -> catch asyncio.CancelledError. run_heartbeat_loop() takes
  no arguments and has no built-in stop condition.

src/self_modification.py's stage_and_test*/apply_staged* are NOT used as
precedent here -- they are dead stubs (raise PermissionError), consistent
with self-modification being frozen for the V1 sign-off this issue is part of.

DB_PATH isolation alone is not sufficient: memory consolidation during the
run reaches src.memory.get_chroma_client(), a process-wide singleton keyed on
src.config.VECTOR_DB_PATH. Both must be isolated and restored together, or a
sandbox run silently writes real embeddings into the production ChromaDB.
"""
import asyncio
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import src.config
import src.memory
from benchmarks.metrics_window import get_windowed_escalations
from src.daemon import run_heartbeat_loop
from src.database import get_connection
from src.metrics import (
    get_windowed_checkpoints_completed,
    get_windowed_cost_total,
    get_windowed_stagnation_pause_count,
)

logger = logging.getLogger("JanusBenchmarkSandbox")

DEFAULT_SANDBOX_DURATION_SECONDS = 8.0


async def run_autonomous_week_sandbox(duration_seconds: float = None) -> dict:
    """Runs run_heartbeat_loop() for a bounded, accelerated duration against a
    disposable copy of the live DB and an isolated ChromaDB directory, then
    returns windowed metrics read from that copy. Never mutates the live DB's
    rows or the live vector store (only reads the DB once to seed the copy);
    all isolated state (src.config.DB_PATH, src.config.VECTOR_DB_PATH,
    src.memory's _chroma_client/_collections singletons, JANUS_TEST_MODE) is
    restored in a finally block so a mid-run exception can't leave the
    process pointed at scratch state.

    duration_seconds defaults to None (resolved to DEFAULT_SANDBOX_DURATION_SECONDS
    inside the function body, not baked into the signature) so tests can
    monkeypatch the module-level constant to shorten the bounded run, mirroring
    tests/test_daemon.py's monkeypatch-a-config-constant convention."""
    if duration_seconds is None:
        duration_seconds = DEFAULT_SANDBOX_DURATION_SECONDS

    original_db_path = src.config.DB_PATH
    original_vector_db_path = src.config.VECTOR_DB_PATH
    original_chroma_client = src.memory._chroma_client
    original_collections = src.memory._collections
    original_test_mode = os.environ.get("JANUS_TEST_MODE")

    scratch_dir = Path(tempfile.mkdtemp(prefix="janus_benchmark_sandbox_"))
    scratch_db_path = scratch_dir / "janus_benchmark.db"
    scratch_vector_db_path = scratch_dir / "chromadb"

    live_db_path = Path(original_db_path)
    if live_db_path.exists() and live_db_path.is_file():
        shutil.copy2(live_db_path, scratch_db_path)
    # else: no live DB yet (e.g. a from-scratch test run) -- daemon will
    # create/init on first connection via the normal init_db() path.

    window_start = datetime.now(timezone.utc)

    try:
        src.config.DB_PATH = str(scratch_db_path)
        src.config.VECTOR_DB_PATH = str(scratch_vector_db_path)
        src.memory._chroma_client = None
        src.memory._collections = {}
        os.environ["JANUS_TEST_MODE"] = "1"

        task = asyncio.create_task(run_heartbeat_loop())
        await asyncio.sleep(duration_seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        window_end = datetime.now(timezone.utc)

        # One shared connection for all four windowed reads (against the
        # scratch DB, still active at this point) rather than one per call.
        conn = get_connection(read_only_constitution=True)
        try:
            cost_total = get_windowed_cost_total(window_start, window_end, conn=conn)
            checkpoints = get_windowed_checkpoints_completed(window_start, window_end, conn=conn)
            stagnation = get_windowed_stagnation_pause_count(window_start, window_end, conn=conn)
            escalations = get_windowed_escalations(window_start, window_end, conn=conn)
        finally:
            conn.close()

        checkpoints_completed = checkpoints["total"]
        cost_per_checkpoint = (cost_total / checkpoints_completed) if checkpoints_completed else None

        return {
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "checkpoints_completed": checkpoints_completed,
            "checkpoints_completed_autonomously": checkpoints["autonomous"],
            "cost_total": cost_total,
            "cost_per_completed_checkpoint": cost_per_checkpoint,
            "stagnation_pauses": stagnation["stagnation"],
            "hard_cap_pauses": stagnation["hard_cap"],
            "escalations": escalations,
        }
    finally:
        src.config.DB_PATH = original_db_path
        src.config.VECTOR_DB_PATH = original_vector_db_path
        src.memory._chroma_client = original_chroma_client
        src.memory._collections = original_collections
        if original_test_mode is None:
            os.environ.pop("JANUS_TEST_MODE", None)
        else:
            os.environ["JANUS_TEST_MODE"] = original_test_mode
        shutil.rmtree(scratch_dir, ignore_errors=True)
