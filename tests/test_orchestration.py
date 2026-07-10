from unittest.mock import MagicMock, patch

import pytest

import src.config
from src.database import get_connection, init_db, save_sandbox_session
from src.security import decrypt_api_key, encrypt_api_key
from src.skills import SafeAgentOrchestration


def _insert_party(conn, party_id: str, role: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO parties (id, name, role) VALUES (?, ?, ?)",
        (party_id, f"Test {party_id}", role),
    )
    conn.commit()


def _insert_dispatch_row(conn, dispatch_id: int, status: str, sandbox_session_id: str, agent_id=None) -> None:
    conn.execute(
        "INSERT INTO dispatch_log (id, agent_id, task_description, status, sandbox_session_id) "
        "VALUES (?, ?, 'task description', ?, ?);",
        (dispatch_id, agent_id, status, sandbox_session_id),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus_orchestration.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

def test_security_encryption_roundtrip(monkeypatch):
    """Verify symmetric encryption and decryption round-trip works correctly."""
    monkeypatch.setenv("JANUS_ENCRYPTION_KEY", "super-secret-key-123")
    secret = "sk-proj-12345abcdef"
    enc = encrypt_api_key(secret)
    assert enc != secret
    assert decrypt_api_key(enc) == secret

    monkeypatch.delenv("JANUS_ENCRYPTION_KEY", raising=False)
    enc_fallback = encrypt_api_key(secret)
    assert decrypt_api_key(enc_fallback) == secret

def test_agent_registration():
    """Verify external agents are correctly registered and API keys are stored in encrypted format."""
    sao = SafeAgentOrchestration()

    # Register an API agent
    aid = sao.register_agent("test-api-agent", "api", "https://api.openai.com/v1", "my-api-key", ["python", "tests"])
    assert aid is not None

    agents = sao.get_agents()
    assert len(agents) >= 1
    api_agent = next(a for a in agents if a["name"] == "test-api-agent")
    assert api_agent["type"] == "api"
    assert api_agent["endpoint"] == "https://api.openai.com/v1"
    assert api_agent["capabilities"] == ["python", "tests"]

    conn = get_connection()
    row = conn.execute("SELECT api_key_encrypted FROM external_agents WHERE name = 'test-api-agent';").fetchone()
    conn.close()
    assert row[0] != "my-api-key"

    # Register a CLI agent
    aid_cli = sao.register_agent("test-cli-agent", "cli", "aider --batch", "", ["refactoring"])
    assert aid_cli is not None

    agents = sao.get_agents()
    cli_agent = next(a for a in agents if a["name"] == "test-cli-agent")
    assert cli_agent["type"] == "cli"
    assert cli_agent["endpoint"] == "aider --batch"
    assert cli_agent["capabilities"] == ["refactoring"]

@patch("src.skills.SafeAgentOrchestration.get_all_dispatches")
@patch("src.skills.SafeAgentOrchestration.get_dispatch_status")
@patch("src.skills.SafeAgentOrchestration.review_dispatch")
@patch("src.skills.SafeAgentOrchestration.dispatch_task")
def test_slash_commands(mock_dispatch, mock_review, mock_get_status, mock_get_all):
    """Verify that slash commands parse and invoke correct SDK methods."""
    from src.persona import handle_agent_command, handle_dispatch_command

    # Register an agent first
    res_reg = handle_agent_command("/agent register coder-cli cli AiderCommand sk-key [\"refactor\"]")
    assert "successfully registered" in res_reg

    res_list = handle_agent_command("/agent list")
    assert "External Coder Agents" in res_list

    mock_get_all.return_value = [{
        "id": 1,
        "agent_name": "coder-cli",
        "task_description": "Write a pytest file",
        "status": "success",
        "sandbox_session_id": "dispatch_1"
    }]
    res_disp_list = handle_dispatch_command("/dispatch list")
    assert "External Agent Task Dispatch Log" in res_disp_list

    mock_dispatch.return_value = 1
    mock_get_status.return_value = {"status": "success", "sandbox_session_id": "dispatch_1"}
    res_run = handle_dispatch_command("/dispatch coder-cli Write a pytest file [src/test.py]")
    mock_dispatch.assert_called_once_with("coder-cli", "Write a pytest file", ["src/test.py"])
    assert "completed with status 'success'" in res_run

    mock_review.return_value = True
    res_rev = handle_dispatch_command("/dispatch review 1 approve")
    mock_review.assert_called_once_with(1, approve=True)
    assert "successfully merged and shipped" in res_rev

@pytest.mark.asyncio
@patch("src.skills.OpenAI")
@patch("src.skills.create_sandbox_session")
@patch("src.skills.run_sandbox_tests")
@patch("src.skills.apply_changes_to_sandbox")
async def test_api_dispatch_task(mock_apply, mock_run_tests, mock_create_sandbox, mock_openai_class, tmp_path):
    """Verify stateless API dispatch spawns sandbox, applies diff blocks, and runs tests."""
    sao = SafeAgentOrchestration()
    sao.register_agent("coder-api", "api", "https://api.mock.net", "secret-key", ["python"])

    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    src_dir = sandbox_dir / "src"
    src_dir.mkdir()
    main_file = src_dir / "main.py"
    main_file.write_text("def old(): pass\n", encoding="utf-8")

    mock_create_sandbox.return_value = (str(sandbox_dir), "janus/sandbox-dispatch_1")

    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client

    mock_completion = MagicMock()
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = (
        "FILE: src/main.py\n"
        "<<<<<<< SEARCH\n"
        "def old(): pass\n"
        "=======\n"
        "def new(): pass\n"
        ">>>>>>> REPLACE\n"
    )
    mock_choice.message = mock_message
    mock_completion.choices = [mock_choice]
    mock_client.chat.completions.create.return_value = mock_completion

    mock_run_tests.return_value = (True, "All tests passed.")

    did = sao.dispatch_task("coder-api", "Optimize main", ["src/main.py"])

    mock_create_sandbox.assert_called_once()
    mock_apply.assert_called_once()
    assert "src/main.py" in mock_apply.call_args[0][0]

    status = sao.get_dispatch_status(did)
    assert status["status"] == "success"
    assert status["sandbox_session_id"] == "dispatch_1"

@pytest.mark.asyncio
@patch("src.skills.subprocess.run")
@patch("src.skills.create_sandbox_session")
@patch("src.skills.run_sandbox_tests")
async def test_cli_dispatch_task(mock_run_tests, mock_create_sandbox, mock_sub_run, tmp_path):
    """Verify stateful CLI dispatch executes the command in the sandbox workspace directory."""
    sao = SafeAgentOrchestration()
    sao.register_agent("coder-cli", "cli", "aider --batch", "", ["refactor"])

    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    mock_create_sandbox.return_value = (str(sandbox_dir), "janus/sandbox-dispatch_1")

    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "Applied changes."
    mock_res.stderr = ""
    mock_sub_run.return_value = mock_res

    mock_run_tests.return_value = (True, "All tests passed.")

    did = sao.dispatch_task("coder-cli", "Refactor module X")

    mock_sub_run.assert_called_once()
    cmd_run = mock_sub_run.call_args[0][0]
    assert cmd_run == ["aider", "--batch", "--message", "Refactor module X"]
    assert mock_sub_run.call_args[1]["cwd"] == str(sandbox_dir)

    status = sao.get_dispatch_status(did)
    assert status["status"] == "success"

@patch("src.skills.ship_sandbox_session")
@patch("src.skills.abort_sandbox_session")
def test_dispatch_review(mock_abort, mock_ship):
    """Verify review_dispatch updates DB and commits/discards sandbox session appropriately."""
    conn = get_connection()
    _insert_party(conn, "admin_dispatch", "admin")
    conn.close()

    sao = SafeAgentOrchestration(party_id="admin_dispatch")
    aid = sao.register_agent("coder-api", "api", "https://api.mock.net", "secret-key", [])

    conn = get_connection()
    _insert_dispatch_row(conn, 1, "success", "dispatch_1", agent_id=aid)
    _insert_dispatch_row(conn, 2, "failed", "dispatch_2", agent_id=aid)
    conn.close()

    save_sandbox_session("/fake/sandbox_1", "evolution/sandbox-dispatch_1", "active", session_name="dispatch_1")
    success = sao.review_dispatch(1, approve=True)
    assert success is True
    mock_ship.assert_called_once()

    status1 = sao.get_dispatch_status(1)
    assert status1["status"] == "reviewed"

    # ship_sandbox_session is mocked, so the real clear_sandbox_session() never ran —
    # re-point the active session at dispatch 2's before reviewing it.
    save_sandbox_session("/fake/sandbox_2", "evolution/sandbox-dispatch_2", "active", session_name="dispatch_2")
    success2 = sao.review_dispatch(2, approve=False)
    assert success2 is True
    mock_abort.assert_called_once()

    status2 = sao.get_dispatch_status(2)
    assert status2["status"] == "failed"


# ---------------------------------------------------------------------------
# review_dispatch — role gate, system carve-out, and session-identity checks (#95)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["observer", "user", "contributor"])
def test_review_dispatch_approve_blocked_for_non_admin_roles(role):
    conn = get_connection()
    _insert_party(conn, f"p_{role}", role)
    _insert_dispatch_row(conn, 1, "success", "dispatch_1")
    conn.close()

    sao = SafeAgentOrchestration(party_id=f"p_{role}")
    with pytest.raises(PermissionError, match="admin"):
        sao.review_dispatch(1, approve=True)


def test_review_dispatch_approve_blocked_for_none_party():
    conn = get_connection()
    _insert_dispatch_row(conn, 1, "success", "dispatch_1")
    conn.close()

    sao = SafeAgentOrchestration(party_id=None)
    with pytest.raises(PermissionError, match="admin"):
        sao.review_dispatch(1, approve=True)


def test_review_dispatch_approve_blocked_for_system_party():
    conn = get_connection()
    _insert_dispatch_row(conn, 1, "success", "dispatch_1")
    conn.close()

    sao = SafeAgentOrchestration(party_id="system")
    with pytest.raises(PermissionError, match="System"):
        sao.review_dispatch(1, approve=True)


@patch("src.skills.ship_sandbox_session")
def test_review_dispatch_approve_allowed_for_admin_with_matching_session(mock_ship):
    conn = get_connection()
    _insert_party(conn, "admin_review", "admin")
    _insert_dispatch_row(conn, 1, "success", "dispatch_1")
    conn.close()

    save_sandbox_session("/fake/sandbox_1", "evolution/sandbox-dispatch_1", "active", session_name="dispatch_1")

    sao = SafeAgentOrchestration(party_id="admin_review")
    assert sao.review_dispatch(1, approve=True) is True
    mock_ship.assert_called_once()


@patch("src.skills.abort_sandbox_session")
def test_review_dispatch_reject_ungated_for_non_admin(mock_abort):
    conn = get_connection()
    _insert_party(conn, "contributor_reject", "contributor")
    _insert_dispatch_row(conn, 1, "failed", "dispatch_1")
    conn.close()

    save_sandbox_session("/fake/sandbox_1", "evolution/sandbox-dispatch_1", "active", session_name="dispatch_1")

    sao = SafeAgentOrchestration(party_id="contributor_reject")
    assert sao.review_dispatch(1, approve=False) is True
    mock_abort.assert_called_once()


@patch("src.skills.ship_sandbox_session")
def test_review_dispatch_refuses_session_mismatch_on_approve(mock_ship):
    conn = get_connection()
    _insert_party(conn, "admin_mismatch", "admin")
    _insert_dispatch_row(conn, 1, "success", "dispatch_1")
    conn.close()

    # Active session belongs to a different dispatch.
    save_sandbox_session("/fake/sandbox_2", "evolution/sandbox-dispatch_2", "active", session_name="dispatch_2")

    sao = SafeAgentOrchestration(party_id="admin_mismatch")
    with pytest.raises(RuntimeError, match="does not match"):
        sao.review_dispatch(1, approve=True)
    mock_ship.assert_not_called()


@patch("src.skills.abort_sandbox_session")
def test_review_dispatch_refuses_session_mismatch_on_reject(mock_abort):
    conn = get_connection()
    _insert_party(conn, "contributor_mismatch", "contributor")
    _insert_dispatch_row(conn, 1, "failed", "dispatch_1")
    conn.close()

    save_sandbox_session("/fake/sandbox_2", "evolution/sandbox-dispatch_2", "active", session_name="dispatch_2")

    sao = SafeAgentOrchestration(party_id="contributor_mismatch")
    with pytest.raises(RuntimeError, match="does not match"):
        sao.review_dispatch(1, approve=False)
    mock_abort.assert_not_called()


@pytest.mark.parametrize("approve", [True, False])
def test_review_dispatch_refuses_when_no_active_session(approve):
    conn = get_connection()
    _insert_party(conn, "admin_no_session", "admin")
    _insert_dispatch_row(conn, 1, "success" if approve else "failed", "dispatch_1")
    conn.close()

    sao = SafeAgentOrchestration(party_id="admin_no_session")
    with pytest.raises(RuntimeError, match="does not match"):
        sao.review_dispatch(1, approve=approve)


def test_no_seeded_skill_reaches_merge_pr_or_review_dispatch():
    """
    Pins that merge_pr/review_dispatch stay unreachable via party_id='system'
    skill-execution paths (interval/reflex/swarm) until a future change
    deliberately re-derives this trust boundary (see #95).
    """
    conn = get_connection()
    try:
        rows = conn.execute("SELECT skill_id, code_blob FROM agent_skills;").fetchall()
    finally:
        conn.close()
    for row in rows:
        skill_id, code_blob = row[0], row[1]
        assert "merge_pr(" not in code_blob, f"skill '{skill_id}' calls merge_pr"
        assert "review_dispatch(" not in code_blob, f"skill '{skill_id}' calls review_dispatch"


# ---------------------------------------------------------------------------
# handle_dispatch_command "review" — command-level gate (#95)
# ---------------------------------------------------------------------------

@patch("src.persona.get_session_party_id", return_value="contributor_cmd")
@patch("src.skills.SafeAgentOrchestration.review_dispatch")
def test_dispatch_review_command_blocked_for_contributor_role(mock_review, mock_party):
    from src.persona import handle_dispatch_command

    conn = get_connection()
    _insert_party(conn, "contributor_cmd", "contributor")
    conn.close()

    result = handle_dispatch_command("/dispatch review 1 approve")
    assert "[Error]" in result
    assert "admin" in result
    mock_review.assert_not_called()


@patch("src.persona.get_session_party_id", return_value="admin_cmd")
@patch("src.skills.SafeAgentOrchestration.review_dispatch")
def test_dispatch_review_command_allowed_for_admin(mock_review, mock_party):
    from src.persona import handle_dispatch_command

    conn = get_connection()
    _insert_party(conn, "admin_cmd", "admin")
    conn.close()

    mock_review.return_value = True
    result = handle_dispatch_command("/dispatch review 1 approve")
    assert "successfully merged and shipped" in result
    mock_review.assert_called_once_with(1, approve=True)


@patch("src.persona.get_session_party_id", return_value="contributor_cmd2")
@patch("src.skills.SafeAgentOrchestration.review_dispatch")
def test_dispatch_review_command_reject_ungated_for_contributor(mock_review, mock_party):
    from src.persona import handle_dispatch_command

    conn = get_connection()
    _insert_party(conn, "contributor_cmd2", "contributor")
    conn.close()

    mock_review.return_value = True
    result = handle_dispatch_command("/dispatch review 1 reject")
    assert "successfully aborted and discarded" in result
    mock_review.assert_called_once_with(1, approve=False)


@patch("src.persona.get_session_party_id", return_value="admin_cmd3")
@patch("src.skills.SafeAgentOrchestration.review_dispatch")
def test_dispatch_review_command_returns_error_string_on_permission_error(mock_review, mock_party):
    from src.persona import handle_dispatch_command

    conn = get_connection()
    _insert_party(conn, "admin_cmd3", "admin")
    conn.close()

    mock_review.side_effect = PermissionError("nope")
    result = handle_dispatch_command("/dispatch review 1 approve")
    assert result == "[Error] nope"


@patch("src.persona.get_session_party_id", return_value="admin_cmd4")
@patch("src.skills.SafeAgentOrchestration.review_dispatch")
def test_dispatch_review_command_returns_error_string_on_runtime_error(mock_review, mock_party):
    from src.persona import handle_dispatch_command

    conn = get_connection()
    _insert_party(conn, "admin_cmd4", "admin")
    conn.close()

    mock_review.side_effect = RuntimeError("session mismatch")
    result = handle_dispatch_command("/dispatch review 1 approve")
    assert result == "[Error] session mismatch"
