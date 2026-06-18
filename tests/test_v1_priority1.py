import pytest
import sqlite3
import uuid
import json
import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from src.database import init_db, get_connection, log_episodic_memory, get_recent_episodic_memories
from src.skills import DynamicSkillExecutor
from src.sandbox_session import (
    ship_sandbox_session,
    abort_sandbox_session,
    get_active_sandbox
)

# Shared state to communicate mock connection builder across fixtures
_shared_state = {}

@pytest.fixture
def db_conn():
    """Create a real in-memory SQLite database with init_db() applied."""
    import src.database as db_module
    import src.web_server as ws_module
    import src.sandbox_session as sb_module
    import src.persona as persona_module
    import src.memory_orchestrator as mo_module
    import src.role_bootstrap as rb_module
    import src.skills as skills_module

    original_get_connection = db_module.get_connection
    db_name = f"memdb_{uuid.uuid4().hex}"
    uri = f"file:{db_name}?mode=memory&cache=shared"

    main_conn = sqlite3.connect(uri, uri=True)
    main_conn.row_factory = sqlite3.Row
    main_conn.execute("PRAGMA foreign_keys = ON;")

    def mock_get_connection(*args, **kwargs):
        c = sqlite3.connect(uri, uri=True)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON;")
        return c

    db_module.get_connection = mock_get_connection
    ws_module.get_connection = mock_get_connection
    sb_module.get_connection = mock_get_connection
    persona_module.get_connection = mock_get_connection
    mo_module.get_connection = mock_get_connection
    rb_module.get_connection = mock_get_connection
    skills_module.get_connection = mock_get_connection

    init_db()

    _shared_state['mock_get_connection'] = mock_get_connection

    yield main_conn

    # Restore original connection factories
    db_module.get_connection = original_get_connection
    ws_module.get_connection = original_get_connection
    sb_module.get_connection = original_get_connection
    persona_module.get_connection = original_get_connection
    mo_module.get_connection = original_get_connection
    rb_module.get_connection = original_get_connection
    skills_module.get_connection = original_get_connection
    main_conn.close()

@pytest.fixture
def api_client(db_conn):
    """Create a TestClient for web server endpoints."""
    import src.web_server as ws_module
    ws_module.memory_orch._get_connection = _shared_state['mock_get_connection']
    ws_module.bootstrap._get_connection = _shared_state['mock_get_connection']
    client = TestClient(ws_module.app)
    return client

# ==========================================
# 1. Header Resolution & Isolation Tests (V1-T5)
# ==========================================

def test_header_resolution_isolation(db_conn, api_client):
    # Enable Auth for test
    import src.config
    original_require_auth = src.config.REQUIRE_AUTH
    src.config.REQUIRE_AUTH = True

    try:
        # 1. Insert Party A matching via public_key column
        party_a_id = "party_a"
        db_conn.execute(
            "INSERT INTO parties (id, name, role, public_key, metadata) VALUES (?, ?, ?, ?, ?);",
            (party_a_id, "User A", "user", "api_key_a", '{}')
        )
        # Seed profile
        db_conn.execute(
            "INSERT INTO interaction_profiles (party_id, response_style, tone_bias) VALUES (?, ?, ?);",
            (party_a_id, "concise", "sarcastic")
        )

        # 2. Insert Party B matching via metadata JSON api_key
        party_b_id = "party_b"
        db_conn.execute(
            "INSERT INTO parties (id, name, role, public_key, metadata) VALUES (?, ?, ?, ?, ?);",
            (party_b_id, "User B", "user", None, '{"api_key": "api_key_b"}')
        )

        # 3. Insert Party C matching via metadata JSON device_fingerprint
        party_c_id = "party_c"
        db_conn.execute(
            "INSERT INTO parties (id, name, role, public_key, metadata) VALUES (?, ?, ?, ?, ?);",
            (party_c_id, "User C", "user", None, '{"device_fingerprint": "fingerprint_c"}')
        )
        db_conn.commit()

        # Clear LRU caches to make sure it loads fresh
        import src.web_server
        src.web_server.resolve_party_by_api_key.cache_clear()
        src.web_server.resolve_party_by_fingerprint.cache_clear()

        # Match A via X-API-Key (public_key column)
        resp = api_client.post("/api/chat", json={"message": "ping"}, headers={"X-API-Key": "api_key_a"})
        assert resp.status_code == 200

        # Match B via X-API-Key (metadata JSON)
        resp = api_client.post("/api/chat", json={"message": "ping"}, headers={"X-API-Key": "api_key_b"})
        assert resp.status_code == 200

        # Match C via X-Device-Fingerprint (metadata JSON)
        resp = api_client.post("/api/chat", json={"message": "ping"}, headers={"X-Device-Fingerprint": "fingerprint_c"})
        assert resp.status_code == 200

        # Hierarchy: Check X-API-Key wins over X-Device-Fingerprint
        # Key 'api_key_a' (party_a, user) + Fingerprint 'fingerprint_c' (party_c, user)
        # If API Key wins, we authenticate as party_a
        import src.web_server as ws_module
        req_mock = MagicMock()
        req_mock.headers = {
            "X-API-Key": "api_key_a",
            "X-Device-Fingerprint": "fingerprint_c"
        }
        resolved = ws_module.get_current_party(req_mock)
        assert resolved["party_id"] == party_a_id

    finally:
        src.config.REQUIRE_AUTH = original_require_auth


# ==========================================
# 2. Regression Watcher Tests (V1-T6)
# ==========================================

@patch("src.sandbox_session.get_active_sandbox")
@patch("src.sandbox_session.run_sandbox_tests")
@patch("src.sandbox_session.abort_sandbox_session")
@patch("src.sandbox_session.cleanup_git_sandbox")
@patch("src.sandbox_session.clear_sandbox_session")
@patch("src.sandbox_session.shutil.copy2")
def test_regression_watcher_flow(
    mock_copy,
    mock_clear,
    mock_cleanup,
    mock_abort,
    mock_run_tests,
    mock_get_active_sb,
    db_conn
):
    import src.config as cfg
    orig_root = cfg.ROOT_DIR
    tmp_path = Path("/tmp/test_janus_v1_p1")
    cfg.ROOT_DIR = tmp_path

    # Active sandbox path contains a tests directory to trigger execution
    sandbox_path = tmp_path / "sandbox"
    (sandbox_path / "tests").mkdir(parents=True, exist_ok=True)

    mock_get_active_sb.return_value = {
        "active_sandbox_path": str(sandbox_path),
        "active_sandbox_branch": "janus-test-branch",
        "active_sandbox_status": "active"
    }

    try:
        # Case 1: Sandbox tests failed (Regression)
        mock_run_tests.return_value = (False, "=== 1 failed in 0.5s ===")
        with pytest.raises(RuntimeError) as exc:
            ship_sandbox_session()
        assert "Regression detected" in str(exc.value)
        mock_abort.assert_called_once()
        mock_abort.reset_mock()

        # Verify regression logged in database
        logs = db_conn.execute("SELECT message_content FROM episodic_memory WHERE speaker = 'system';").fetchall()
        assert len(logs) == 1
        assert "Regression Watcher aborted sandbox ship flow" in logs[0]["message_content"]

        # Clear episodic memory to reset system logs
        db_conn.execute("DELETE FROM episodic_memory;")
        db_conn.commit()

        # Case 2: Sandbox tests passed, inserts first baseline
        mock_run_tests.return_value = (True, "=== 10 passed in 1.2s ===\nTOTAL          100     20    80%")
        # Mock modifications list
        with patch("src.sandbox_session.get_sandbox_modified_files", return_value=["src/main.py"]):
            main_py = sandbox_path / "src" / "main.py"
            main_py.parent.mkdir(parents=True, exist_ok=True)
            main_py.write_text("print('hello')")
            copied = ship_sandbox_session()
            assert copied == ["src/main.py"]
            mock_clear.assert_called_once()
            mock_clear.reset_mock()

        # Verify baseline inserted
        baselines = db_conn.execute("SELECT total_tests, passed_tests, failed_tests, coverage_percentage FROM test_run_baselines;").fetchall()
        assert len(baselines) == 1
        assert baselines[0]["total_tests"] == 10
        assert baselines[0]["passed_tests"] == 10
        assert baselines[0]["failed_tests"] == 0
        assert baselines[0]["coverage_percentage"] == 80.0

        # Case 3: Tests pass but coverage drops (Regression)
        # Mock sandbox active session again
        mock_get_active_sb.return_value = {
            "active_sandbox_path": str(sandbox_path),
            "active_sandbox_branch": "janus-test-branch",
            "active_sandbox_status": "active"
        }
        mock_run_tests.return_value = (True, "=== 10 passed in 1.1s ===\nTOTAL          100     25    75%")
        with pytest.raises(RuntimeError) as exc:
            ship_sandbox_session()
        assert "Regression detected: Coverage dropped from 80.0% to 75.0%." in str(exc.value)
        mock_abort.assert_called_once()
        mock_abort.reset_mock()

        # Case 4: Tests pass, coverage is None (Graceful Degradation)
        mock_get_active_sb.return_value = {
            "active_sandbox_path": str(sandbox_path),
            "active_sandbox_branch": "janus-test-branch",
            "active_sandbox_status": "active"
        }
        mock_run_tests.return_value = (True, "=== 10 passed in 1.1s ===")  # No coverage output
        with patch("src.sandbox_session.get_sandbox_modified_files", return_value=["src/main.py"]):
            main_py = sandbox_path / "src" / "main.py"
            main_py.parent.mkdir(parents=True, exist_ok=True)
            main_py.write_text("print('hello')")
            copied = ship_sandbox_session()
            assert copied == ["src/main.py"]

        # Verify second baseline recorded with NULL coverage
        baselines = db_conn.execute("SELECT coverage_percentage FROM test_run_baselines ORDER BY id DESC LIMIT 1;").fetchone()
        assert baselines["coverage_percentage"] is None

    finally:
        cfg.ROOT_DIR = orig_root
        import shutil
        shutil.rmtree(tmp_path, ignore_errors=True)


# ==========================================
# 3. Episodic Memory Cleanup & TTL Tests (V1-T7)
# ==========================================

def test_episodic_memory_cleanup_ttl(db_conn):
    # Clear episodic memory first
    db_conn.execute("DELETE FROM episodic_memory;")
    db_conn.commit()

    # Seed retention_days config (e.g., 30 days)
    db_conn.execute(
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) VALUES ('memory.retention_days', '30', 1);"
    )
    # Clear last run time to ensure it triggers
    db_conn.execute(
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) VALUES ('memory.last_cleanup_time', '', 1);"
    )
    db_conn.commit()

    # Helper function to insert memory with specific timestamp
    def insert_old_memory(speaker, content, days_ago):
        target_time = datetime.datetime.utcnow() - datetime.timedelta(days=days_ago)
        ts_str = target_time.strftime("%Y-%m-%d %H:%M:%S")
        db_conn.execute(
            "INSERT INTO episodic_memory (speaker, message_content, context_type, timestamp) VALUES (?, ?, 'user_visible', ?);",
            (speaker, content, ts_str)
        )
        db_conn.commit()

    # 1. Insert recent memory (10 days old)
    insert_old_memory("user", "Hello 10 days ago", 10)
    # 2. Insert expired memory (40 days old)
    insert_old_memory("user", "Hello 40 days ago", 40)

    # Verify both exist
    rows = db_conn.execute("SELECT message_content FROM episodic_memory;").fetchall()
    assert len(rows) == 2

    # Execute episodic memory cleanup skill
    res = DynamicSkillExecutor.execute("cleanup_episodic_memory", {}, party_id="system")
    assert res["success"] is True
    assert "Episodic memory cleanup complete" in res["result"]

    # Verify only recent memory remains
    rows_after = db_conn.execute("SELECT message_content FROM episodic_memory;").fetchall()
    assert len(rows_after) == 1
    assert rows_after[0]["message_content"] == "Hello 10 days ago"

    # Verify last_cleanup_time is now populated
    last_cleanup = db_conn.execute("SELECT config_value FROM system_config WHERE config_key = 'memory.last_cleanup_time';").fetchone()
    assert last_cleanup["config_value"] != ""

    # Re-run immediately: it should skip cleanup to prevent duplicate daily database load
    res_skipped = DynamicSkillExecutor.execute("cleanup_episodic_memory", {}, party_id="system")
    assert res_skipped["success"] is True
    assert "Episodic memory cleanup skipped" in res_skipped["result"]
