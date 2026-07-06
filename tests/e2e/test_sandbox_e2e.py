"""End-to-end: real git worktree sandbox lifecycle (create -> apply -> test -> ship).

Uses the isolated_git_workspace fixture (tests/e2e/conftest.py) so
create_sandbox_session/ship_sandbox_session's real `git worktree`/`branch`
subprocesses and the real (but synthetic, single-test) `pytest -v` run inside
run_sandbox_tests() never touch the actual /opt/janus checkout or its own test
suite. No LLM mocking is needed here — none of these functions call query_agent.
"""

import subprocess

import pytest

from src.sandbox_session import (
    apply_changes_to_sandbox,
    create_sandbox_session,
    get_active_sandbox,
    run_sandbox_tests,
    sanitize_session_name,
    ship_sandbox_session,
)

pytestmark = pytest.mark.e2e


def test_sandbox_full_lifecycle(isolated_git_workspace):
    workspace = isolated_git_workspace

    path, branch = create_sandbox_session("e2e-lifecycle")
    sandbox_root = (
        workspace / ".janus_sandboxes" / f"session_{sanitize_session_name('e2e-lifecycle')}"
    )
    assert str(sandbox_root) == path
    assert sandbox_root.exists()

    branches = subprocess.run(
        ["git", "branch", "--list", branch], cwd=workspace, capture_output=True, text=True
    ).stdout
    assert branch in branches

    apply_changes_to_sandbox({"src/greeting.py": "def greet():\n    return 'hi'\n"})
    assert (sandbox_root / "src" / "greeting.py").read_text() == "def greet():\n    return 'hi'\n"

    passed, logs = run_sandbox_tests()
    assert passed is True, f"expected sandboxed test run to pass, got logs:\n{logs}"

    copied_files = ship_sandbox_session()
    assert "src/greeting.py" in copied_files

    shipped_file = workspace / "src" / "greeting.py"
    assert shipped_file.exists()
    assert shipped_file.read_text() == "def greet():\n    return 'hi'\n"

    # Cleanup must have run: no active session, no leftover worktree/branch.
    assert get_active_sandbox() == {}
    worktrees = subprocess.run(
        ["git", "worktree", "list"], cwd=workspace, capture_output=True, text=True
    ).stdout
    assert str(sandbox_root) not in worktrees
    branches_after = subprocess.run(
        ["git", "branch", "--list", branch], cwd=workspace, capture_output=True, text=True
    ).stdout
    assert branch not in branches_after


def test_ship_aborts_on_regression(isolated_git_workspace):
    workspace = isolated_git_workspace

    path, branch = create_sandbox_session("e2e-regression")
    apply_changes_to_sandbox(
        {"tests/test_trivial.py": "def test_trivial():\n    assert False\n"}
    )

    with pytest.raises(RuntimeError, match="Regression detected"):
        ship_sandbox_session()

    # abort_sandbox_session() must have cleaned up on the regression path.
    assert get_active_sandbox() == {}
    worktrees = subprocess.run(
        ["git", "worktree", "list"], cwd=workspace, capture_output=True, text=True
    ).stdout
    assert path not in worktrees
    branches_after = subprocess.run(
        ["git", "branch", "--list", branch], cwd=workspace, capture_output=True, text=True
    ).stdout
    assert branch not in branches_after
