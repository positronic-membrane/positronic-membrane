from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.sandbox_session import commit_sandbox_state, discard_sandbox_changes, rollback_sandbox_last_commit
from src.self_modification import apply_search_replace_blocks

# -----------------
# 1. Search-and-Replace Tests
# -----------------

def test_apply_search_replace_blocks_success():
    current = "line 1\nline 2\nline 3\n"
    block = """<<<<<<< SEARCH
line 2
=======
line two updated
>>>>>>> REPLACE"""
    result = apply_search_replace_blocks(current, block)
    assert result == "line 1\nline two updated\nline 3\n"

def test_apply_search_replace_blocks_no_match():
    current = "line 1\nline 2\nline 3\n"
    block = """<<<<<<< SEARCH
line 4
=======
line four
>>>>>>> REPLACE"""
    with pytest.raises(ValueError, match="Search block not found"):
        apply_search_replace_blocks(current, block)

def test_apply_search_replace_blocks_multiple_matches():
    current = "line 2\nline 2\nline 3\n"
    block = """<<<<<<< SEARCH
line 2
=======
line two
>>>>>>> REPLACE"""
    with pytest.raises(ValueError, match="Search block matches multiple times"):
        apply_search_replace_blocks(current, block)

def test_apply_search_replace_blocks_normalization():
    current = "line 1\r\nline 2\r\nline 3\r\n"
    block = """<<<<<<< SEARCH
line 2
=======
line two
>>>>>>> REPLACE"""
    result = apply_search_replace_blocks(current, block)
    assert "line two" in result

# -----------------
# 2. Git Transactional Tests
# -----------------

@patch("src.sandbox_session.get_active_sandbox")
@patch("src.sandbox_session.subprocess.run")
def test_commit_sandbox_state_no_changes(mock_run, mock_get_active):
    mock_get_active.return_value = {
        "active_sandbox_path": "/mock/sandbox",
        "active_sandbox_branch": "janus/sandbox-feat"
    }
    # Mock git status indicating no changes
    mock_res = MagicMock()
    mock_res.stdout = ""
    mock_run.return_value = mock_res

    success = commit_sandbox_state("test commit")
    assert success is True
    # Git add/commit should not have run since status is clean
    assert mock_run.call_count == 1

@patch("src.sandbox_session.get_active_sandbox")
@patch("src.sandbox_session.subprocess.run")
def test_commit_sandbox_state_with_changes(mock_run, mock_get_active):
    mock_get_active.return_value = {
        "active_sandbox_path": "/mock/sandbox",
        "active_sandbox_branch": "janus/sandbox-feat"
    }
    # 1. status -> modified files
    # 2. git add . -> success
    # 3. git commit -> success
    mock_status = MagicMock()
    mock_status.stdout = " M src/utils.py"

    mock_add = MagicMock()
    mock_add.returncode = 0

    mock_commit = MagicMock()
    mock_commit.returncode = 0

    mock_run.side_effect = [mock_status, mock_add, mock_commit]

    success = commit_sandbox_state("Passing changes committed")
    assert success is True
    assert mock_run.call_count == 3
    # Verify environment has custom author details injected
    commit_env = mock_run.call_args_list[2][1].get("env", {})
    assert commit_env.get("GIT_AUTHOR_NAME") == "Project Janus"

@patch("src.sandbox_session.get_active_sandbox")
@patch("src.sandbox_session.subprocess.run")
def test_rollback_sandbox_last_commit(mock_run, mock_get_active):
    mock_get_active.return_value = {
        "active_sandbox_path": "/mock/sandbox",
        "active_sandbox_branch": "janus/sandbox-feat"
    }
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_run.return_value = mock_res

    success = rollback_sandbox_last_commit()
    assert success is True
    mock_run.assert_called_once_with(
        ["git", "reset", "--hard", "HEAD~1"],
        cwd=Path("/mock/sandbox"),
        capture_output=True,
        text=True
    )

@patch("src.sandbox_session.get_active_sandbox")
@patch("src.sandbox_session.subprocess.run")
def test_discard_sandbox_changes(mock_run, mock_get_active):
    mock_get_active.return_value = {
        "active_sandbox_path": "/mock/sandbox",
        "active_sandbox_branch": "janus/sandbox-feat"
    }
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_run.return_value = mock_res

    success = discard_sandbox_changes()
    assert success is True
    assert mock_run.call_count == 2
    mock_run.assert_any_call(
        ["git", "reset", "--hard", "HEAD"],
        cwd=Path("/mock/sandbox"),
        capture_output=True,
        text=True
    )
    mock_run.assert_any_call(
        ["git", "clean", "-fd"],
        cwd=Path("/mock/sandbox"),
        capture_output=True,
        text=True
    )
