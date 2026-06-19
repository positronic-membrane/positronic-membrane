import json
from unittest.mock import MagicMock, patch

import pytest

import src.config
from src.database import (
    deactivate_helper_agent,
    get_connection,
    get_pending_swarm_messages,
    init_db,
    mark_swarm_message_processed,
    register_helper_agent,
    send_swarm_message,
)
from src.persona import handle_replication_command
from src.skills import SafeReplication


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB for testing."""
    temp_db = tmp_path / "test_janus_swarm.db"
    orig_db_path = src.config.DB_PATH
    orig_db_type = src.config.DB_TYPE
    orig_spawn = src.config.SPAWN_PROVIDER

    src.config.DB_PATH = str(temp_db)
    src.config.DB_TYPE = "sqlite"
    src.config.SPAWN_PROVIDER = "local"

    init_db()
    yield
    src.config.DB_PATH = orig_db_path
    src.config.DB_TYPE = orig_db_type
    src.config.SPAWN_PROVIDER = orig_spawn

def test_message_bus_operations():
    """Verify messages can be sent, retrieved, and processed in the SQLite message bus."""
    # 1. Send task request
    send_swarm_message("proposer", "explorer", "task_request", "Search for Git Hook security")

    # 2. Retrieve pending messages for explorer
    pending = get_pending_swarm_messages("explorer")
    assert len(pending) == 1
    msg_id, sender_id, msg_type, content, _ = pending[0]
    assert sender_id == "proposer"
    assert msg_type == "task_request"
    assert content == "Search for Git Hook security"

    # 3. Process the message
    mark_swarm_message_processed(msg_id)

    # 4. Verify no pending messages remain
    pending_after = get_pending_swarm_messages("explorer")
    assert len(pending_after) == 0

def test_dynamic_helper_agent_registry():
    """Verify helper agents can be dynamically registered and deactivated."""
    agent_id = "test_helper"
    name = "Test Helper Agent"
    prompt = "You are a test helper agent."

    # 1. Register agent
    register_helper_agent(agent_id, name, prompt)

    # Verify in DB
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT agent_name, system_prompt, is_active FROM agent_registry WHERE agent_id = ?;", (agent_id,))
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == name
    assert row[1] == prompt
    assert row[2] == 1

    # 2. Deactivate agent
    deactivate_helper_agent(agent_id)

    # Verify in DB
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_active FROM agent_registry WHERE agent_id = ?;", (agent_id,))
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == 0

# --- Consolidating from test_phase3_cloud_spawning.py & test_phase7_instincts_replication.py ---

def test_seed_instincts_populates():
    """Verify that calling init_db seeds the instincts table with core categories."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()

    # Check that schema table instincts exist
    cursor.execute("SELECT COUNT(*) FROM instincts WHERE category = 'schema';")
    assert cursor.fetchone()[0] > 0, "No schema instincts seeded"

    # Check that constitution, tool, boot, and meta instincts exist
    cursor.execute("SELECT category, COUNT(*) FROM instincts GROUP BY category;")
    categories = {row[0]: row[1] for row in cursor.fetchall()}
    assert "schema" in categories
    assert "constitution" in categories
    assert "tool" in categories
    assert "boot" in categories
    assert "meta" in categories

    conn.close()

def test_safe_replication_get_methods():
    """Verify get_instincts and get_children SDK calls return data."""
    rep = SafeReplication()

    instincts = rep.get_instincts()
    assert len(instincts) > 0
    assert any(i["category"] == "schema" for i in instincts)

    # Initially no children spawned
    children = rep.get_children()
    assert len(children) == 0

def test_spawn_child_path_safety():
    """Verify that attempting to spawn a child outside the workspace directory raises PermissionError."""
    rep = SafeReplication()
    with pytest.raises(PermissionError):
        rep.spawn_child("attacker-child", "../../../../outside-dir")

@patch("src.config.get_effective_workspace_root")
@patch("subprocess.Popen")
@patch("shutil.copytree")
def test_spawn_child_success(mock_copytree, mock_popen, mock_get_root, tmp_path):
    """Verify copying codebase, bootstrapping child DB, and launching subprocess works."""
    rep = SafeReplication()
    child_path = tmp_path / "my-child-instance"
    mock_get_root.return_value = tmp_path

    # Create real SQLite connection to run in-memory so bootstrap sql actually executes
    import sqlite3
    original_connect = sqlite3.connect
    real_child_db = original_connect(":memory:")

    class ConnectionProxy:
        def __init__(self, conn):
            self._conn = conn
        def __getattr__(self, name):
            if name == "close":
                return lambda: None
            return getattr(self._conn, name)

    # Mock subprocess return
    mock_process = MagicMock()
    mock_process.pid = 9999
    mock_popen.return_value = mock_process

    def mock_connect(database, *args, **kwargs):
        if "test_janus.db" in str(database) or "test_janus_swarm.db" in str(database):
            return original_connect(database, *args, **kwargs)
        if "janus.db" in str(database) or database == ":memory:":
            return ConnectionProxy(real_child_db)
        return original_connect(database, *args, **kwargs)

    with patch("sqlite3.connect", side_effect=mock_connect) as mock_sqlite_connect:
        res = rep.spawn_child("my-child-instance", "my-child-instance")

    assert res["success"] is True
    assert res["child_pid"] == 9999
    assert res["status"] == "alive"

    # Verify copytree was called
    mock_copytree.assert_called_once()

    # Verify child DB got initialized
    cursor = real_child_db.cursor()
    cursor.execute("SELECT COUNT(*) FROM agent_skills;")
    assert cursor.fetchone()[0] > 0

    # Verify parent database registered child in spawn_log
    conn = get_connection(read_only_constitution=True)
    row = conn.execute("SELECT child_path, child_pid, status FROM spawn_log WHERE child_path = ?;", (str(child_path.resolve()),)).fetchone()
    assert row is not None
    assert row[1] == 9999
    assert row[2] == "alive"

    # Verify child registered parent and child in parties table
    cursor.execute("SELECT id, role, metadata FROM parties;")
    parties = {r[0]: (r[1], json.loads(r[2])) for r in cursor.fetchall()}
    assert "parent" in parties
    assert parties["parent"][0] == "admin"
    assert parties["parent"][1]["type"] == "parent"
    assert "my-child-instance" in parties

    real_child_db.close()
    conn.close()

def test_replication_slash_commands():
    """Verify slash commands route to correct responses."""
    # List children
    res_list_empty = handle_replication_command("/children")
    assert "No child Janus instances" in res_list_empty

    # Try spawn command syntax checks
    res_err = handle_replication_command("/spawn")
    assert "Usage: /spawn" in res_err

    res_err_incomplete = handle_replication_command("/spawn only-name")
    assert "Usage: /spawn" in res_err_incomplete

@patch("src.skills.get_connection")
@patch("shutil.copytree")
@patch("shutil.rmtree")
@patch("psycopg2.connect")
def test_spawn_child_postgres_schema(mock_connect, mock_rmtree, mock_copytree, mock_get_conn, tmp_path):
    # Set DB type to postgres
    src.config.DB_TYPE = "postgres"
    src.config.DATABASE_URL = "postgresql://user:pass@host:port/dbname"
    src.config.SPAWN_PROVIDER = "ecs"

    # Mock parent DB queries
    mock_parent_conn = MagicMock()
    mock_parent_cur = MagicMock()
    mock_parent_cur.fetchall.side_effect = [
        [{"key": "schema_key", "value": "CREATE TABLE instincts (id SERIAL PRIMARY KEY);"}] * 1,
        [
            {"key": "core_constitution", "value": "[]", "category": "constitution", "version": 1},
            {"key": "agent_skills", "value": "[]", "category": "tool", "version": 1},
            {"key": "system_config", "value": "[]", "category": "boot", "version": 1}
        ]
    ]
    mock_parent_conn.cursor.return_value = mock_parent_cur
    mock_get_conn.return_value = mock_parent_conn

    # Mock psycopg2 with context managers
    mock_child_conn = MagicMock()
    mock_child_cur = MagicMock()
    mock_child_conn.cursor.return_value.__enter__.return_value = mock_child_cur
    mock_connect.return_value = mock_child_conn

    # Execute spawn child
    swarm = SafeReplication()
    res = swarm.spawn_child("child_alpha", "child_alpha")

    # Verify child database schema was initialized
    mock_child_cur.execute.assert_any_call("CREATE SCHEMA IF NOT EXISTS janus_child_child_alpha;")
    mock_child_cur.execute.assert_any_call("SET search_path TO janus_child_child_alpha;")

    # Verify return attributes for ECS spawning
    assert res["success"] is True
    assert res["child_pid"] == 99999
    assert res["status"] == "alive"
