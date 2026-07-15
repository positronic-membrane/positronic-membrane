"""Integration coverage for `python -m benchmarks.run` (issue #112).

Marked e2e since the sandbox-path tests spin a real run_heartbeat_loop()
task (mirroring tests/test_daemon.py's bounded-run pattern). All tests run
under LLM_MOCK_MODE against the autouse tmp-path test DB from
tests/conftest.py -- this suite never touches /opt/janus/janus.db, per this
session's "build+test only, no live run" scope.
"""
import asyncio
import json
from unittest.mock import patch

import pytest

import src.config
import src.memory
from benchmarks import run as benchmarks_run
from benchmarks import sandbox as benchmarks_sandbox

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch):
    monkeypatch.setattr(src.config, "LLM_MOCK_MODE", True)


@pytest.fixture(autouse=True)
def _isolated_vector_db(tmp_path, monkeypatch):
    """generate_persona_response_autonomous() touches the vector store
    (query_memories/add_memory); isolate it per tests/test_memory.py's
    convention since the global conftest fixture doesn't cover ChromaDB."""
    monkeypatch.setattr(src.config, "VECTOR_DB_PATH", str(tmp_path / "test_chromadb"))
    monkeypatch.setattr(src.memory, "_chroma_client", None)
    monkeypatch.setattr(src.memory, "_collections", {})


@pytest.fixture(autouse=True)
def _mock_embeddings():
    """Every persona-prompt build and every reflection/consolidation cycle
    calls into add_memory/query_memories, which hit a real OpenAI-compatible
    embedding endpoint with no LLM_MOCK_MODE switch of their own (confirmed:
    src/memory.py::get_embeddings has no mock-mode branch) -- patch it here,
    mirroring tests/e2e/conftest.py's identical fixture, so this suite never
    depends on network reachability."""
    with patch("src.memory.get_embeddings") as mock_get:
        mock_get.side_effect = lambda texts: [[0.1] * 384 for _ in texts]
        yield mock_get


def test_run_layer2_skip_sandbox_covers_all_conversation_categories():
    layer2 = benchmarks_run.run_layer2("test", skip_sandbox=True)

    for category in ("voice_integrity", "memory_recall", "refusal_escalation", "slash_commands"):
        assert category in layer2
        assert layer2[category]["mean_score"] is not None

    assert layer2["autonomous_week"]["skipped"] is True


def test_cli_main_writes_output_json(tmp_path):
    out_path = tmp_path / "result.json"
    exit_code = benchmarks_run.main(["--target", "test", "--layer", "2", "--skip-sandbox", "--out", str(out_path)])

    assert exit_code == 0
    result = json.loads(out_path.read_text())
    assert result["target"] == "test"
    assert result["layer1"] == {"e2e_suite": "skipped", "conformance_suite": "skipped"}
    assert "voice_integrity" in result["layer2"]
    assert result["layer2"]["autonomous_week"]["skipped"] is True


def test_sandbox_run_restores_db_path_on_success(monkeypatch):
    monkeypatch.setattr(benchmarks_sandbox, "DEFAULT_SANDBOX_DURATION_SECONDS", 0.5)
    original_db_path = src.config.DB_PATH
    original_vector_db_path = src.config.VECTOR_DB_PATH

    result = asyncio.run(benchmarks_sandbox.run_autonomous_week_sandbox())

    assert src.config.DB_PATH == original_db_path
    # The vector store must be restored too -- not just DB_PATH -- since
    # memory consolidation during the run reaches the process-wide
    # _chroma_client singleton keyed on VECTOR_DB_PATH.
    assert src.config.VECTOR_DB_PATH == original_vector_db_path
    for key in (
        "checkpoints_completed", "checkpoints_completed_autonomously",
        "cost_per_completed_checkpoint", "stagnation_pauses", "hard_cap_pauses", "escalations",
    ):
        assert key in result


def test_sandbox_run_restores_db_path_on_exception(monkeypatch):
    monkeypatch.setattr(benchmarks_sandbox, "DEFAULT_SANDBOX_DURATION_SECONDS", 0.2)
    original_db_path = src.config.DB_PATH
    original_vector_db_path = src.config.VECTOR_DB_PATH

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure after the daemon run")

    monkeypatch.setattr(benchmarks_sandbox, "get_windowed_cost_total", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        asyncio.run(benchmarks_sandbox.run_autonomous_week_sandbox())

    assert src.config.DB_PATH == original_db_path
    assert src.config.VECTOR_DB_PATH == original_vector_db_path


def test_run_layer2_including_sandbox_produces_autonomous_week_metrics(monkeypatch):
    monkeypatch.setattr(benchmarks_sandbox, "DEFAULT_SANDBOX_DURATION_SECONDS", 0.5)

    layer2 = benchmarks_run.run_layer2("test", skip_sandbox=False)

    week = layer2["autonomous_week"]
    assert "skipped" not in week
    assert week["checkpoints_completed"] is not None
    assert week["escalation_count"] == 0
    assert week["escalation_quality_mean"] is None
