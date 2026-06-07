import pytest
import re
from unittest.mock import patch, MagicMock
from pathlib import Path
import src.config
from src.database import init_db, log_episodic_memory, get_recent_episodic_memories
from src.persona import (
    execute_chat_sandbox_commands,
    generate_persona_response_autonomous
)

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

def test_get_recent_episodic_memories_filtering():
    """Verify that episodic memories can be filtered by context_type."""
    log_episodic_memory("user", "Hello visible", "user_visible")
    log_episodic_memory("sandbox_automation", "Hello invisible", "background_thought")
    
    # 1. Fetch only visible
    visible_memories = get_recent_episodic_memories(limit=10, context_type="user_visible")
    assert len(visible_memories) == 1
    assert visible_memories[0][0] == "user"
    assert visible_memories[0][1] == "Hello visible"
    
    # 2. Fetch only background
    background_memories = get_recent_episodic_memories(limit=10, context_type="background_thought")
    assert len(background_memories) == 1
    assert background_memories[0][0] == "sandbox_automation"
    assert background_memories[0][1] == "Hello invisible"
    
    # 3. Fetch all (default behavior)
    all_memories = get_recent_episodic_memories(limit=10)
    assert len(all_memories) == 2

@patch("src.config.get_effective_workspace_root")
@patch("src.sandbox_session.run_sandbox_tests")
@patch("src.sandbox_session.get_sandbox_diff")
@patch("subprocess.run")
@patch("src.sandbox_session.discard_sandbox_changes")
@patch("src.sandbox_session.rollback_sandbox_last_commit")
def test_execute_chat_sandbox_commands(mock_rollback, mock_discard, mock_run, mock_diff, mock_tests, mock_workspace, tmp_path):
    """Verify execution of individual sandbox commands."""
    mock_workspace.return_value = tmp_path
    mock_tests.return_value = (True, "All tests passed!")
    mock_diff.return_value = "diff --git a/file b/file"
    
    # Create a dummy file in workspace root
    dummy_file = tmp_path / "test_file.txt"
    dummy_file.write_text("Hello workspace!")
    
    # Test read command
    block = "read test_file.txt"
    res = execute_chat_sandbox_commands(block)
    assert "Hello workspace!" in res
    assert "test_file.txt" in res
    
    # Test read command with colon
    block = "read: test_file.txt"
    res = execute_chat_sandbox_commands(block)
    assert "Hello workspace!" in res
    
    # Test read nonexistent
    block = "read nonexistent.txt"
    res = execute_chat_sandbox_commands(block)
    assert "File not found" in res
    
    # Test directory traversal protection
    block = "read ../outside.txt"
    res = execute_chat_sandbox_commands(block)
    assert "Access denied" in res
    
    # Test checkout command
    mock_run_instance = MagicMock()
    mock_run_instance.returncode = 0
    mock_run.return_value = mock_run_instance
    block = "checkout test_file.txt"
    res = execute_chat_sandbox_commands(block)
    assert "Reverted successfully" in res
    mock_run.assert_called_with(["git", "checkout", "--", "test_file.txt"], cwd=tmp_path, capture_output=True, text=True)
    
    # Test checkout directory traversal protection
    block = "checkout ../outside.txt"
    res = execute_chat_sandbox_commands(block)
    assert "Access denied" in res
    
    # Test discard command
    mock_discard.return_value = True
    block = "discard"
    res = execute_chat_sandbox_commands(block)
    assert "discarded successfully" in res
    mock_discard.assert_called_once()
    
    # Test rollback command
    mock_rollback.return_value = True
    block = "rollback"
    res = execute_chat_sandbox_commands(block)
    assert "Rolled back the last commit" in res
    mock_rollback.assert_called_once()
    
    # Test run test command
    block = "test"
    res = execute_chat_sandbox_commands(block)
    assert "PASSED" in res
    assert "All tests passed!" in res
    
    # Test diff command
    block = "diff"
    res = execute_chat_sandbox_commands(block)
    assert "diff --git" in res
    
    # Test unknown command
    block = "unknown_cmd param"
    res = execute_chat_sandbox_commands(block)
    assert "Unknown sandbox command" in res

@patch("src.sandbox_session.get_active_sandbox")
@patch("src.persona.generate_persona_response")
def test_generate_persona_response_autonomous_no_sandbox(mock_gen, mock_get_sb):
    """Verify that when no sandbox is active, response is generated normally in 1 turn."""
    mock_get_sb.return_value = None
    mock_gen.return_value = "Hello normal user!"
    
    res = generate_persona_response_autonomous("Hi Janus")
    assert res == "Hello normal user!"
    mock_gen.assert_called_once_with("Hi Janus")

@patch("src.sandbox_session.get_active_sandbox")
@patch("src.persona.generate_persona_response")
def test_generate_persona_response_autonomous_no_commands(mock_gen, mock_get_sb):
    """Verify that with active sandbox but no sandbox commands, response exits in 1 turn."""
    mock_get_sb.return_value = {"active_sandbox_path": "/dummy", "active_sandbox_branch": "dummy-branch"}
    mock_gen.return_value = "Normal response without sandbox blocks."
    
    res = generate_persona_response_autonomous("Hi Janus")
    assert res == "Normal response without sandbox blocks."
    mock_gen.assert_called_once_with("Hi Janus")

@patch("src.sandbox_session.get_active_sandbox")
@patch("src.persona.generate_persona_response")
@patch("src.persona.execute_chat_sandbox_commands")
@patch("src.persona.parse_proposed_changes")
def test_generate_persona_response_autonomous_react_loop(mock_parse, mock_exec, mock_gen, mock_get_sb):
    """Verify ReAct loop when persona outputs sandbox commands."""
    mock_get_sb.return_value = {"active_sandbox_path": "/dummy", "active_sandbox_branch": "dummy-branch"}
    mock_parse.return_value = {} # No file writes
    
    # First query response: has sandbox commands
    resp1 = "Let me run tests first.\n```sandbox\ntest\n```"
    # Second query response: final answer
    resp2 = "All tests passed, everything is green!"
    
    mock_gen.side_effect = [resp1, resp2]
    mock_exec.return_value = "- test: PASSED\nLogs: Green!"
    
    res = generate_persona_response_autonomous("Run the test suite please")
    
    assert res == "All tests passed, everything is green!"
    # Assert generate_persona_response was called twice
    assert mock_gen.call_count == 2
    mock_gen.assert_any_call("Run the test suite please")
    mock_gen.assert_any_call("Executed requested actions/skills. Please review the background thought history and continue.")
    
    # Assert execution result was logged as a background thought in episodic memory
    mems = get_recent_episodic_memories(limit=10, context_type="background_thought")
    assert len(mems) == 1
    assert mems[0][0] == "sandbox_automation"
    assert "Logs: Green!" in mems[0][1]

@patch("src.sandbox_session.get_active_sandbox")
@patch("src.persona.generate_persona_response")
@patch("src.persona.execute_chat_sandbox_commands")
@patch("src.persona.parse_proposed_changes")
def test_generate_persona_response_autonomous_loop_limit(mock_parse, mock_exec, mock_gen, mock_get_sb):
    """Verify that loop cap is enforced (max 5 turns) to prevent runaway execution."""
    mock_get_sb.return_value = {"active_sandbox_path": "/dummy", "active_sandbox_branch": "dummy-branch"}
    mock_exec.return_value = "- test: PASSED"
    mock_parse.return_value = {}  # Mock parsing to return no file modifications
    
    # Persona always returns a sandbox command, causing an infinite loop if unchecked
    mock_gen.return_value = "Still checking...\n```sandbox\ntest\n```"
    
    res = generate_persona_response_autonomous("Keep running tests")
    
    # Assert it was run 5 times and exited
    assert mock_gen.call_count == 5
    assert res == "Still checking...\n```sandbox\ntest\n```"
