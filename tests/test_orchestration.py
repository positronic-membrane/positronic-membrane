from unittest.mock import MagicMock, patch

import pytest

import src.config
from src.database import get_connection, init_db
from src.security import decrypt_api_key, encrypt_api_key
from src.skills import SafeAgentOrchestration


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
    sao = SafeAgentOrchestration()
    aid = sao.register_agent("coder-api", "api", "https://api.mock.net", "secret-key", [])

    conn = get_connection()
    conn.execute(
        "INSERT INTO dispatch_log (id, agent_id, task_description, status, sandbox_session_id) "
        "VALUES (1, ?, 'task description', 'success', 'dispatch_1');",
        (aid,)
    )
    conn.execute(
        "INSERT INTO dispatch_log (id, agent_id, task_description, status, sandbox_session_id) "
        "VALUES (2, ?, 'task description', 'failed', 'dispatch_2');",
        (aid,)
    )
    conn.commit()
    conn.close()

    success = sao.review_dispatch(1, approve=True)
    assert success is True
    mock_ship.assert_called_once()

    status1 = sao.get_dispatch_status(1)
    assert status1["status"] == "reviewed"

    success2 = sao.review_dispatch(2, approve=False)
    assert success2 is True
    mock_abort.assert_called_once()

    status2 = sao.get_dispatch_status(2)
    assert status2["status"] == "failed"
