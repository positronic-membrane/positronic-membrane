import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest

import src.config
from src.daemon import check_smart_governor_stagnation, detect_user_presence, run_heartbeat_loop
from src.database import get_connection, init_db
from src.skills import DynamicSkillExecutor, SafeLayeredCognition

logger = logging.getLogger("TestDaemon")

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    import src.daemon
    src.daemon._consecutive_stagnant_cycles = 0
    src.daemon._last_git_diff_hash = None
    src.daemon._last_db_write_count = None
    src.daemon._last_completed_checkpoints = None

    temp_db = tmp_path / "test_janus_daemon.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

def test_user_presence_detection(tmp_path):
    """Verify that file modifications indicate user presence, except ignored items."""
    # Empty directory -> no user presence
    assert not detect_user_presence(tmp_path, max_age_seconds=10)

    # Create an active workspace file
    test_file = tmp_path / "index.py"
    test_file.touch()

    # File touched now -> presence detected
    assert detect_user_presence(tmp_path, max_age_seconds=10)

    # Check that database files, git, and venv are ignored
    test_file.unlink()

    ignored_files = ["janus.db", "janus.db-wal", ".DS_Store"]
    for f in ignored_files:
        (tmp_path / f).touch()

    # Only ignored files touched -> presence should be False
    assert not detect_user_presence(tmp_path, max_age_seconds=10)

    # Clean up ignored files
    for f in ignored_files:
        (tmp_path / f).unlink()

@pytest.mark.asyncio
@patch("src.memory.add_memory")
@patch("src.memory.query_memories")
@patch("src.skills.query_agent")
async def test_heartbeat_loop_execution(mock_query, mock_query_memories, mock_add_memory, tmp_path, monkeypatch):
    """
    Test that the heartbeat daemon loop runs, increments boredom,
    and resets boredom when threshold is reached.
    """
    mock_query_memories.return_value = [{"id": "mem_1", "content": "Memory match content", "metadata": {}, "distance": 0.1}]
    mock_add_memory.return_value = None

    def side_effect(agent_id, prompt, system_override=None, **kwargs):
        if agent_id == "proposer":
            return "PROPOSED_ACTION: Scan codebase docs"
        elif agent_id == "critic":
            return "Decision: 1\nJustification: Action is safe and complies with all rules."
        elif agent_id == "archivist":
            if "curiosity" in prompt.lower() or "curiosity_topics" in prompt.lower():
                return "CURIOSITY_TOPICS: git hooks, sqlite locks"
            return "Janus execution summary nugget logged."
        return ""
    mock_query.side_effect = side_effect

    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "BOREDOM_THRESHOLD", 2)
    monkeypatch.setattr(src.config, "N_LOOP_LIMIT", 3)
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)

    conn = get_connection()
    conn.execute("UPDATE system_config SET config_value = '2' WHERE config_key = 'boredom_threshold';")
    conn.commit()
    conn.close()

    daemon_task = asyncio.create_task(run_heartbeat_loop())
    await asyncio.sleep(5.5)

    daemon_task.cancel()
    try:
        await daemon_task
    except asyncio.CancelledError:
        pass

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM internal_deliberations;")
    deliberations_count = cursor.fetchone()[0]
    conn.close()

    assert deliberations_count >= 1

@pytest.mark.asyncio
@patch("src.memory.add_memory")
@patch("src.memory.query_memories")
@patch("src.skills.query_agent")
async def test_modify_code_path_verification(mock_query, mock_query_memories, mock_add_memory, tmp_path, monkeypatch):
    """Verify that modify_code tool execution validates directories and reports error on parent path."""
    mock_query_memories.return_value = []
    mock_add_memory.return_value = None

    def side_effect(agent_id, prompt, system_override=None, **kwargs):
        if agent_id == "proposer":
            return "PROPOSED_ACTION: modify_code: invalid_parent_dir/new_file.py | print('Hello')"
        elif agent_id == "critic":
            return "Decision: 1\nJustification: Approved."
        elif agent_id == "archivist":
            return "Curiosity rules or summary."
        return ""
    mock_query.side_effect = side_effect

    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "BOREDOM_THRESHOLD", 1)
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)

    conn = get_connection()
    conn.execute("UPDATE system_config SET config_value = '1' WHERE config_key = 'boredom_threshold';")
    conn.commit()
    conn.close()

    daemon_task = asyncio.create_task(run_heartbeat_loop())
    await asyncio.sleep(2.5)
    daemon_task.cancel()
    try:
        await daemon_task
    except asyncio.CancelledError:
        pass

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT proposed_action, critic_decision FROM internal_deliberations;")
    rows = cursor.fetchall()

    cursor.execute("SELECT message_content FROM episodic_memory WHERE speaker = 'system' AND message_content LIKE '%Action execution failed%';")
    system_errors = cursor.fetchall()
    conn.close()

    assert len(rows) >= 1
    assert "invalid_parent_dir/new_file.py" in rows[0][0]
    assert len(system_errors) >= 1
    assert "parent directory 'invalid_parent_dir' does not exist" in system_errors[0][0]

# --- Consolidating from test_phase5_layered_cognition.py ---

def test_schema_and_seeding():
    """Assert that cognitive_layers and reflex_rules tables are created and seeded correctly."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cognitive_layers';")
    assert cursor.fetchone() is not None

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reflex_rules';")
    assert cursor.fetchone() is not None

    cursor.execute("SELECT layer_name, cadence_ms FROM cognitive_layers;")
    layers = {row[0]: row[1] for row in cursor.fetchall()}
    assert "high" in layers
    assert layers["high"] == 60000
    assert "mid" in layers
    assert layers["mid"] == 5000
    assert "low" in layers
    assert layers["low"] == 100

    cursor.execute("SELECT trigger_pattern, action, priority FROM reflex_rules;")
    rules = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    assert ".*\\.py$" in rules
    assert rules[".*\\.py$"] == ("evaluate_goals", 5)

    conn.close()

def test_sdk_layered_cognition():
    """Verify SafeLayeredCognition triggers reflexes and lists layer states."""
    sdk_lc = SafeLayeredCognition()

    layers = sdk_lc.get_layers()
    assert len(layers) == 3
    layer_names = [l["name"] for l in layers]
    assert "high" in layer_names
    assert "mid" in layer_names

    with patch("src.daemon.enqueue_reflex_action") as mock_enqueue:
        sdk_lc.trigger_reflex("scan_workspace", 8)
        mock_enqueue.assert_called_once_with("scan_workspace", 8)

@pytest.mark.asyncio
async def test_directory_watcher_reflex_trigger(tmp_path, monkeypatch):
    """Mock DirectoryWatcher events and assert callback triggers matching priority tasks."""
    captured_callback = None

    class MockDirectoryWatcher:
        def __init__(self, path, callback=None):
            nonlocal captured_callback
            captured_callback = callback
            self.path = path
        def watch(self, interval=2.0, stop_event=None):
            pass

    monkeypatch.setattr("src.daemon.DirectoryWatcher", MockDirectoryWatcher)
    monkeypatch.setattr("src.daemon.orchestrate_workspace_snapshot", lambda x: None)
    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)

    task = asyncio.create_task(run_heartbeat_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert captured_callback is not None

    with patch("src.daemon.enqueue_reflex_action") as mock_enqueue:
        captured_callback({'added': ['src/daemon.py'], 'modified': []})
        mock_enqueue.assert_called_once_with("evaluate_goals", 5)
        mock_enqueue.reset_mock()

        captured_callback({'added': [], 'modified': ['requirements.txt']})
        mock_enqueue.assert_called_once_with("scan_workspace", 10)
        mock_enqueue.reset_mock()

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

    conn = get_connection()
    conn.execute("UPDATE system_config SET config_value = '99' WHERE config_key = 'boredom_threshold';")
    conn.execute("INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) VALUES ('governor.stagnant_threshold', '99', 1);")
    conn.execute("UPDATE system_config SET config_value = 'active' WHERE config_key = 'user_presence_status';")
    conn.commit()
    conn.close()

    task = asyncio.create_task(run_heartbeat_loop())
    await asyncio.sleep(5.5)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    mid_skills = ["check_presence", "evaluate_drives"]
    high_skills = ["decay_self_model", "consolidate_memories", "evaluate_goals"]

    counts = {s: executed_skills.count(s) for s in (mid_skills + high_skills)}

    assert counts["check_presence"] >= 4
    assert counts["evaluate_drives"] >= 4

    assert counts["decay_self_model"] >= 2
    assert counts["consolidate_memories"] >= 2
    assert counts["evaluate_goals"] >= 2
    assert counts["check_presence"] > counts["decay_self_model"]

# --- Consolidating from test_v1_priority0.py ---

@patch("subprocess.run")
def test_smart_governor_stagnation_checks(mock_run):
    import src.daemon

    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = ""
    mock_run.return_value = mock_res

    stagnant, desc = check_smart_governor_stagnation()
    assert stagnant is False
    assert src.daemon._consecutive_stagnant_cycles == 0

    stagnant, desc = check_smart_governor_stagnation()
    assert stagnant is True
    assert src.daemon._consecutive_stagnant_cycles == 1

    stagnant, desc = check_smart_governor_stagnation()
    assert stagnant is True
    assert src.daemon._consecutive_stagnant_cycles == 2

@patch("subprocess.run")
def test_smart_governor_progress_reset(mock_run):
    import src.daemon

    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = ""
    mock_run.return_value = mock_res

    check_smart_governor_stagnation()

    check_smart_governor_stagnation()
    assert src.daemon._consecutive_stagnant_cycles == 1

    mock_res.stdout = "diff --git a/src/main.py b/src/main.py\n+ # some changes"

    stagnant, desc = check_smart_governor_stagnation()
    assert stagnant is False
    assert src.daemon._consecutive_stagnant_cycles == 0
