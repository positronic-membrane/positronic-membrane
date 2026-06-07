import os
import shutil
import pytest
import time
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import src.config
from src.database import init_db, log_episodic_memory
from src.sandbox_session import DockerSandboxExecutor
from src.memory import compress_episodic_memory
from src.self_modification import apply_staged_change

@pytest.fixture(autouse=True)
def setup_isolated_db(tmp_path):
    """Isolate DB settings for testing prerequisites."""
    temp_db = tmp_path / "test_janus_prereq.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

def test_docker_sandbox_executor_network_none():
    """Verify DockerSandboxExecutor passes the configured --network parameter."""
    executor = DockerSandboxExecutor()
    
    # Temporarily override DOCKER_NETWORK config value
    orig_net = getattr(src.config, "DOCKER_NETWORK", "none")
    src.config.DOCKER_NETWORK = "test_isolated_net"
    
    with patch("shutil.which", return_value="/usr/local/bin/docker"), \
         patch("subprocess.run") as mock_run:
        
        mock_run.return_value = MagicMock(returncode=0, stdout="success", stderr="")
        
        executor.run_tests("/mock/sandbox/root", 30, {})
        
        # Verify subprocess.run command includes --network test_isolated_net
        called_args = mock_run.call_args[0][0]
        assert "--network" in called_args
        assert "test_isolated_net" in called_args
        
    src.config.DOCKER_NETWORK = orig_net

@patch("src.memory.query_agent")
@patch("src.memory.add_memory")
def test_compress_episodic_memory_trigger(mock_add_memory, mock_query_agent):
    """Verify compress_episodic_memory triggers, processes LLM summary, and deletes rows."""
    mock_query_agent.return_value = "Synthesized primary concept summary."
    
    # 1. Seed 15 episodic memories
    for i in range(15):
        log_episodic_memory(
            speaker="persona" if i % 2 == 0 else "user",
            message_content=f"Dummy message content {i}",
            context_type="background_thought" if i % 3 == 0 else "user_visible"
        )
        
    # Verify we seeded 15 items
    from src.database import get_connection
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM episodic_memory;").fetchone()[0]
    assert count == 15
    conn.close()
    
    # 2. Trigger compression with limit 10 and keeping recent 3.
    # This should compress 15 - 3 = 12 items.
    compress_episodic_memory(limit=10, keep_recent=3)
    
    # 3. Verify database count is now 3
    conn = get_connection()
    new_count = conn.execute("SELECT COUNT(*) FROM episodic_memory;").fetchone()[0]
    assert new_count == 3
    conn.close()
    
    # 4. Verify vector DB add_memory was called to store synthesized concept
    mock_add_memory.assert_called_once()
    stored_concept = mock_add_memory.call_args[0][0]
    assert stored_concept == "Synthesized primary concept summary."
    
    # 5. Verify query_agent was called for the archivist role
    mock_query_agent.assert_called_once()
    assert mock_query_agent.call_args[0][0] == "archivist"
    prompt_sent = mock_query_agent.call_args[0][1]
    assert "Dummy message content 0" in prompt_sent
    assert "Dummy message content 11" in prompt_sent
    # Message 12, 13, 14 should not be in the prompt because they are the 3 kept recent ones
    assert "Dummy message content 12" not in prompt_sent

@patch("src.self_modification.subprocess.run")
@patch("urllib.request.urlopen")
def test_github_pr_gating_flow(mock_urlopen, mock_sub_run, tmp_path):
    """Verify that staged changes push branches and open PRs if GITHUB_ENABLED is True."""
    # 1. Enable GitHub gating in configuration
    orig_enabled = getattr(src.config, "GITHUB_ENABLED", False)
    orig_token = getattr(src.config, "GITHUB_ACCESS_TOKEN", "")
    orig_repo = getattr(src.config, "GITHUB_REPO", "")
    orig_root = src.config.ROOT_DIR
    
    src.config.GITHUB_ENABLED = True
    src.config.GITHUB_ACCESS_TOKEN = "mock_token"
    src.config.GITHUB_REPO = "mock_owner/mock_repo"
    src.config.ROOT_DIR = tmp_path / "mock_project_root"
    src.config.ROOT_DIR.mkdir()
    
    # Mock branch returns "main"
    def mock_sub_run_side_effect(cmd, **kwargs):
        res = MagicMock()
        if "rev-parse" in cmd:
            res.returncode = 0
            res.stdout = "main\n"
        elif "cat-file" in cmd:
            # File exists in base branch
            res.returncode = 0
        else:
            res.returncode = 0
            res.stdout = ""
            res.stderr = ""
        return res
    mock_sub_run.side_effect = mock_sub_run_side_effect
    
    # Mock urlopen return value
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"html_url": "https://github.com/mock_owner/mock_repo/pull/123"}'
    mock_urlopen.return_value.__enter__.return_value = mock_resp
    
    # Create mock staged directory and file
    temp_stage = tmp_path / "stage_dir"
    temp_stage.mkdir()
    staged_file = temp_stage / "src" / "utils.py"
    staged_file.parent.mkdir(parents=True, exist_ok=True)
    staged_file.write_text("def new_func(): pass\n")
    
    # Call apply_staged_change
    apply_staged_change(str(temp_stage), "src/utils.py")
    
    # Verify:
    # 1. checkout branch commands called
    called_commands = [call[0][0] for call in mock_sub_run.call_args_list]
    
    checkout_b_called = any("checkout" in cmd and "-b" in cmd for cmd in called_commands)
    assert checkout_b_called
    
    # 2. push command called with correct token URL
    push_called = False
    for cmd in called_commands:
        if "push" in cmd:
            push_called = True
            assert "https://x-access-token:mock_token@github.com/mock_owner/mock_repo.git" in cmd
    assert push_called
    
    # 3. urllib POST request to pulls API issued
    mock_urlopen.assert_called_once()
    called_req = mock_urlopen.call_args[0][0]
    assert called_req.full_url == "https://api.github.com/repos/mock_owner/mock_repo/pulls"
    assert called_req.get_header("Authorization") == "token mock_token"
    
    data_sent = json.loads(called_req.data.decode("utf-8"))
    assert "main" == data_sent["base"]
    assert "Janus Self-Modification: updates to src/utils.py" == data_sent["title"]
    
    # 4. Cleanup/restore config
    src.config.GITHUB_ENABLED = orig_enabled
    src.config.GITHUB_ACCESS_TOKEN = orig_token
    src.config.GITHUB_REPO = orig_repo
    src.config.ROOT_DIR = orig_root
