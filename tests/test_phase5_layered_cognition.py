import os
import pytest
import asyncio
import logging
import re
from unittest.mock import patch, MagicMock
import src.config
from src.database import init_db, get_connection
from src.daemon import run_heartbeat_loop, enqueue_reflex_action, get_cadence_seconds
from src.skills import DynamicSkillExecutor, SafeLayeredCognition

logger = logging.getLogger("TestPhase5")

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    import src.daemon
    src.daemon._consecutive_stagnant_cycles = 0
    src.daemon._last_git_diff_hash = None
    src.daemon._last_db_write_count = None
    src.daemon._last_completed_checkpoints = None

    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

def test_schema_and_seeding():
    """Assert that cognitive_layers and reflex_rules tables are created and seeded correctly."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    
    # Check tables exist
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cognitive_layers';")
    assert cursor.fetchone() is not None
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reflex_rules';")
    assert cursor.fetchone() is not None
    
    # Check default layers
    cursor.execute("SELECT layer_name, cadence_ms FROM cognitive_layers;")
    layers = {row[0]: row[1] for row in cursor.fetchall()}
    assert "high" in layers
    assert layers["high"] == 60000
    assert "mid" in layers
    assert layers["mid"] == 5000
    assert "low" in layers
    assert layers["low"] == 100
    
    # Check default reflex rules
    cursor.execute("SELECT trigger_pattern, action, priority FROM reflex_rules;")
    rules = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    assert ".*\\.py$" in rules
    assert rules[".*\\.py$"] == ("evaluate_goals", 5)
    assert ".*requirements\\.txt$" in rules
    assert rules[".*requirements\\.txt$"] == ("scan_workspace", 10)
    
    conn.close()

def test_sdk_layered_cognition():
    """Verify SafeLayeredCognition triggers reflexes and lists layer states."""
    sdk_lc = SafeLayeredCognition()
    
    # Test get_layers
    layers = sdk_lc.get_layers()
    assert len(layers) == 3
    layer_names = [l["name"] for l in layers]
    assert "high" in layer_names
    assert "mid" in layer_names
    assert "low" in layer_names
    
    # Test trigger_reflex mocks enqueue_reflex_action in src.daemon
    with patch("src.daemon.enqueue_reflex_action") as mock_enqueue:
        sdk_lc.trigger_reflex("scan_workspace", 8)
        mock_enqueue.assert_called_once_with("scan_workspace", 8)

@pytest.mark.asyncio
async def test_directory_watcher_reflex_trigger(tmp_path, monkeypatch):
    """Mock DirectoryWatcher events and assert that matching patterns successfully enqueue priority tasks."""
    captured_callback = None
    
    class MockDirectoryWatcher:
        def __init__(self, path, callback=None):
            nonlocal captured_callback
            captured_callback = callback
            self.path = path
        def watch(self, interval=2.0, stop_event=None):
            pass
            
    # Apply mocks to watcher in daemon
    monkeypatch.setattr("src.daemon.DirectoryWatcher", MockDirectoryWatcher)
    monkeypatch.setattr("src.daemon.orchestrate_workspace_snapshot", lambda x: None)
    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)
    
    # Run the loop briefly just to initialize and capture callback
    task = asyncio.create_task(run_heartbeat_loop())
    await asyncio.sleep(0.1) # Wait a bit for loop initialization
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
        
    assert captured_callback is not None
    
    # Now, test the captured callback behavior
    with patch("src.daemon.enqueue_reflex_action") as mock_enqueue:
        # 1. Change to .py file -> should trigger evaluate_goals (priority 5)
        captured_callback({'added': ['src/daemon.py'], 'modified': []})
        mock_enqueue.assert_called_once_with("evaluate_goals", 5)
        mock_enqueue.reset_mock()
        
        # 2. Change to requirements.txt -> should trigger scan_workspace (priority 10)
        captured_callback({'added': [], 'modified': ['requirements.txt']})
        mock_enqueue.assert_called_once_with("scan_workspace", 10)
        mock_enqueue.reset_mock()
        
        # 3. Change to unrelated file (README.md) -> should not trigger
        captured_callback({'added': ['README.md'], 'modified': []})
        mock_enqueue.assert_not_called()

@pytest.mark.asyncio
async def test_cadence_concurrency_and_ratios(tmp_path, monkeypatch):
    """Assert concurrent execution of high and mid layers runs at expected relative frequencies."""
    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)
    
    monkeypatch.setattr("src.daemon.DirectoryWatcher", lambda *a, **k: MagicMock())
    monkeypatch.setattr("src.codebase.index_codebase", lambda: None)
    
    executed_skills = []
    
    def mock_execute(skill_id, arguments, party_id=None):
        executed_skills.append(skill_id)
        return {"success": True, "result": "mocked"}
        
    monkeypatch.setattr(DynamicSkillExecutor, "execute", mock_execute)
    
    # Set high boredom threshold, stagnant threshold, and user presence to prevent reflection triggers and governor halts during the concurrency test
    conn = get_connection()
    conn.execute("UPDATE system_config SET config_value = '99' WHERE config_key = 'boredom_threshold';")
    conn.execute("INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) VALUES ('governor.stagnant_threshold', '99', 1);")
    conn.execute("UPDATE system_config SET config_value = 'active' WHERE config_key = 'user_presence_status';")
    conn.commit()
    conn.close()

    # Start heartbeat daemon
    task = asyncio.create_task(run_heartbeat_loop())
    
    # Wait for 5.5 seconds
    # In 5.5 seconds:
    # - Mid layer ticks at ~1s, 2s, 3s, 4s, 5s (5 ticks).
    # - High layer ticks at ~2s, 4s (2 ticks).
    await asyncio.sleep(5.5)
    
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
        
    # Count occurrences
    mid_skills = ["check_presence", "evaluate_drives"]
    high_skills = ["decay_self_model", "consolidate_memories", "evaluate_goals"]
    
    counts = {s: executed_skills.count(s) for s in (mid_skills + high_skills)}
    
    print(f"\nExecuted skills list: {executed_skills}")
    print(f"Counts: {counts}")
    
    # Verify execution ratio
    assert counts["check_presence"] >= 4
    assert counts["evaluate_drives"] >= 4
    
    assert counts["decay_self_model"] >= 2
    assert counts["consolidate_memories"] >= 2
    assert counts["evaluate_goals"] >= 2
    
    assert counts["check_presence"] > counts["decay_self_model"]
