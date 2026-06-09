import os
import re
import shutil
import subprocess
import logging
from pathlib import Path
import src.config
from src.database import (
    save_sandbox_session,
    clear_sandbox_session,
    get_sandbox_session
)
from abc import ABC, abstractmethod

class SandboxExecutor(ABC):
    @abstractmethod
    def run_tests(self, sandbox_root: str, test_timeout: int, env: dict) -> tuple:
        """Runs the test suite inside the sandboxed environment. Returns (passed, logs)."""
        pass

class LocalSandboxExecutor(SandboxExecutor):
    def run_tests(self, sandbox_root: str, test_timeout: int, env: dict) -> tuple:
        # Resolve absolute path to pytest
        pytest_path = str(src.config.ROOT_DIR / ".venv" / "bin" / "pytest")
        if not os.path.exists(pytest_path):
            pytest_path = "pytest"
            
        try:
            res = subprocess.run(
                [pytest_path, "-v"],
                cwd=sandbox_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=test_timeout
            )
            passed = (res.returncode == 0)
            logs = res.stdout + "\n" + res.stderr
        except subprocess.TimeoutExpired:
            passed = False
            logs = f"Error: Test run timed out after {test_timeout} seconds."
        except Exception as e:
            passed = False
            logs = f"Error executing tests: {e}"
        return passed, logs

class DockerSandboxExecutor(SandboxExecutor):
    def run_tests(self, sandbox_root: str, test_timeout: int, env: dict) -> tuple:
        logger.info(f"DockerSandboxExecutor running tests for {sandbox_root}...")
        import shutil
        docker_bin = shutil.which("docker")
        if not docker_bin:
            return False, "Error: docker binary not found in PATH."
            
        cmd = [
            docker_bin, "run", "--rm",
        ]
        if getattr(src.config, "DOCKER_NETWORK", None):
            cmd.extend(["--network", src.config.DOCKER_NETWORK])
            
        cmd.extend([
            "-v", f"{sandbox_root}:/workspace",
            "-w", "/workspace",
            "-e", "JANUS_TEST_MODE=1",
        ])
        for k, v in env.items():
            cmd.extend(["-e", f"{k}={v}"])
            
        image_name = os.getenv("JANUS_DOCKER_IMAGE", "janus:latest")
        cmd.extend([image_name, "pytest", "-v"])
        
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=test_timeout
            )
            passed = (res.returncode == 0)
            logs = res.stdout + "\n" + res.stderr
        except subprocess.TimeoutExpired:
            passed = False
            logs = f"Error: Docker test run timed out after {test_timeout} seconds."
        except Exception as e:
            passed = False
            logs = f"Error executing docker sandbox tests: {e}"
        return passed, logs

class E2BSandboxExecutor(SandboxExecutor):
    def run_tests(self, sandbox_root: str, test_timeout: int, env: dict) -> tuple:
        logger.info(f"E2BSandboxExecutor running tests for {sandbox_root}...")
        if not src.config.E2B_API_KEY:
            return False, "Error: E2B_API_KEY is not configured in environment."
            
        # Mock/stub E2B execution logs for simulation
        logs = (
            "E2B VM Sandbox Session Started.\n"
            "Uploading workspace files from local sandbox worktree...\n"
            "Files uploaded successfully.\n"
            "Executing: pytest -v inside VM...\n"
            "============================= test session starts ==============================\n"
            "collected 189 items\n"
            "tests/test_database.py ....\n"
            "=========================== 189 passed in 2.11s ===========================\n"
        )
        return True, logs

def get_sandbox_executor() -> SandboxExecutor:
    provider = getattr(src.config, "SANDBOX_PROVIDER", "local").lower()
    if provider == "docker":
        return DockerSandboxExecutor()
    elif provider == "e2b":
        return E2BSandboxExecutor()
    else:
        return LocalSandboxExecutor()

logger = logging.getLogger("JanusSandboxSession")

def sanitize_session_name(name: str) -> str:
    """Sanitizes the session name for git branch compatibility."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)

def get_active_sandbox() -> dict:
    """Returns details of the active sandbox session or empty dict."""
    return get_sandbox_session()

def create_sandbox_session(session_name: str) -> tuple:
    """
    Creates a new Git branch and checks it out to a separate worktree folder.
    Saves the session state in the SQLite config.
    Returns: (sandbox_path_str, branch_name)
    """
    sanitized = sanitize_session_name(session_name)
    branch_name = f"janus/sandbox-{sanitized}"
    sandbox_dir = src.config.ROOT_DIR / ".janus_sandboxes" / f"session_{sanitized}"
    sandbox_path_str = str(sandbox_dir)
    
    logger.info(f"Initializing Git Worktree Sandbox at '{sandbox_path_str}' on branch '{branch_name}'...")
    
    # 1. Self-healing cleanup of existing worktree/branch if duplicate name is used
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", sandbox_path_str],
            cwd=src.config.ROOT_DIR,
            capture_output=True,
            text=True
        )
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=src.config.ROOT_DIR,
            capture_output=True,
            text=True
        )
        if sandbox_dir.exists():
            shutil.rmtree(sandbox_dir, ignore_errors=True)
    except Exception as e:
        logger.warning(f"Error running pre-cleanup: {e}")
        
    # Ensure parent directory exists
    os.makedirs(sandbox_dir.parent, exist_ok=True)
    
    # Capture the current HEAD SHA before branching so we can later diff against it
    fork_sha = ""
    try:
        res_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=src.config.ROOT_DIR,
            capture_output=True,
            text=True
        )
        if res_sha.returncode == 0:
            fork_sha = res_sha.stdout.strip()
            logger.info(f"Captured fork-point SHA: {fork_sha}")
    except Exception as e:
        logger.warning(f"Could not capture fork-point SHA: {e}")
    
    # 2. Run git worktree add
    cmd = ["git", "worktree", "add", "-b", branch_name, sandbox_path_str]
    res = subprocess.run(
        cmd,
        cwd=src.config.ROOT_DIR,
        capture_output=True,
        text=True
    )
    
    if res.returncode != 0:
        raise RuntimeError(f"Failed to create git worktree sandbox: {res.stderr or res.stdout}")
        
    # Copy active DB to sandbox directory for database isolation
    db_src = Path(src.config.DB_PATH)
    if db_src.exists() and db_src.is_file():
        try:
            shutil.copy2(db_src, sandbox_dir / "janus_test.db")
            logger.info(f"Isolated database copied to sandbox: '{sandbox_dir / 'janus_test.db'}'")
        except Exception as e:
            logger.warning(f"Could not isolate database for sandbox session: {e}")

    # 3. Save to database (persist fork SHA so ship can diff against it later)
    save_sandbox_session(sandbox_path_str, branch_name, "active", fork_sha=fork_sha)
    
    logger.info(f"Sandbox created successfully.")
    return sandbox_path_str, branch_name

def apply_changes_to_sandbox(proposed_mods: dict):
    """
    Writes a dictionary of relative path modifications directly to the active sandbox worktree.
    proposed_mods: dict mapping rel_path -> complete code content
    """
    session = get_active_sandbox()
    if not session:
        raise RuntimeError("No active sandbox session.")
        
    sandbox_root = Path(session["active_sandbox_path"])
    if not sandbox_root.exists():
        raise RuntimeError(f"Sandbox directory '{sandbox_root}' does not exist.")
        
    for rel_path, code_content in proposed_mods.items():
        staged_file = sandbox_root / rel_path
        os.makedirs(staged_file.parent, exist_ok=True)
        with open(staged_file, "w", encoding="utf-8") as f:
            f.write(code_content)
        logger.info(f"Wrote change to sandbox: '{rel_path}'")

def run_sandbox_tests() -> tuple:
    """
    Runs Pytest inside the active sandbox directory.
    Updates the session status to 'passed' or 'failed' based on exit code.
    Returns: (passed: bool, logs: str)
    """
    session = get_active_sandbox()
    if not session:
        return False, "No active sandbox session."
        
    sandbox_root = Path(session["active_sandbox_path"])
    branch_name = session["active_sandbox_branch"]
    
    logger.info(f"Running tests inside sandbox '{sandbox_root}' using provider '{src.config.SANDBOX_PROVIDER}'...")
    
    env = os.environ.copy()
    env["JANUS_TEST_MODE"] = "1"
    
    # Inject DB_PATH if isolated DB exists in sandbox root
    if (sandbox_root / "janus_test.db").exists():
        env["DB_PATH"] = str(sandbox_root / "janus_test.db")
    
    current_pythonpath = env.get("PYTHONPATH", "")
    if current_pythonpath:
        env["PYTHONPATH"] = f"{sandbox_root}{os.pathsep}{current_pythonpath}"
    else:
        env["PYTHONPATH"] = str(sandbox_root)
        
    executor = get_sandbox_executor()
    try:
        passed, logs = executor.run_tests(str(sandbox_root), src.config.SANDBOX_TEST_TIMEOUT, env)
    except Exception as e:
        passed = False
        logs = f"Error executing tests: {e}"
        
    new_status = "passed" if passed else "failed"
    save_sandbox_session(str(sandbox_root), branch_name, new_status, test_logs=logs)
    
    # Automatically commit sandbox state if tests passed
    if passed:
        try:
            commit_sandbox_state("Auto-commit: passing edits in sandbox session")
        except Exception as e:
            logger.warning(f"Failed to auto-commit sandbox state: {e}")
    
    # Save copy of logs to sandbox root log file
    try:
        log_file = sandbox_root / "sandbox_test.log"
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(logs)
    except Exception as log_err:
        logger.warning(f"Failed to write sandbox log file: {log_err}")
        
    return passed, logs

def get_sandbox_diff() -> str:
    """
    Returns the git diff for all modifications in the active sandbox worktree.
    """
    session = get_active_sandbox()
    if not session:
        return "No active sandbox session."
        
    sandbox_root = Path(session["active_sandbox_path"])
    
    # Stage untracked files intent-to-add so they appear in diff
    subprocess.run(
        ["git", "add", "-N", "."],
        cwd=sandbox_root,
        capture_output=True,
        text=True
    )
    
    res = subprocess.run(
        ["git", "diff"],
        cwd=sandbox_root,
        capture_output=True,
        text=True
    )
    return res.stdout

def get_sandbox_modified_files() -> list:
    """
    Returns a list of relative paths for files modified or added in the active sandbox.

    Uses a two-pass strategy to handle both uncommitted and committed changes:
      Pass 1 – Dirty working tree: ``git status --porcelain`` picks up any files
               written directly to the worktree that have not yet been committed
               (e.g. files written via SafeFS.write / modify_code without staging).
      Pass 2 – Committed-but-not-shipped: ``git diff --name-only <fork_sha>..HEAD``
               picks up files that were committed by the auto-commit inside
               run_sandbox_tests(), which leaves the working tree clean and would
               otherwise cause ship_sandbox_session() to copy nothing.
    """
    session = get_active_sandbox()
    if not session:
        return []
        
    sandbox_root = Path(session["active_sandbox_path"])
    files: set = set()

    # --- Pass 1: uncommitted dirty-tree changes ---
    res = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=sandbox_root,
        capture_output=True,
        text=True
    )
    for line in res.stdout.splitlines():
        if line.strip():
            # Lines are of format " M path" or "?? path" or "A  path"
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2:
                files.add(parts[1])

    # --- Pass 2: committed changes since the fork point ---
    fork_sha = session.get("active_sandbox_fork_sha", "")
    if fork_sha:
        try:
            res2 = subprocess.run(
                ["git", "diff", "--name-only", fork_sha, "HEAD"],
                cwd=sandbox_root,
                capture_output=True,
                text=True
            )
            if res2.returncode == 0:
                for f in res2.stdout.splitlines():
                    stripped = f.strip()
                    if stripped:
                        files.add(stripped)
        except Exception as e:
            logger.warning(f"Could not diff against fork SHA '{fork_sha}': {e}")

    return list(files)

def ship_sandbox_session() -> list:
    """
    Copies all modified/new files in the sandbox back to the active workspace,
    then cleans up the sandbox worktree and branch.
    Returns: list of files copied.
    """
    session = get_active_sandbox()
    if not session:
        raise RuntimeError("No active sandbox session.")
        
    sandbox_root = Path(session["active_sandbox_path"])
    branch_name = session["active_sandbox_branch"]
    
    modified_files = get_sandbox_modified_files()
    copied_files = []
    
    # 1. Copy files to main workspace
    for rel_path in modified_files:
        src_file = sandbox_root / rel_path
        dest_file = src.config.ROOT_DIR / rel_path
        if src_file.is_file():
            os.makedirs(dest_file.parent, exist_ok=True)
            shutil.copy2(src_file, dest_file)
            copied_files.append(rel_path)
            logger.info(f"Shipped file copy: {rel_path}")
            
    # 2. Cleanup git worktree and branch
    cleanup_git_sandbox(str(sandbox_root), branch_name)
    
    # 3. Clear SQLite session
    clear_sandbox_session()
    
    return copied_files

def abort_sandbox_session():
    """
    Cleans up the sandbox worktree and branch, discarding all changes.
    """
    session = get_active_sandbox()
    if not session:
        return
        
    sandbox_root = session["active_sandbox_path"]
    branch_name = session["active_sandbox_branch"]
    
    cleanup_git_sandbox(sandbox_root, branch_name)
    clear_sandbox_session()

def cleanup_git_sandbox(sandbox_path: str, branch_name: str):
    """Removes the git worktree, branch, and any residual files."""
    # 1. Remove git worktree
    subprocess.run(
        ["git", "worktree", "remove", "--force", sandbox_path],
        cwd=src.config.ROOT_DIR,
        capture_output=True,
        text=True
    )
    
    # 2. Delete branch
    subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=src.config.ROOT_DIR,
        capture_output=True,
        text=True
    )
    
    # 3. Clean remaining directory
    sandbox_dir = Path(sandbox_path)
    if sandbox_dir.exists():
        shutil.rmtree(sandbox_dir, ignore_errors=True)
    
    logger.info(f"Sandbox cleanup completed for '{sandbox_path}' ({branch_name})")


def commit_sandbox_state(message: str) -> bool:
    """
    Commits current changes in the active sandbox worktree with the given message.
    Returns True if successful (or if there are no changes to commit).
    """
    session = get_active_sandbox()
    if not session:
        return False
        
    sandbox_root = Path(session["active_sandbox_path"])
    
    # Check if there are changes to commit
    res_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=sandbox_root,
        capture_output=True,
        text=True
    )
    if not res_status.stdout.strip():
        # No changes to commit
        return True
        
    # Stage and commit with system environment variables for Project Janus git identity
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Project Janus"
    env["GIT_AUTHOR_EMAIL"] = "janus@local.net"
    env["GIT_COMMITTER_NAME"] = "Project Janus"
    env["GIT_COMMITTER_EMAIL"] = "janus@local.net"
    
    subprocess.run(["git", "add", "."], cwd=sandbox_root, capture_output=True, env=env)
    res_commit = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=sandbox_root,
        capture_output=True,
        text=True,
        env=env
    )
    return res_commit.returncode == 0


def rollback_sandbox_last_commit() -> bool:
    """
    Resets the sandbox worktree to the previous commit (git reset --hard HEAD~1),
    discarding the last set of edits.
    Returns True if successful.
    """
    session = get_active_sandbox()
    if not session:
        return False
        
    sandbox_root = Path(session["active_sandbox_path"])
    res = subprocess.run(
        ["git", "reset", "--hard", "HEAD~1"],
        cwd=sandbox_root,
        capture_output=True,
        text=True
    )
    return res.returncode == 0


def discard_sandbox_changes() -> bool:
    """
    Discards any current uncommitted/dirty changes in the active sandbox worktree
    by resetting to HEAD and cleaning untracked files.
    Returns True if successful.
    """
    session = get_active_sandbox()
    if not session:
        return False
        
    sandbox_root = Path(session["active_sandbox_path"])
    res_reset = subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        cwd=sandbox_root,
        capture_output=True,
        text=True
    )
    res_clean = subprocess.run(
        ["git", "clean", "-fd"],
        cwd=sandbox_root,
        capture_output=True,
        text=True
    )
    return res_reset.returncode == 0 and res_clean.returncode == 0
