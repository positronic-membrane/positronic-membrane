import os
import pytest
import asyncio
from unittest.mock import patch
import src.config
from src.database import init_db, get_connection
from src.skills import DynamicSkillExecutor, SafeDrives, SafeSwarm
from src.daemon import run_heartbeat_loop

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB for testing."""
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    
    yield
    src.config.DB_PATH = orig_db_path

@pytest.fixture(autouse=True)
def setup_test_vector_db(tmp_path):
    """Isolates vector db."""
    import src.memory
    orig_path = src.config.VECTOR_DB_PATH
    src.config.VECTOR_DB_PATH = str(tmp_path / "test_chromadb")
    src.memory._chroma_client = None
    src.memory._collections = {}
    yield
    src.config.VECTOR_DB_PATH = orig_path

def test_safe_drives_sdk():
    """Verify sdk['drives'] functions correctly query and update drive_state."""
    drives = SafeDrives()
    
    # Initial boredom should be 0
    assert drives.get("boredom") == 0
    
    # Setting boredom
    drives.set("boredom", 5)
    assert drives.get("boredom") == 5
    
    # Incrementing boredom
    val = drives.increment("boredom", 2)
    assert val == 7
    assert drives.get("boredom") == 7
    
    # Throws error on invalid drive key
    with pytest.raises(ValueError):
        drives.get("happiness")

def test_check_presence_skill(tmp_path, monkeypatch):
    """Verify check_presence skill walks filesystem and updates DB presence config."""
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)
    
    # Run first check_presence. Since tmp_path is empty, status should be idle
    res = DynamicSkillExecutor.execute("check_presence", {})
    assert res["success"]
    assert "idle" in res["result"]
    
    conn = get_connection()
    row = conn.execute("SELECT config_value FROM system_config WHERE config_key = 'user_presence_status';").fetchone()
    assert row[0] == "idle"
    conn.close()
    
    # Touch a file to simulate active user
    test_file = tmp_path / "index.py"
    test_file.touch()
    
    # Run check_presence again
    res = DynamicSkillExecutor.execute("check_presence", {})
    assert res["success"]
    assert "active" in res["result"]
    
    conn = get_connection()
    row = conn.execute("SELECT config_value FROM system_config WHERE config_key = 'user_presence_status';").fetchone()
    assert row[0] == "active"
    conn.close()

@patch("src.llm.query_agent")
def test_evaluate_drives_triggers_reflection(mock_query):
    """Verify evaluate_drives triggers swarm reflection cycle when boredom threshold is crossed."""
    mock_query.return_value = "PROPOSED_ACTION: scan_workspace"
    
    # Set threshold to 2 in database
    conn = get_connection()
    conn.execute("UPDATE system_config SET config_value = '2' WHERE config_key = 'boredom_threshold';")
    conn.execute("UPDATE system_config SET config_value = 'idle' WHERE config_key = 'user_presence_status';")
    conn.commit()
    conn.close()
    
    # Initialize boredom to 0
    drives = SafeDrives()
    drives.set("boredom", 0)
    
    # Clear triggers queue
    from src.daemon import _pending_swarm_triggers
    _pending_swarm_triggers.clear()
    
    # Run tick 1
    res = DynamicSkillExecutor.execute("evaluate_drives", {})
    assert res["success"]
    assert "Boredom incremented to 1/2" in res["result"]
    assert not _pending_swarm_triggers
    
    # Run tick 2 -> threshold met, trigger reflection
    res = DynamicSkillExecutor.execute("evaluate_drives", {})
    assert res["success"]
    assert "Boredom threshold met" in res["result"]
    assert len(_pending_swarm_triggers) == 1
    assert drives.get("boredom") == 0 # Reset to 0
