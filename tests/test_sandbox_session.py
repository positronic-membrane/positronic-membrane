from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import src.config
from src.sandbox_session import (
    abort_sandbox_session,
    apply_changes_to_sandbox,
    create_sandbox_session,
    get_sandbox_diff,
    get_sandbox_modified_files,
    run_sandbox_tests,
    sanitize_session_name,
    ship_sandbox_session,
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

    orig_provider = src.config.SANDBOX_PROVIDER
    orig_allow_local = src.config.ALLOW_LOCAL_SANDBOX_EXEC
    src.config.SANDBOX_PROVIDER = "local"
    src.config.ALLOW_LOCAL_SANDBOX_EXEC = True
    try:
        passed, logs = run_sandbox_tests()
    finally:
        src.config.SANDBOX_PROVIDER = orig_provider
        src.config.ALLOW_LOCAL_SANDBOX_EXEC = orig_allow_local

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

    orig_provider = src.config.SANDBOX_PROVIDER
    orig_allow_local = src.config.ALLOW_LOCAL_SANDBOX_EXEC
    src.config.SANDBOX_PROVIDER = "local"
    src.config.ALLOW_LOCAL_SANDBOX_EXEC = True
    try:
        passed, logs = run_sandbox_tests()
    finally:
        src.config.SANDBOX_PROVIDER = orig_provider
        src.config.ALLOW_LOCAL_SANDBOX_EXEC = orig_allow_local

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
    """Verify get_sandbox_modified_files parses porcelain output (dirty-tree pass)."""
    mock_get_session.return_value = {
        "active_sandbox_path": "/path/to/sandbox",
        "active_sandbox_branch": "janus/sandbox-feat",
        "active_sandbox_status": "active",
        # No fork_sha → only the dirty-tree pass runs
    }

    # Pass 1 (git status --porcelain) returns two files; no Pass 2 since no fork_sha
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = " M src/calc.py\n?? tests/test_calc.py\n"
    mock_run.return_value = mock_res

    files = get_sandbox_modified_files()

    assert sorted(files) == ["src/calc.py", "tests/test_calc.py"]
    # Only one subprocess call because there is no fork_sha to trigger Pass 2
    mock_run.assert_called_once_with(
        ["git", "status", "--porcelain"],
        cwd=Path("/path/to/sandbox"),
        capture_output=True,
        text=True
    )


@patch("src.sandbox_session.get_sandbox_session")
@patch("src.sandbox_session.subprocess.run")
def test_get_sandbox_modified_files_with_fork_sha(mock_run, mock_get_session):
    """
    Regression test: when the worktree is clean (auto-commit ran) but a fork_sha is
    present, Pass 2 (git diff --name-only) must pick up the committed files.
    """
    fork_sha = "abc1234"
    mock_get_session.return_value = {
        "active_sandbox_path": "/path/to/sandbox",
        "active_sandbox_branch": "janus/sandbox-feat",
        "active_sandbox_status": "passed",
        "active_sandbox_fork_sha": fork_sha,
    }

    # Pass 1: clean working tree (the auto-commit already ran)
    clean_res = MagicMock()
    clean_res.returncode = 0
    clean_res.stdout = ""

    # Pass 2: git diff reports the committed file
    diff_res = MagicMock()
    diff_res.returncode = 0
    diff_res.stdout = "docs/pre_cloud_multi_party_hardening.md\n"

    mock_run.side_effect = [clean_res, diff_res]

    files = get_sandbox_modified_files()

    assert files == ["docs/pre_cloud_multi_party_hardening.md"]

    # Verify Pass 2 call used the correct fork SHA
    call_args_list = mock_run.call_args_list
    assert len(call_args_list) == 2
    assert call_args_list[0][0][0] == ["git", "status", "--porcelain"]
    assert call_args_list[1][0][0] == ["git", "diff", "--name-only", fork_sha, "HEAD"]


@patch("src.sandbox_session.get_sandbox_session")
@patch("src.sandbox_session.subprocess.run")
def test_get_sandbox_modified_files_union_of_both_passes(mock_run, mock_get_session):
    """
    Verify that both dirty-tree files AND committed files are returned together
    (de-duplicated) when a session has a fork_sha stored.
    """
    fork_sha = "deadbeef"
    mock_get_session.return_value = {
        "active_sandbox_path": "/path/to/sandbox",
        "active_sandbox_branch": "janus/sandbox-feat",
        "active_sandbox_status": "active",
        "active_sandbox_fork_sha": fork_sha,
    }

    dirty_res = MagicMock()
    dirty_res.returncode = 0
    dirty_res.stdout = "?? src/new_file.py\n"

    diff_res = MagicMock()
    diff_res.returncode = 0
    # Includes src/new_file.py (overlap) + a committed file
    diff_res.stdout = "src/new_file.py\nREADME.md\n"

    mock_run.side_effect = [dirty_res, diff_res]

    files = get_sandbox_modified_files()

    # De-duplicated union
    assert sorted(files) == ["README.md", "src/new_file.py"]


@patch("src.sandbox_session.get_sandbox_session")
@patch("src.sandbox_session.get_sandbox_modified_files")
@patch("src.sandbox_session.cleanup_git_sandbox")
@patch("src.sandbox_session.clear_sandbox_session")
def test_ship_sandbox_session_after_auto_commit(
    mock_clear,
    mock_cleanup,
    mock_get_modified,
    mock_get_session,
    tmp_path
):
    """
    Regression test: ship_sandbox_session must copy files that were auto-committed
    by run_sandbox_tests (leaving a clean working tree), NOT just dirty-tree files.
    """
    import src.config as cfg
    orig_root = cfg.ROOT_DIR
    cfg.ROOT_DIR = tmp_path

    sandbox_path = tmp_path / "sandbox"
    sandbox_path.mkdir()

    mock_get_session.return_value = {
        "active_sandbox_path": str(sandbox_path),
        "active_sandbox_branch": "janus/sandbox-feat",
        "active_sandbox_status": "passed",
        "active_sandbox_fork_sha": "abc1234",
    }
    # Simulate: get_sandbox_modified_files correctly returns the committed file
    committed_file = "docs/pre_cloud_multi_party_hardening.md"
    mock_get_modified.return_value = [committed_file]

    # Create the file inside the sandbox so copy2 has something to copy
    doc_dir = sandbox_path / "docs"
    doc_dir.mkdir()
    (doc_dir / "pre_cloud_multi_party_hardening.md").write_text("hardening docs")

    try:
        copied = ship_sandbox_session()

        # The committed file must be reported as copied
        assert committed_file in copied
        # Cleanup must run
        mock_cleanup.assert_called_once_with(str(sandbox_path), "janus/sandbox-feat")
        mock_clear.assert_called_once()
    finally:
        cfg.ROOT_DIR = orig_root

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


# --- Consolidating from test_v1_priority1.py ---

@patch("src.sandbox_session.get_active_sandbox")
@patch("src.sandbox_session.run_sandbox_tests")
@patch("src.sandbox_session.abort_sandbox_session")
@patch("src.sandbox_session.cleanup_git_sandbox")
@patch("src.sandbox_session.clear_sandbox_session")
@patch("src.sandbox_session.shutil.copy2")
def test_regression_watcher_flow(
    mock_copy,
    mock_clear,
    mock_cleanup,
    mock_abort,
    mock_run_tests,
    mock_get_active_sb
):
    import src.config as cfg
    from src.database import get_connection
    orig_root = cfg.ROOT_DIR
    tmp_path = Path("/tmp/test_janus_v1_p1_watcher")
    cfg.ROOT_DIR = tmp_path

    # Active sandbox path contains a tests directory to trigger execution
    sandbox_path = tmp_path / "sandbox"
    (sandbox_path / "tests").mkdir(parents=True, exist_ok=True)

    mock_get_active_sb.return_value = {
        "active_sandbox_path": str(sandbox_path),
        "active_sandbox_branch": "janus-test-branch",
        "active_sandbox_status": "active"
    }

    db_conn = get_connection(read_only_constitution=False)
    import sqlite3
    db_conn.row_factory = sqlite3.Row

    try:
        # Case 1: Sandbox tests failed (Regression)
        mock_run_tests.return_value = (False, "=== 1 failed in 0.5s ===")
        with pytest.raises(RuntimeError) as exc:
            ship_sandbox_session()
        assert "Regression detected" in str(exc.value)
        mock_abort.assert_called_once()
        mock_abort.reset_mock()

        # Verify regression logged in database
        logs = db_conn.execute("SELECT message_content FROM episodic_memory WHERE speaker = 'system';").fetchall()
        assert len(logs) == 1
        assert "Regression Watcher aborted sandbox ship flow" in logs[0]["message_content"]

        # Clear episodic memory to reset system logs
        db_conn.execute("DELETE FROM episodic_memory;")
        db_conn.commit()

        # Case 2: Sandbox tests passed, inserts first baseline
        mock_run_tests.return_value = (True, "=== 10 passed in 1.2s ===\nTOTAL          100     20    80%")
        with patch("src.sandbox_session.get_sandbox_modified_files", return_value=["src/main.py"]):
            main_py = sandbox_path / "src" / "main.py"
            main_py.parent.mkdir(parents=True, exist_ok=True)
            main_py.write_text("print('hello')")
            copied = ship_sandbox_session()
            assert copied == ["src/main.py"]
            mock_clear.assert_called_once()
            mock_clear.reset_mock()

        # Verify baseline inserted
        baselines = db_conn.execute("SELECT total_tests, passed_tests, failed_tests, coverage_percentage FROM test_run_baselines;").fetchall()
        assert len(baselines) == 1
        assert baselines[0]["total_tests"] == 10
        assert baselines[0]["passed_tests"] == 10
        assert baselines[0]["failed_tests"] == 0
        assert baselines[0]["coverage_percentage"] == 80.0

        # Case 3: Tests pass but coverage drops (Regression)
        mock_get_active_sb.return_value = {
            "active_sandbox_path": str(sandbox_path),
            "active_sandbox_branch": "janus-test-branch",
            "active_sandbox_status": "active"
        }
        mock_run_tests.return_value = (True, "=== 10 passed in 1.1s ===\nTOTAL          100     25    75%")
        with pytest.raises(RuntimeError) as exc:
            ship_sandbox_session()
        assert "Regression detected: Coverage dropped from 80.0% to 75.0%." in str(exc.value)
        mock_abort.assert_called_once()
        mock_abort.reset_mock()

        # Case 4: Tests pass, coverage is None (Graceful Degradation)
        mock_get_active_sb.return_value = {
            "active_sandbox_path": str(sandbox_path),
            "active_sandbox_branch": "janus-test-branch",
            "active_sandbox_status": "active"
        }
        mock_run_tests.return_value = (True, "=== 10 passed in 1.1s ===")
        with patch("src.sandbox_session.get_sandbox_modified_files", return_value=["src/main.py"]):
            main_py = sandbox_path / "src" / "main.py"
            main_py.parent.mkdir(parents=True, exist_ok=True)
            main_py.write_text("print('hello')")
            copied = ship_sandbox_session()
            assert copied == ["src/main.py"]

        baselines = db_conn.execute("SELECT coverage_percentage FROM test_run_baselines ORDER BY id DESC LIMIT 1;").fetchone()
        assert baselines["coverage_percentage"] is None

    finally:
        cfg.ROOT_DIR = orig_root
        db_conn.close()
        import shutil
        shutil.rmtree(tmp_path, ignore_errors=True)
