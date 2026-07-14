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
                return 'CURIOSITY_TOPICS: ["git hooks", "sqlite locks"]'
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
    """Verify that proposing the removed modify_code skill results in an execution failure being logged."""
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
    # V3-T3: modify_code is removed from parse_action — hits the mock fallback silently,
    # no skill execution happens, no "Action execution failed" error is logged.
    assert len(system_errors) == 0

@pytest.mark.asyncio
@patch("src.daemon.send_webhook_notification")
@patch("src.daemon.run_interval_skills")
@patch("src.codebase.add_memory")
@patch("src.memory.add_memory")
@patch("src.memory.query_memories")
@patch("src.skills.query_agent")
async def test_governor_halt_sends_webhook_notification(
    mock_query, mock_query_memories, mock_add_memory, mock_codebase_add_memory,
    mock_interval_skills, mock_webhook,
    tmp_path, monkeypatch
):
    """A Smart Governor halt (stagnation or hard loop cap) must dispatch a 'governor_halt' webhook notification."""
    mock_query_memories.return_value = []
    mock_add_memory.return_value = None
    mock_codebase_add_memory.return_value = None
    mock_query.return_value = ""

    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "N_LOOP_LIMIT", 1)
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)

    daemon_task = asyncio.create_task(run_heartbeat_loop())
    await asyncio.sleep(4.0)
    daemon_task.cancel()
    try:
        await daemon_task
    except asyncio.CancelledError:
        pass

    assert mock_webhook.called
    assert mock_webhook.call_args[0][0] == "governor_halt"

@pytest.mark.asyncio
@patch("src.memory.add_memory")
@patch("src.memory.query_memories")
@patch("src.skills.query_agent")
async def test_dispute_paused_skips_reflection_trigger(
    mock_query, mock_query_memories, mock_add_memory, tmp_path, monkeypatch
):
    """While dispute_paused is set, the mid-layer loop must not run the Proposer/Critic
    reflection cycle, even when a swarm reflection trigger is pending."""
    import src.daemon

    mock_query_memories.return_value = []
    mock_add_memory.return_value = None
    mock_query.return_value = ""

    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "BOREDOM_THRESHOLD", 99)
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)

    conn = get_connection()
    conn.execute("UPDATE system_config SET config_value = 'true' WHERE config_key = 'dispute_paused';")
    conn.execute("UPDATE system_config SET config_value = '99' WHERE config_key = 'boredom_threshold';")
    conn.commit()
    conn.close()

    src.daemon._pending_swarm_triggers.clear()
    src.daemon._pending_swarm_triggers.append("reflection")

    with patch.object(DynamicSkillExecutor, "execute", wraps=DynamicSkillExecutor.execute) as mock_execute:
        daemon_task = asyncio.create_task(run_heartbeat_loop())
        await asyncio.sleep(1.5)
        daemon_task.cancel()
        try:
            await daemon_task
        except asyncio.CancelledError:
            pass

        reflection_calls = [c for c in mock_execute.call_args_list if c[0][0] == "run_reflection_cycle"]
        assert reflection_calls == []

    # The trigger is still pending since it was never drained while paused.
    assert "reflection" in src.daemon._pending_swarm_triggers

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

# --- Issue #65: Smart Loop Governor persisted state, cooldown, and resume paths ---

@pytest.mark.asyncio
@patch("src.daemon.send_webhook_notification")
@patch("src.daemon.run_interval_skills")
@patch("src.codebase.add_memory")
@patch("src.memory.add_memory")
@patch("src.memory.query_memories")
@patch("src.skills.query_agent")
async def test_governor_pause_persists_state_flag(
    mock_query, mock_query_memories, mock_add_memory, mock_codebase_add_memory,
    mock_interval_skills, mock_webhook,
    tmp_path, monkeypatch
):
    """A Smart Governor halt must persist governor.state='paused' and a non-empty
    governor.paused_at, so pause is inspectable independent of the blocked coroutine."""
    mock_query_memories.return_value = []
    mock_add_memory.return_value = None
    mock_codebase_add_memory.return_value = None
    mock_query.return_value = ""

    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "N_LOOP_LIMIT", 1)
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)

    daemon_task = asyncio.create_task(run_heartbeat_loop())
    await asyncio.sleep(4.0)
    daemon_task.cancel()
    try:
        await daemon_task
    except asyncio.CancelledError:
        pass

    conn = get_connection()
    state_row = conn.execute("SELECT config_value FROM system_config WHERE config_key = 'governor.state';").fetchone()
    paused_at_row = conn.execute(
        "SELECT config_value FROM system_config WHERE config_key = 'governor.paused_at';"
    ).fetchone()
    conn.close()

    assert state_row[0] == "paused"
    assert paused_at_row[0]

@pytest.mark.asyncio
@patch("src.daemon.send_webhook_notification")
@patch("src.daemon.run_interval_skills")
@patch("src.codebase.add_memory")
@patch("src.memory.add_memory")
@patch("src.memory.query_memories")
@patch("src.skills.query_agent")
async def test_governor_resume_on_cooldown_expiry(
    mock_query, mock_query_memories, mock_add_memory, mock_codebase_add_memory,
    mock_interval_skills, mock_webhook,
    tmp_path, monkeypatch
):
    """Once paused, the governor must auto-resume once governor.cooldown_minutes
    elapses, even though user_presence_status never becomes 'active'."""
    import src.daemon

    mock_query_memories.return_value = []
    mock_add_memory.return_value = None
    mock_codebase_add_memory.return_value = None
    mock_query.return_value = ""

    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "N_LOOP_LIMIT", 1)
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)

    conn = get_connection()
    conn.execute("UPDATE system_config SET config_value = '1' WHERE config_key = 'governor.cooldown_minutes';")
    conn.commit()
    conn.close()

    # Simulate an hour of elapsed wall-clock time on the very first re-check inside
    # pause_until_user_active(), so the cooldown fires without a real 60s wait.
    # Patches daemon's own _governor_monotonic() indirection rather than the real
    # time.monotonic(), since asyncio's own scheduling also depends on that clock.
    calls = {"n": 0}
    def fake_monotonic():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 3600.0
    monkeypatch.setattr(src.daemon, "_governor_monotonic", fake_monotonic)

    daemon_task = asyncio.create_task(run_heartbeat_loop())
    await asyncio.sleep(4.0)
    daemon_task.cancel()
    try:
        await daemon_task
    except asyncio.CancelledError:
        pass

    resume_calls = [c for c in mock_webhook.call_args_list if c[0][0] == "governor_resume"]
    assert resume_calls, "expected a governor_resume webhook notification"
    assert "cooldown_expiry" in resume_calls[0][0][1]

    conn = get_connection()
    state_row = conn.execute("SELECT config_value FROM system_config WHERE config_key = 'governor.state';").fetchone()
    conn.close()
    assert state_row[0] == "running"

@pytest.mark.asyncio
@patch("src.daemon.send_webhook_notification")
@patch("src.daemon.run_interval_skills")
@patch("src.codebase.add_memory")
@patch("src.memory.add_memory")
@patch("src.memory.query_memories")
@patch("src.skills.query_agent")
async def test_governor_resume_on_user_activity(
    mock_query, mock_query_memories, mock_add_memory, mock_codebase_add_memory,
    mock_interval_skills, mock_webhook,
    tmp_path, monkeypatch
):
    """Detected user presence must flip governor.state back to 'running', not just
    reset the in-memory counters."""
    mock_query_memories.return_value = []
    mock_add_memory.return_value = None
    mock_codebase_add_memory.return_value = None
    mock_query.return_value = ""

    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "N_LOOP_LIMIT", 1)
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)

    # The real check_presence skill re-scans ROOT_DIR on every mid-tick and would
    # overwrite our manual 'active' write below with 'idle' (tmp_path has no fresh
    # non-DB files) — no-op it here so the manual write is what the loop observes.
    real_execute = DynamicSkillExecutor.execute
    def guarded_execute(skill_id, arguments, party_id=None):
        if skill_id == "check_presence":
            return {"success": True, "result": "skipped in test"}
        return real_execute(skill_id, arguments, party_id=party_id)

    with patch.object(DynamicSkillExecutor, "execute", side_effect=guarded_execute):
        daemon_task = asyncio.create_task(run_heartbeat_loop())
        await asyncio.sleep(2.5)

        conn = get_connection()
        state_row = conn.execute(
            "SELECT config_value FROM system_config WHERE config_key = 'governor.state';"
        ).fetchone()
        assert state_row[0] == "paused"
        conn.execute("UPDATE system_config SET config_value = 'active' WHERE config_key = 'user_presence_status';")
        conn.commit()
        conn.close()

        await asyncio.sleep(2.5)
        daemon_task.cancel()
        try:
            await daemon_task
        except asyncio.CancelledError:
            pass

    conn = get_connection()
    state_row = conn.execute("SELECT config_value FROM system_config WHERE config_key = 'governor.state';").fetchone()
    paused_at_row = conn.execute(
        "SELECT config_value FROM system_config WHERE config_key = 'governor.paused_at';"
    ).fetchone()
    conn.close()
    assert state_row[0] == "running"
    assert paused_at_row[0] == ""

@pytest.mark.asyncio
@patch("src.daemon.send_webhook_notification")
@patch("src.daemon.run_interval_skills")
@patch("src.codebase.add_memory")
@patch("src.memory.add_memory")
@patch("src.memory.query_memories")
@patch("src.skills.query_agent")
async def test_governor_resume_on_chat_activity(
    mock_query, mock_query_memories, mock_add_memory, mock_codebase_add_memory,
    mock_interval_skills, mock_webhook,
    tmp_path, monkeypatch
):
    """reset_governor_state('user_chat'), the shared helper called from every chat
    entry point, must resolve an active pause exactly like presence detection does."""
    import src.daemon

    mock_query_memories.return_value = []
    mock_add_memory.return_value = None
    mock_codebase_add_memory.return_value = None
    mock_query.return_value = ""

    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "N_LOOP_LIMIT", 1)
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)

    daemon_task = asyncio.create_task(run_heartbeat_loop())
    await asyncio.sleep(2.5)

    assert src.daemon.get_governor_state() == "paused"
    src.daemon.reset_governor_state("user_chat")
    assert src.daemon.get_governor_state() == "running"

    await asyncio.sleep(1.5)
    daemon_task.cancel()
    try:
        await daemon_task
    except asyncio.CancelledError:
        pass

    resume_calls = [c for c in mock_webhook.call_args_list if c[0][0] == "governor_resume"]
    assert resume_calls
    assert "user_chat" in resume_calls[0][0][1]

def test_governor_chat_trigger_is_noop_when_not_paused():
    """reset_governor_state('user_chat') must NOT reset the stagnation/hard-cap
    counters while the governor is running — otherwise routine chat traffic would
    perpetually suppress the safety valve from ever accumulating enough consecutive
    unproductive cycles to trip. Only 'user_presence' resets unconditionally,
    matching its pre-existing per-tick behavior."""
    import src.daemon
    from src.database import get_connection, increment_consecutive_background_loops

    assert src.daemon.get_governor_state() == "running"
    src.daemon._consecutive_stagnant_cycles = 5
    increment_consecutive_background_loops()

    src.daemon.reset_governor_state("user_chat")

    assert src.daemon._consecutive_stagnant_cycles == 5
    conn = get_connection(read_only_constitution=True)
    loop_count = conn.execute(
        "SELECT config_value FROM system_config WHERE config_key = 'consecutive_background_loops';"
    ).fetchone()
    conn.close()
    assert loop_count[0] == "1"

    # user_presence still resets unconditionally even when not paused (pre-existing behavior).
    src.daemon.reset_governor_state("user_presence")
    assert src.daemon._consecutive_stagnant_cycles == 0

@pytest.mark.asyncio
async def test_reflex_queue_worker_skips_dispatch_while_paused(tmp_path, monkeypatch):
    """File-change-triggered reflex actions must not fire while the Smart Loop
    Governor is paused — otherwise background automation continues through this
    channel even though the mid/high loops correctly stop dispatching."""
    import src.daemon
    from src.daemon import reflex_queue_worker

    conn = get_connection()
    conn.execute("UPDATE system_config SET config_value = 'paused' WHERE config_key = 'governor.state';")
    conn.commit()
    conn.close()

    src.daemon._reflex_queue = asyncio.PriorityQueue()
    src.daemon._reflex_queue.put_nowait((-5, "evaluate_goals"))

    with patch.object(DynamicSkillExecutor, "execute", wraps=DynamicSkillExecutor.execute) as mock_execute:
        worker_task = asyncio.create_task(reflex_queue_worker())
        await asyncio.sleep(0.2)
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        assert not mock_execute.called

@pytest.mark.asyncio
@patch("src.memory.add_memory")
@patch("src.memory.query_memories")
@patch("src.skills.query_agent")
async def test_high_layer_loop_skips_dispatch_while_paused(
    mock_query, mock_query_memories, mock_add_memory, tmp_path, monkeypatch
):
    """While governor.state='paused', the high-level loop must skip all four of its
    tasks but still keep the heartbeat timestamp fresh. Exercises run_high_layer_loop()
    directly (not the full run_heartbeat_loop, which resets governor.state='running'
    on startup and would immediately clobber a pre-set 'paused' state)."""
    from src.daemon import run_high_layer_loop

    mock_query_memories.return_value = []
    mock_add_memory.return_value = None
    mock_query.return_value = ""

    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)

    conn = get_connection()
    conn.execute("UPDATE system_config SET config_value = 'paused' WHERE config_key = 'governor.state';")
    conn.commit()
    conn.close()

    with patch.object(DynamicSkillExecutor, "execute", wraps=DynamicSkillExecutor.execute) as mock_execute:
        loop_task = asyncio.create_task(run_high_layer_loop())
        await asyncio.sleep(2.5)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        high_skill_ids = {"decay_self_model", "consolidate_memories", "evaluate_goals", "cleanup_episodic_memory"}
        called_ids = {c[0][0] for c in mock_execute.call_args_list}
        assert not (high_skill_ids & called_ids)

    conn = get_connection(read_only_constitution=True)
    last_run = conn.execute("SELECT last_run_at FROM cognitive_layers WHERE layer_name = 'high';").fetchone()
    conn.close()
    assert last_run[0] is not None

@pytest.mark.asyncio
@patch("src.daemon.run_interval_skills")
@patch("src.memory.add_memory")
@patch("src.memory.query_memories")
@patch("src.skills.query_agent")
async def test_mid_layer_loop_explicit_pause_guard_skips_dispatch(
    mock_query, mock_query_memories, mock_add_memory, mock_interval_skills,
    tmp_path, monkeypatch
):
    """While governor.state='paused' (set directly, independent of the stagnation
    path), the mid-level loop must skip interval skills and reflection-trigger
    dispatch, proving the guard is explicit rather than an accidental side effect
    of the blocking pause_until_user_active() await. Exercises run_mid_layer_loop()
    directly for the same startup-reset reason as the high-layer test above."""
    import src.daemon
    from src.daemon import run_mid_layer_loop

    mock_query_memories.return_value = []
    mock_add_memory.return_value = None
    mock_query.return_value = ""

    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)

    conn = get_connection()
    conn.execute("UPDATE system_config SET config_value = 'paused' WHERE config_key = 'governor.state';")
    conn.commit()
    conn.close()

    src.daemon._pending_swarm_triggers.clear()
    src.daemon._pending_swarm_triggers.append("reflection")

    with patch.object(DynamicSkillExecutor, "execute", wraps=DynamicSkillExecutor.execute) as mock_execute:
        loop_task = asyncio.create_task(run_mid_layer_loop())
        await asyncio.sleep(2.5)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        assert not mock_interval_skills.called
        reflection_calls = [c for c in mock_execute.call_args_list if c[0][0] == "run_reflection_cycle"]
        assert reflection_calls == []

    assert "reflection" in src.daemon._pending_swarm_triggers

    conn = get_connection(read_only_constitution=True)
    last_run = conn.execute("SELECT last_run_at FROM cognitive_layers WHERE layer_name = 'mid';").fetchone()
    conn.close()
    assert last_run[0] is not None


@pytest.mark.asyncio
@patch("src.daemon.run_interval_skills")
@patch("src.memory.add_memory")
@patch("src.memory.query_memories")
@patch("src.skills.query_agent")
async def test_mid_layer_loop_increments_daemon_cycles_total(
    mock_query, mock_query_memories, mock_add_memory, mock_interval_skills,
    tmp_path, monkeypatch
):
    """Every mid-tick — even while the Smart Loop Governor is paused, per the
    existing 'always runs' comment on the cognitive_layers update — must
    increment metrics.daemon_cycles_total (issue #63)."""
    import src.daemon
    from src.daemon import run_mid_layer_loop
    from src.metrics import _get_counter

    mock_query_memories.return_value = []
    mock_add_memory.return_value = None
    mock_query.return_value = ""

    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)

    before = _get_counter("metrics.daemon_cycles_total")

    loop_task = asyncio.create_task(run_mid_layer_loop())
    await asyncio.sleep(2.5)
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    assert _get_counter("metrics.daemon_cycles_total") > before
