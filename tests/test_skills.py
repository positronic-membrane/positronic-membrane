import json
from unittest.mock import patch

import pytest

import src.config
import src.memory
from src.database import get_connection, init_db
from src.skills import DynamicSkillExecutor, has_role


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB for testing."""
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()

    # Create test parties
    conn = get_connection(read_only_constitution=False)
    conn.execute("INSERT INTO parties (id, name, role, public_key) VALUES ('user1', 'Alice', 'user', 'key1');")
    conn.execute("INSERT INTO parties (id, name, role, public_key) VALUES ('contrib1', 'Bob', 'contributor', 'key2');")
    conn.execute("INSERT INTO parties (id, name, role, public_key) VALUES ('admin1', 'Charlie', 'admin', 'key3');")
    conn.commit()
    conn.close()

    yield
    src.config.DB_PATH = orig_db_path

@pytest.fixture(autouse=True)
def setup_test_vector_db(tmp_path):
    """Isolates the ChromaDB persistent directory."""
    orig_path = src.config.VECTOR_DB_PATH
    src.config.VECTOR_DB_PATH = str(tmp_path / "test_chromadb")
    src.memory._chroma_client = None
    src.memory._collections = {}
    yield
    src.config.VECTOR_DB_PATH = orig_path

@pytest.fixture
def mock_embeddings():
    """Mock OpenAI embeddings endpoint."""
    with patch("src.memory.get_embeddings") as mock_get:
        mock_get.return_value = [[0.1] * 384]
        yield mock_get

def test_has_role():
    """Verify that role hierarchies are checked correctly."""
    assert has_role('user1', 'observer')
    assert has_role('user1', 'user')
    assert not has_role('user1', 'contributor')
    assert not has_role('user1', 'admin')

    assert has_role('contrib1', 'user')
    assert has_role('contrib1', 'contributor')
    assert not has_role('contrib1', 'admin')

    assert has_role('admin1', 'admin')
    assert has_role(None, 'observer')
    assert not has_role(None, 'user')
    assert has_role('system', 'admin')

def test_executor_basic_execution():
    """Verify that a registered dynamic skill executes successfully and isolates namespaces."""
    conn = get_connection(read_only_constitution=False)
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES (
        'test_add', 'Test Add', 'Adds two numbers', '{}',
        'def add_nums(x, y):\n    return x + y', 'add_nums', 'user'
    );
    """)
    conn.commit()
    conn.close()

    res = DynamicSkillExecutor.execute('test_add', {'x': 10, 'y': 20}, party_id='contrib1')
    assert res['success']
    assert res['result'] == 30

def test_executor_permission_veto():
    """Verify that role restrictions veto unauthorized executions."""
    conn = get_connection(read_only_constitution=False)
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES (
        'test_admin_only', 'Admin Only', 'Private skill', '{}',
        'def run():\n    return "secret"', 'run', 'admin'
    );
    """)
    conn.commit()
    conn.close()

    res = DynamicSkillExecutor.execute('test_admin_only', {}, party_id='user1')
    assert not res['success']
    assert 'Security Veto' in res['error']

    res = DynamicSkillExecutor.execute('test_admin_only', {}, party_id='admin1')
    assert res['success']
    assert res['result'] == 'secret'

def test_executor_traceback_mapping():
    """Verify that custom traceback line offset mapping works for dynamic skill errors."""
    conn = get_connection(read_only_constitution=False)
    code = (
        "def fail_skill():\n"
        "    x = 1\n"
        "    y = x / 0\n"
        "    return y"
    )
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES ('test_fail', 'Fail Skill', 'Throws division error', '{}', ?, 'fail_skill', 'user');
    """, (code,))
    conn.commit()
    conn.close()

    res = DynamicSkillExecutor.execute('test_fail', {}, party_id='user1')
    assert not res['success']
    assert "ZeroDivisionError" in res['error']
    assert "File <dynamic_skill>, line 3, in fail_skill" in res['error']
    assert "y = x / 0" in res['error']

def test_sdk_database_authorizer():
    """Verify that SafeDB.query honors read-only core_constitution authorizer."""
    conn = get_connection(read_only_constitution=False)
    code = "def write():\n    sdk['db'].query(\"DELETE FROM core_constitution WHERE rule_key = 'xxx'\")\n    return 'ok'"
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES ('db_write_test', 'DB Write Test', 'Tries to edit constitution', '{}', ?, 'write', 'contributor');
    """, (code,))
    conn.commit()
    conn.close()

    res = DynamicSkillExecutor.execute('db_write_test', {}, party_id='contrib1')
    assert not res['success']
    err_lower = res['error'].lower()
    assert any(term in err_lower for term in ("databaseaccessexception", "deny", "authorizer", "safety", "violation"))

def test_sdk_memory_isolation(mock_embeddings):
    """Verify that SafeMemory.add inserts party_id into metadata when party_id is scoped."""
    conn = get_connection(read_only_constitution=False)
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES (
        'mem_add_test', 'Memory Add Test', 'Adds memory scoped by party', '{}',
        'def run():\n    sdk["memory"].add("scoped memory content", {"tag": "test"}, "scoped_1")\n    return "done"',
        'run', 'contributor'
    );
    """)
    conn.commit()
    conn.close()

    # Bob (contrib1) running it
    res = DynamicSkillExecutor.execute('mem_add_test', {}, party_id='contrib1')
    assert res['success']

    from src.memory import get_collection
    col = get_collection("janus_long_term")
    mem_records = col.get(ids=["scoped_1"])
    assert mem_records["metadatas"][0]["party_id"] == "contrib1"


def test_sdk_decoupled_wrappers():
    """Verify that SafeExplorer, SafeCodebase, and SafeSandbox execute and forward calls correctly."""
    conn = get_connection(read_only_constitution=False)
    code = (
        "def run():\n"
        "    r_search = sdk['explorer'].search('hello')\n"
        "    r_fetch = sdk['explorer'].fetch('http://test.com')\n"
        "    r_query = sdk['codebase'].query('symbol')\n"
        "    r_scan = sdk['codebase'].scan()\n"
        "    r_exec = sdk['sandbox'].execute('print(1)')\n"
        "    return {'search': r_search, 'fetch': r_fetch, 'query': r_query, 'scan': r_scan, 'exec': r_exec}"
    )
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES ('sdk_decouple_test', 'Decouple Test', 'Verifies decoupled SDK objects', '{}', ?, 'run', 'contributor');
    """, (code,))
    conn.commit()
    conn.close()

    with patch("src.skills.search_web") as mock_search, \
         patch("src.skills.fetch_webpage") as mock_fetch, \
         patch("src.skills.query_codebase_context") as mock_query, \
         patch("src.skills.index_codebase") as mock_scan, \
         patch("src.skills.execute_code_safely") as mock_exec:

        mock_search.return_value = [{"title": "t", "url": "u", "snippet": "s"}]
        mock_fetch.return_value = "parsed webpage content"
        mock_query.return_value = "codebase query result"
        mock_scan.return_value = None
        mock_exec.return_value = "exec output"

        res = DynamicSkillExecutor.execute('sdk_decouple_test', {}, party_id='contrib1')
        assert res['success']
        assert res['result'] == {
            "search": [{"title": "t", "url": "u", "snippet": "s"}],
            "fetch": "parsed webpage content",
            "query": "codebase query result",
            "scan": "Codebase successfully scanned and indexed.",
            "exec": "exec output"
        }

        mock_search.assert_called_once_with("hello")
        mock_fetch.assert_called_once_with("http://test.com")
        mock_query.assert_called_once_with("symbol")
        mock_scan.assert_called_once()
        mock_exec.assert_called_once_with("print(1)")


@patch("src.sandbox_session.create_sandbox_session")
@patch("src.sandbox_session.run_sandbox_tests")
@patch("src.sandbox_session.ship_sandbox_session")
@patch("src.sandbox_session.abort_sandbox_session")
def test_manage_sandbox_skill(mock_abort, mock_ship, mock_test, mock_create):
    """Verify that the manage_sandbox dynamic skill executes all git workspace sandbox operations successfully."""
    # First, make sure the skill is in the DB for this run
    conn = get_connection(read_only_constitution=False)
    conn.execute("DELETE FROM agent_skills WHERE skill_id = 'manage_sandbox';")

    schema = json.dumps({
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "session_name": {"type": "string"}
        },
        "required": ["action"]
    })
    code = (
        'def manage_sandbox(action, session_name=None):\n'
        '    from src.sandbox_session import create_sandbox_session, run_sandbox_tests, ship_sandbox_session, abort_sandbox_session\n'
        '    if action == "start":\n'
        '        if not session_name:\n'
        '            raise ValueError("session_name is required to start a sandbox.")\n'
        '        path, branch = create_sandbox_session(session_name)\n'
        '        return f"Sandbox spawned successfully at: {path} (Branch: {branch})"\n'
        '    elif action == "test":\n'
        '        passed, logs = run_sandbox_tests()\n'
        '        status = "PASSED" if passed else "FAILED"\n'
        '        return f"Sandbox test suite run completed: {status}.\\nLogs:\\n{logs}"\n'
        '    elif action == "ship":\n'
        '        copied = ship_sandbox_session()\n'
        '        return f"Sandbox shipped and applied to active workspace. Files modified: {copied}"\n'
        '    elif action == "abort":\n'
        '        abort_sandbox_session()\n'
        '        return "Sandbox session aborted and discarded."\n'
        '    else:\n'
        '        raise ValueError(f"Unknown sandbox action: {action}")\n'
    )
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES ('manage_sandbox', 'Manage Sandbox', 'Controls sandboxes', ?, ?, 'manage_sandbox', 'contributor');
    """, (schema, code))
    conn.commit()
    conn.close()

    mock_create.return_value = ("/path/to/sb", "janus/sandbox-auto-test")
    mock_test.return_value = (True, "mocked unit test execution logs")
    mock_ship.return_value = ["src/config.py"]

    # Test "start"
    res = DynamicSkillExecutor.execute("manage_sandbox", {"action": "start", "session_name": "auto-test"}, party_id="contrib1")
    assert res["success"], res.get("error")
    assert "auto-test" in res["result"] or "sandbox-auto-test" in res["result"]
    mock_create.assert_called_once_with("auto-test")

    # Test "test"
    res = DynamicSkillExecutor.execute("manage_sandbox", {"action": "test"}, party_id="contrib1")
    assert res["success"], res.get("error")
    assert "mocked unit test execution logs" in res["result"]
    mock_test.assert_called_once()

    # Test "ship"
    res = DynamicSkillExecutor.execute("manage_sandbox", {"action": "ship"}, party_id="contrib1")
    assert res["success"], res.get("error")
    assert "src/config.py" in res["result"]
    mock_ship.assert_called_once()

    # Test "abort"
    res = DynamicSkillExecutor.execute("manage_sandbox", {"action": "abort"}, party_id="contrib1")
    assert res["success"], res.get("error")
    assert "aborted" in res["result"]
    mock_abort.assert_called_once()


# --- Consolidating from test_phase2_decoupling.py ---

def test_safe_drives_sdk():
    """Verify sdk['drives'] functions correctly query and update drive_state."""
    from src.skills import SafeDrives
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
    from src.skills import SafeDrives
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


def test_executor_increments_skills_executed_and_failed_totals():
    from src.metrics import _get_counter

    conn = get_connection(read_only_constitution=False)
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES (
        'test_metrics_ok', 'Test Metrics Ok', 'Succeeds', '{}',
        'def run():\n    return 1', 'run', 'user'
    );
    """)
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES (
        'test_metrics_fail', 'Test Metrics Fail', 'Fails', '{}',
        'def run():\n    raise ValueError("boom")', 'run', 'user'
    );
    """)
    conn.commit()
    conn.close()

    before_executed = _get_counter("metrics.skills_executed_total")
    before_failed = _get_counter("metrics.skills_failed_total")

    res = DynamicSkillExecutor.execute('test_metrics_ok', {}, party_id='user1')
    assert res['success']
    assert _get_counter("metrics.skills_executed_total") == before_executed + 1
    assert _get_counter("metrics.skills_failed_total") == before_failed

    res = DynamicSkillExecutor.execute('test_metrics_fail', {}, party_id='user1')
    assert not res['success']
    assert _get_counter("metrics.skills_executed_total") == before_executed + 2
    assert _get_counter("metrics.skills_failed_total") == before_failed + 1


def test_executor_veto_does_not_increment_skills_executed_total():
    """A role veto happens before dispatch, so it must not count as 'executed'."""
    from src.metrics import _get_counter

    conn = get_connection(read_only_constitution=False)
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES (
        'test_metrics_vetoed', 'Test Metrics Vetoed', 'Admin only', '{}',
        'def run():\n    return 1', 'run', 'admin'
    );
    """)
    conn.commit()
    conn.close()

    before_executed = _get_counter("metrics.skills_executed_total")
    res = DynamicSkillExecutor.execute('test_metrics_vetoed', {}, party_id='user1')
    assert not res['success']
    assert _get_counter("metrics.skills_executed_total") == before_executed


def test_safe_goals_complete_checkpoint_records_party_id():
    from src.skills import SafeGoals

    sg = SafeGoals(party_id=None)
    goal_id = sg.create_goal("short", "test goal")
    checkpoint_id = sg.add_checkpoint(goal_id, "test checkpoint")

    autonomous_sg = SafeGoals(party_id="system")
    assert autonomous_sg.complete_checkpoint(checkpoint_id)

    conn = get_connection(read_only_constitution=True)
    row = conn.execute(
        "SELECT achieved, completed_by_party_id FROM goal_checkpoints WHERE id = ?;", (checkpoint_id,)
    ).fetchone()
    conn.close()
    assert row[0] == 1
    assert row[1] == "system"


def test_safe_goals_complete_checkpoint_without_party_id_leaves_null():
    from src.skills import SafeGoals

    sg = SafeGoals()
    goal_id = sg.create_goal("short", "test goal 2")
    checkpoint_id = sg.add_checkpoint(goal_id, "test checkpoint 2")

    assert sg.complete_checkpoint(checkpoint_id)

    conn = get_connection(read_only_constitution=True)
    row = conn.execute(
        "SELECT completed_by_party_id FROM goal_checkpoints WHERE id = ?;", (checkpoint_id,)
    ).fetchone()
    conn.close()
    assert row[0] is None



# ---------------------------------------------------------------------------
# SafeFS secrets denylist (issue #147)
# ---------------------------------------------------------------------------

@pytest.fixture
def safefs_workspace(tmp_path):
    """Workspace containing the secret paths SafeFS must refuse to touch."""
    (tmp_path / ".keys").mkdir()
    (tmp_path / ".keys" / "jwt_private.pem").write_text("PRIVATE KEY")
    (tmp_path / ".env").write_text("NEO4J_PASSWORD=secret")
    (tmp_path / ".env.production").write_text("AWS_SECRET_ACCESS_KEY=secret")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("hello")
    with patch("src.config.get_effective_workspace_root", return_value=tmp_path):
        yield tmp_path


def test_safefs_blocks_env_and_keys_reads(safefs_workspace):
    from src.skills import SafeFS
    fs = SafeFS()
    for path in (".env", ".env.production", ".keys/jwt_private.pem"):
        with pytest.raises(PermissionError, match="protected secrets"):
            fs.read(path)


def test_safefs_blocks_traversal_spellings_of_env(safefs_workspace):
    from src.skills import SafeFS
    fs = SafeFS()
    for path in ("./.env", "docs/../.env", "docs/../../" + safefs_workspace.name + "/.env"):
        with pytest.raises(PermissionError):
            fs.read(path)


def test_safefs_blocks_secret_writes(safefs_workspace):
    from src.skills import SafeFS
    fs = SafeFS()
    with pytest.raises(PermissionError, match="protected secrets"):
        fs.write(".env", "LLM_BASE_URL=http://attacker")
    with pytest.raises(PermissionError, match="protected secrets"):
        fs.write(".keys/jwt_private.pem", "tampered")
    assert (safefs_workspace / ".env").read_text() == "NEO4J_PASSWORD=secret"


def test_safefs_exists_denies_secrets(safefs_workspace):
    from src.skills import SafeFS
    fs = SafeFS()
    assert fs.exists(".env") is False
    assert fs.exists(".keys/jwt_private.pem") is False


def test_safefs_blocks_sibling_root_prefix_escape(safefs_workspace):
    # str(root).startswith() would have accepted a sibling dir sharing the
    # root's name as a prefix; relative_to() must reject it.
    from src.skills import SafeFS
    sibling = safefs_workspace.parent / (safefs_workspace.name + "_evil")
    sibling.mkdir()
    (sibling / "loot.txt").write_text("outside")
    fs = SafeFS()
    with pytest.raises(PermissionError, match="outside the active workspace"):
        fs.read(str(sibling / "loot.txt"))


def test_safefs_normal_paths_unaffected(safefs_workspace):
    from src.skills import SafeFS
    fs = SafeFS()
    assert fs.read("docs/note.md") == "hello"
    fs.write("docs/new.md", "content")
    assert fs.exists("docs/new.md") is True
