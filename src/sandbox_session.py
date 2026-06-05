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
        
    # 3. Save to database
    save_sandbox_session(sandbox_path_str, branch_name, "active")
    
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
    
    logger.info(f"Running pytest inside sandbox '{sandbox_root}'...")
    
    # Resolve absolute path to pytest
    pytest_path = str(src.config.ROOT_DIR / ".venv" / "bin" / "pytest")
    if not os.path.exists(pytest_path):
        pytest_path = "pytest"
        
    env = os.environ.copy()
    env["JANUS_TEST_MODE"] = "1"
    
    current_pythonpath = env.get("PYTHONPATH", "")
    if current_pythonpath:
        env["PYTHONPATH"] = f"{sandbox_root}{os.pathsep}{current_pythonpath}"
    else:
        env["PYTHONPATH"] = str(sandbox_root)
        
    try:
        res = subprocess.run(
            [pytest_path, "-v"],
            cwd=sandbox_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30
        )
        passed = (res.returncode == 0)
        logs = res.stdout + "\n" + res.stderr
    except subprocess.TimeoutExpired:
        passed = False
        logs = "Error: Test run timed out after 30 seconds."
    except Exception as e:
        passed = False
        logs = f"Error executing tests: {e}"
        
    new_status = "passed" if passed else "failed"
    save_sandbox_session(str(sandbox_root), branch_name, new_status, test_logs=logs)
    
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
    """
    session = get_active_sandbox()
    if not session:
        return []
        
    sandbox_root = Path(session["active_sandbox_path"])
    
    # git status --porcelain
    res = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=sandbox_root,
        capture_output=True,
        text=True
    )
    
    files = []
    for line in res.stdout.splitlines():
        if line.strip():
            # Lines are of format " M path" or "?? path" or "A  path"
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2:
                files.append(parts[1])
    return files

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
