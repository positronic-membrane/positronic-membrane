import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import src.config
from src.sandbox_session import (
    sanitize_session_name,
    get_active_sandbox,
    create_sandbox_session,
    apply_changes_to_sandbox,
    run_sandbox_tests,
    get_sandbox_diff,
    get_sandbox_modified_files,
    ship_sandbox_session,
    abort_sandbox_session
)

def test_sanitize_session_name():
    """Verify session name is sanitized to a safe git branch string."""
    assert sanitize_session_name("feature/add-math!") == "feature_add-math_"
    assert sanitize_session_name("cool_stuff") == "cool_stuff"

@patch("src.sandbox_session.subprocess.run")
@patch("src.sandbox_session.save_sandbox_session")
def test_create_sandbox_session(mock_save, mock_run, tmp_path):
    """Verify create_sandbox_session spawns a git worktree and saves state."""
    # Setup mock config root dir
    orig_root = src.config.ROOT_DIR
    src.config.ROOT_DIR = tmp_path
    
    mock_run_instance = MagicMock()
    mock_run_instance.returncode = 0
    mock_run_instance.stderr = ""
    mock_run_instance.stdout = ""
    mock_run.return_value = mock_run_instance
    
    try:
        path, branch = create_sandbox_session("my-feature")
        
        # Verify git worktree add was called
        # Calls should have git worktree remove, git branch -D, and git worktree add
        assert mock_run.call_count >= 3
        worktree_add_args = mock_run.call_args_list[-1][0][0]
        assert "worktree" in worktree_add_args
        assert "add" in worktree_add_args
        assert "janus/sandbox-my-feature" in worktree_add_args
        
        # Verify database save was triggered
        mock_save.assert_called_once()
        args = mock_save.call_args[0]
        assert args[1] == "janus/sandbox-my-feature"
        assert args[2] == "active"
        
        assert "session_my-feature" in path
        assert branch == "janus/sandbox-my-feature"
    finally:
        src.config.ROOT_DIR = orig_root

@patch("src.sandbox_session.get_sandbox_session")
def test_apply_changes_to_sandbox(mock_get_session, tmp_path):
    """Verify proposed changes are written to the sandbox files."""
    sandbox_path = tmp_path / "sandbox_folder"
    sandbox_path.mkdir()
    
    mock_get_session.return_value = {
        "active_sandbox_path": str(sandbox_path),
        "active_sandbox_branch": "janus/sandbox-feat",
        "active_sandbox_status": "active"
    }
    
    modifications = {
        "src/new_helper.py": "def hello(): pass\n",
        "tests/test_new_helper.py": "def test_hello(): pass\n"
    }
    
    apply_changes_to_sandbox(modifications)
    
    # Assert files exist and match content
    file1 = sandbox_path / "src" / "new_helper.py"
    file2 = sandbox_path / "tests" / "test_new_helper.py"
    
    assert file1.exists()
    assert file1.read_text() == "def hello(): pass\n"
    
    assert file2.exists()
    assert file2.read_text() == "def test_hello(): pass\n"

@patch("src.sandbox_session.get_sandbox_session")
@patch("src.sandbox_session.subprocess.run")
@patch("src.sandbox_session.save_sandbox_session")
def test_run_sandbox_tests(mock_save, mock_run, mock_get_session, tmp_path):
    """Verify run_sandbox_tests executes pytest in sandbox directory."""
    sandbox_path = tmp_path / "sandbox_folder"
    sandbox_path.mkdir()
    
    mock_get_session.return_value = {
        "active_sandbox_path": str(sandbox_path),
        "active_sandbox_branch": "janus/sandbox-feat",
        "active_sandbox_status": "active"
    }
    
    # Mock successful pytest execution
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "All tests passed"
    mock_res.stderr = ""
    mock_run.return_value = mock_res
    
    passed, logs = run_sandbox_tests()
    
    assert passed is True
    assert "All tests passed" in logs
    
    # Verify DB was updated to "passed" with correct logs
    mock_save.assert_called_once_with(str(sandbox_path), "janus/sandbox-feat", "passed", test_logs="All tests passed\n")

@patch("src.sandbox_session.get_sandbox_session")
@patch("src.sandbox_session.subprocess.run")
@patch("src.sandbox_session.save_sandbox_session")
def test_run_sandbox_tests_timeout(mock_save, mock_run, mock_get_session, tmp_path):
    """Verify run_sandbox_tests handles subprocess.TimeoutExpired correctly."""
    sandbox_path = tmp_path / "sandbox_folder"
    sandbox_path.mkdir()
    
    mock_get_session.return_value = {
        "active_sandbox_path": str(sandbox_path),
        "active_sandbox_branch": "janus/sandbox-feat",
        "active_sandbox_status": "active"
    }
    
    import subprocess
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["pytest"], timeout=src.config.SANDBOX_TEST_TIMEOUT)
    
    passed, logs = run_sandbox_tests()
    
    assert passed is False
    assert f"timed out after {src.config.SANDBOX_TEST_TIMEOUT} seconds" in logs
    
    # Verify DB was updated to "failed" with timeout logs
    mock_save.assert_called_once_with(str(sandbox_path), "janus/sandbox-feat", "failed", test_logs=logs)

@patch("src.sandbox_session.get_sandbox_session")
@patch("src.sandbox_session.subprocess.run")
def test_get_sandbox_diff(mock_run, mock_get_session):
    """Verify get_sandbox_diff runs git add and diff."""
    mock_get_session.return_value = {
        "active_sandbox_path": "/path/to/sandbox",
        "active_sandbox_branch": "janus/sandbox-feat",
        "active_sandbox_status": "active"
    }
    
    mock_res = MagicMock()
    mock_res.stdout = "dummy git diff content"
    mock_run.return_value = mock_res
    
    diff = get_sandbox_diff()
    
    assert diff == "dummy git diff content"
    assert mock_run.call_count == 2
    assert mock_run.call_args_list[0][0][0] == ["git", "add", "-N", "."]
    assert mock_run.call_args_list[1][0][0] == ["git", "diff"]

@patch("src.sandbox_session.get_sandbox_session")
@patch("src.sandbox_session.subprocess.run")
def test_get_sandbox_modified_files(mock_run, mock_get_session):
    """Verify get_sandbox_modified_files parses porcelain output."""
    mock_get_session.return_value = {
        "active_sandbox_path": "/path/to/sandbox",
        "active_sandbox_branch": "janus/sandbox-feat",
        "active_sandbox_status": "active"
    }
    
    mock_res = MagicMock()
    mock_res.stdout = " M src/calc.py\n?? tests/test_calc.py\n"
    mock_run.return_value = mock_res
    
    files = get_sandbox_modified_files()
    
    assert files == ["src/calc.py", "tests/test_calc.py"]
    mock_run.assert_called_once_with(["git", "status", "--porcelain"], cwd=Path("/path/to/sandbox"), capture_output=True, text=True)

@patch("src.sandbox_session.get_sandbox_session")
@patch("src.sandbox_session.get_sandbox_modified_files")
@patch("src.sandbox_session.cleanup_git_sandbox")
@patch("src.sandbox_session.clear_sandbox_session")
@patch("src.sandbox_session.shutil.copy2")
def test_ship_sandbox_session(
    mock_copy,
    mock_clear,
    mock_cleanup,
    mock_get_modified,
    mock_get_session,
    tmp_path
):
    """Verify ship copies files to active workspace and cleans up."""
    orig_root = src.config.ROOT_DIR
    src.config.ROOT_DIR = tmp_path
    
    sandbox_path = tmp_path / "sandbox"
    sandbox_path.mkdir()
    
    mock_get_session.return_value = {
        "active_sandbox_path": str(sandbox_path),
        "active_sandbox_branch": "janus/sandbox-feat",
        "active_sandbox_status": "passed"
    }
    mock_get_modified.return_value = ["src/helper.py"]
    
    # Write helper in sandbox
    src_file = sandbox_path / "src" / "helper.py"
    src_file.parent.mkdir()
    src_file.write_text("shipped content")
    
    try:
        copied = ship_sandbox_session()
        
        assert copied == ["src/helper.py"]
        mock_copy.assert_called_once_with(src_file, tmp_path / "src" / "helper.py")
        mock_cleanup.assert_called_once_with(str(sandbox_path), "janus/sandbox-feat")
        mock_clear.assert_called_once()
    finally:
        src.config.ROOT_DIR = orig_root

@patch("src.sandbox_session.get_sandbox_session")
@patch("src.sandbox_session.cleanup_git_sandbox")
@patch("src.sandbox_session.clear_sandbox_session")
def test_abort_sandbox_session(mock_clear, mock_cleanup, mock_get_session):
    """Verify abort cleans up without shipping."""
    mock_get_session.return_value = {
        "active_sandbox_path": "/path/to/sandbox",
        "active_sandbox_branch": "janus/sandbox-feat",
        "active_sandbox_status": "active"
    }
    
    abort_sandbox_session()
    
    mock_cleanup.assert_called_once_with("/path/to/sandbox", "janus/sandbox-feat")
    mock_clear.assert_called_once()
