import os
import sys
import pytest
import json
from unittest.mock import patch, MagicMock
import src.config
from src.database import init_db, get_connection
from src.skills import SafeReplication
from src.persona import handle_replication_command

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

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
        if "test_janus.db" in str(database):
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
    
    # Verify child DB got initialized (e.g. check child's agent_skills table is populated from instincts)
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
