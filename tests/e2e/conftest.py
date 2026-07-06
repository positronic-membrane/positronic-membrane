import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import src.config
import src.memory
from src.auth import create_access_token
from src.database import get_connection
from src.web_server import app

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "E2E Test",
    "GIT_AUTHOR_EMAIL": "e2e@test.local",
    "GIT_COMMITTER_NAME": "E2E Test",
    "GIT_COMMITTER_EMAIL": "e2e@test.local",
}


@pytest.fixture(autouse=True)
def e2e_vector_db(tmp_path):
    """Isolate ChromaDB for every e2e test. Most flows here (chat hydration,
    daemon reflection/consolidation) touch the vector store; this is autouse
    rather than opt-in per file, unlike the unit suite's convention, so a
    future e2e test can't forget the isolation and silently reach the real
    data/chromadb/ directory."""
    orig_path = src.config.VECTOR_DB_PATH
    src.config.VECTOR_DB_PATH = str(tmp_path / "test_chromadb")
    src.memory._chroma_client = None
    src.memory._collections = {}

    yield

    src.config.VECTOR_DB_PATH = orig_path
    src.memory._chroma_client = None
    src.memory._collections = {}


@pytest.fixture(autouse=True)
def mock_embeddings():
    """Every persona-prompt build and every reflection/consolidation cycle calls
    into add_memory/query_memories, which hit a real OpenAI-compatible embedding
    endpoint with no mock-mode switch of their own — patch it here so no e2e test
    depends on network reachability."""
    with patch("src.memory.get_embeddings") as mock_get:
        mock_get.side_effect = lambda texts: [[0.1] * 384 for _ in texts]
        yield mock_get


@pytest.fixture
def e2e_client():
    return TestClient(app)


@pytest.fixture
def seed_party():
    """Factory fixture: seed_party(role="user") -> (party_id, jwt_token).

    Inserts directly into `parties` and mints a JWT via create_access_token,
    bypassing the /api/v1/auth/token HTTP round trip — reserved for
    test_auth_flow.py, which exercises that endpoint for real.
    """

    def _factory(role: str = "user", name: str = None):
        party_id = str(uuid.uuid4())
        name = name or f"e2e-{role}-{party_id[:8]}"
        now = datetime.now(UTC).isoformat()
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO parties (id, name, role, created_at, last_seen, public_key) VALUES (?, ?, ?, ?, ?, ?)",
                (party_id, name, role, now, now, f"key-{party_id}"),
            )
            conn.commit()
        finally:
            conn.close()
        token = create_access_token(party_id, role)
        return party_id, token

    return _factory


@pytest.fixture
def isolated_git_workspace(tmp_path, monkeypatch):
    """Real, throwaway git repo for test_sandbox_e2e.py.

    create_sandbox_session/ship_sandbox_session run real `git worktree`/`branch`
    subprocesses against src.config.ROOT_DIR. This redirects ROOT_DIR to a tiny
    synthetic repo (one trivial test) before any sandbox call, so the sandboxed
    LocalSandboxExecutor's `pytest -v` run stays fast and never touches the real
    /opt/janus checkout or its 324+ test suite.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {**os.environ, **_GIT_ENV}

    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, env=env)
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("def hello():\n    return 'hello'\n")
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_trivial.py").write_text(
        "def test_trivial():\n    assert True\n"
    )
    (workspace / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=workspace,
        check=True,
        capture_output=True,
        env=env,
    )

    # LocalSandboxExecutor resolves pytest via `ROOT_DIR / ".venv" / "bin" / "pytest"`
    # first, falling back to a bare "pytest" resolved via PATH only if that's absent.
    # The bare fallback isn't reliable here (this suite may run via an unactivated
    # venv), so provide the same .venv/bin/pytest layout the app expects in production,
    # symlinked to the real pytest actually running this test session — checking both
    # the running interpreter's own bin dir and PATH, since either may hold it depending
    # on how this suite was invoked.
    real_pytest = Path(sys.executable).parent / "pytest"
    if not real_pytest.exists():
        which_pytest = shutil.which("pytest")
        real_pytest = Path(which_pytest) if which_pytest else None
    if real_pytest and real_pytest.exists():
        venv_bin = workspace / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "pytest").symlink_to(real_pytest)

    monkeypatch.setattr(src.config, "ROOT_DIR", workspace)
    monkeypatch.setattr(src.config, "SANDBOX_PROVIDER", "local")
    monkeypatch.setattr(src.config, "ALLOW_LOCAL_SANDBOX_EXEC", True)
    monkeypatch.setenv("SANDBOX_PROVIDER", "local")

    assert src.config.ROOT_DIR == workspace and src.config.ROOT_DIR != Path("/opt/janus"), (
        "isolated_git_workspace fixture failed to redirect ROOT_DIR — refusing to proceed "
        "to avoid running real git/subprocess operations against the actual repo checkout."
    )

    yield workspace


def _llm_side_effect(agent_id, prompt, system_override=None, **kwargs):
    """Shared deterministic script for both src.skills.query_agent and
    src.memory.query_agent call sites, keyed by agent_id + prompt content —
    same convention as tests/test_daemon.py."""
    if agent_id == "proposer":
        if "candidate goals" in prompt:
            return (
                '[{"type": "short", '
                '"description": "Investigate SQLite WAL lock contention under concurrent daemon writes.", '
                '"confidence": 0.7, '
                '"source_reason": "Recurring curiosity topic from reflection cycle."}]'
            )
        return "PROPOSED_ACTION: Scan codebase docs"
    if agent_id == "critic":
        return "Decision: 1\nJustification: Action is safe and complies with all constitutional rules."
    if agent_id == "archivist":
        if "topic strings" in prompt:
            return '["sqlite wal locks", "daemon concurrency"]'
        if "Primary Concept" in prompt:
            return "Janus reflected on recent activity and archived a concise summary nugget."
        return "Janus execution summary nugget logged."
    return ""


@pytest.fixture
def daemon_llm_script(monkeypatch):
    """Patches query_agent at both real call sites (src/skills.py and
    src/memory.py each bind the name at import time) with one deterministic
    script, so test_daemon_cycle.py never depends on a live LLM endpoint."""
    monkeypatch.setattr("src.skills.query_agent", _llm_side_effect)
    monkeypatch.setattr("src.memory.query_agent", _llm_side_effect)
