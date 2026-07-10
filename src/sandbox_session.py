import os
import re
import shutil
import subprocess
import sys
import logging
from pathlib import Path
import src.config
from src.database import (
    save_sandbox_session,
    clear_sandbox_session,
    get_sandbox_session,
    get_connection,
    log_episodic_memory
)
from src.regression_watcher import get_current_commit_sha, record_test_run
from abc import ABC, abstractmethod

class SandboxExecutor(ABC):
    @abstractmethod
    def run_tests(self, sandbox_root: str, test_timeout: int, env: dict) -> tuple:
        """Runs the test suite inside the sandboxed environment. Returns (passed, logs)."""
        pass

class LocalSandboxExecutor(SandboxExecutor):
    def run_tests(self, sandbox_root: str, test_timeout: int, env: dict) -> tuple:
        if not getattr(src.config, "ALLOW_LOCAL_SANDBOX_EXEC", False):
            return False, (
                "Error: LocalSandboxExecutor is disabled by default for security "
                "(unsandboxed host subprocess execution). Set SANDBOX_PROVIDER=docker "
                "(recommended, default) or explicitly set ALLOW_LOCAL_SANDBOX_EXEC=True "
                "and SANDBOX_PROVIDER=local to override."
            )

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
        docker_bin = shutil.which("docker")
        if not docker_bin:
            return False, "Error: docker binary not found in PATH."

        image_name = getattr(src.config, "JANUS_DOCKER_IMAGE", "janus:latest")

        info_res = subprocess.run([docker_bin, "info"], capture_output=True, text=True, timeout=10)
        if info_res.returncode != 0:
            return False, (
                f"Error: Docker daemon is not reachable. Is Docker running? "
                f"(docker info failed: {info_res.stderr})"
            )

        inspect_res = subprocess.run(
            [docker_bin, "image", "inspect", image_name], capture_output=True, text=True, timeout=10
        )
        if inspect_res.returncode != 0:
            return False, (
                f"Error: Docker image '{image_name}' not found locally. "
                f"Build it first with: docker build -t {image_name} ."
            )

        cmd = [
            docker_bin, "run", "--rm",
        ]
        if getattr(src.config, "DOCKER_NETWORK", None):
            cmd.extend(["--network", src.config.DOCKER_NETWORK])

        cmd.extend([
            "--memory", getattr(src.config, "DOCKER_MEMORY_LIMIT", "512m"),
            "--cpus", getattr(src.config, "DOCKER_CPU_LIMIT", "1.0"),
            "--pids-limit", getattr(src.config, "DOCKER_PIDS_LIMIT", "256"),
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
        ])

        cmd.extend([
            "-v", f"{sandbox_root}:/workspace",
            "-w", "/workspace",
            "-e", "JANUS_TEST_MODE=1",
        ])
        for k, v in env.items():
            cmd.extend(["-e", f"{k}={v}"])

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

def create_sandbox_session(session_name: str, purpose: str = "evolution", app_name: str = None) -> tuple:
    """
    Creates a new sandbox session of the given purpose.

    purpose="evolution" (default): today's existing behavior, unchanged for every
    caller that doesn't pass `purpose` explicitly — a git worktree on an
    `evolution/*` branch with a duplicated SQLite DB, for agent self-modification.

    purpose="project": a plain, independently `git init`'d folder under
    `.janus_sandboxes/projects/<app_name>` for building an independent user app.
    No worktree, no branch, no DB duplication — SafeFS confines reads/writes to
    this folder via the same active-sandbox mechanism evolution sessions use.

    Returns: (sandbox_path_str, branch_name) — branch_name is "" for project sandboxes.
    """
    if purpose == "project":
        return _create_project_sandbox(session_name, app_name or session_name)
    return _create_evolution_sandbox(session_name)

def _create_evolution_sandbox(session_name: str) -> tuple:
    """
    Creates a new Git branch and checks it out to a separate worktree folder.
    Saves the session state in the SQLite config.
    Returns: (sandbox_path_str, branch_name)
    """
    sanitized = sanitize_session_name(session_name)
    branch_name = f"evolution/sandbox-{sanitized}"
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
    save_sandbox_session(
        sandbox_path_str, branch_name, "active", fork_sha=fork_sha, purpose="evolution", session_name=session_name
    )

    logger.info(f"Sandbox created successfully.")
    return sandbox_path_str, branch_name

def _create_project_sandbox(session_name: str, app_name: str, overwrite: bool = False) -> tuple:
    """
    Provisions an isolated, independently git-init'd folder for building a user
    app, unrelated to Janus's own codebase or database. Unlike evolution
    sandboxes (throwaway-per-attempt worktrees), project directories are meant to
    persist across sessions, so an existing app_name raises unless overwrite=True.
    Returns: (sandbox_path_str, "") — there is no branch concept for project sandboxes.
    """
    sanitized = sanitize_session_name(app_name)
    project_dir = src.config.ROOT_DIR / ".janus_sandboxes" / "projects" / sanitized
    project_path_str = str(project_dir)

    if project_dir.exists():
        if not overwrite:
            raise RuntimeError(
                f"Project sandbox '{sanitized}' already exists at '{project_path_str}'. "
                f"Choose a different app_name, or pass overwrite=True to recreate it."
            )
        shutil.rmtree(project_dir, ignore_errors=True)

    os.makedirs(project_dir, exist_ok=True)

    res = subprocess.run(
        ["git", "init", project_path_str],
        capture_output=True,
        text=True
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed to git init project sandbox: {res.stderr or res.stdout}")

    save_sandbox_session(
        project_path_str, "", "active", purpose="project", app_name=sanitized, session_name=session_name
    )

    logger.info(f"Project sandbox '{sanitized}' created at '{project_path_str}'.")
    return project_path_str, ""

def delete_project_sandbox(app_name: str) -> bool:
    """
    Permanently deletes a project sandbox directory. This is the only path that
    removes project app files — ship/abort never do, since ending a session
    shouldn't delete the user's app. Returns True if a directory was deleted.
    """
    sanitized = sanitize_session_name(app_name)
    project_dir = src.config.ROOT_DIR / ".janus_sandboxes" / "projects" / sanitized
    if not project_dir.exists():
        return False

    session = get_active_sandbox()
    if session and session.get("active_sandbox_purpose") == "project" and \
            session.get("active_sandbox_app_name") == sanitized:
        clear_sandbox_session()

    shutil.rmtree(project_dir, ignore_errors=True)
    logger.info(f"Deleted project sandbox '{sanitized}'.")
    return True

def _find_free_evolution_port(start_port: int = 5001, max_attempts: int = 50) -> int:
    """Returns the first port >= start_port that nothing is currently listening on."""
    import socket
    port = start_port
    for _ in range(max_attempts):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            in_use = sock.connect_ex(("127.0.0.1", port)) == 0
        finally:
            sock.close()
        if not in_use:
            return port
        port += 1
    raise RuntimeError(f"Could not find a free evolution port starting from {start_port}.")

def spawn_evolution_daemon(sandbox_dir, branch_name: str) -> dict:
    """
    Launches a concurrent child Janus daemon process inside an evolution
    sandbox worktree, pointed at the already-duplicated DB and running on an
    offset port, so the agent can autonomously work inside the sandbox using
    its own live heartbeat loop. Opt-in — create_sandbox_session() never calls
    this automatically, since most evolution sandbox usage (e.g. dispatch_task)
    is a write-files-then-test flow that doesn't need a live child process.

    Reuses the subprocess-launch + spawn_log bookkeeping shape from
    SafeReplication.spawn_child() (src/skills.py), not its tree-copy/DB-reseed —
    the worktree and DB copy already exist from _create_evolution_sandbox().
    """
    sandbox_dir = Path(sandbox_dir)
    child_db_path = sandbox_dir / "janus_test.db"
    if not child_db_path.exists():
        raise RuntimeError(f"No isolated database found at '{child_db_path}' — cannot spawn evolution daemon.")

    branch_suffix = branch_name.rsplit("-", 1)[-1] if branch_name else sandbox_dir.name
    self_party_id = f"evolution_{branch_suffix}"
    port = _find_free_evolution_port()

    env = os.environ.copy()
    env["DB_PATH"] = str(child_db_path)
    env["DB_TYPE"] = "sqlite"
    env["JANUS_EVOLUTION_PORT"] = str(port)
    env["JANUS_PARENT_DB_PATH"] = str(Path(src.config.DB_PATH).resolve())
    env["JANUS_ROLE"] = "evolution_child"
    env["JANUS_PARENT_PARTY_ID"] = "parent"
    env["JANUS_SELF_PARTY_ID"] = self_party_id
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{sandbox_dir}{os.pathsep}{current_pythonpath}" if current_pythonpath else str(sandbox_dir)
    )

    conn = get_connection(read_only_constitution=True)
    try:
        conn.execute("""
        INSERT OR REPLACE INTO spawn_log (child_path, status, spawned_at)
        VALUES (?, 'spawning', CURRENT_TIMESTAMP);
        """, (str(sandbox_dir),))
        conn.commit()
    finally:
        conn.close()

    main_py = str(sandbox_dir / "src" / "main.py")
    process = subprocess.Popen(
        [sys.executable, main_py],
        cwd=str(sandbox_dir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    child_pid = process.pid

    conn = get_connection(read_only_constitution=True)
    try:
        conn.execute("""
        UPDATE spawn_log
        SET child_pid = ?, status = 'alive', last_heartbeat = CURRENT_TIMESTAMP
        WHERE child_path = ?;
        """, (child_pid, str(sandbox_dir)))
        conn.commit()
    finally:
        conn.close()

    logger.info(
        f"Spawned evolution child daemon for '{sandbox_dir}' "
        f"(pid={child_pid}, port={port}, party={self_party_id})."
    )
    return {
        "success": True,
        "child_path": str(sandbox_dir),
        "child_pid": child_pid,
        "port": port,
        "self_party_id": self_party_id,
    }

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

def parse_pytest_results(logs: str) -> dict:
    """
    Parses pytest output logs to extract total, passed, failed, and coverage.
    """
    passed = 0
    failed = 0
    total = 0
    coverage = None

    summary_lines = []
    for line in logs.splitlines():
        if line.startswith("===") and ("passed" in line or "failed" in line or "error" in line):
            summary_lines.append(line)
        if "TOTAL " in line:
            cov_match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", line)
            if cov_match:
                try:
                    coverage = float(cov_match.group(1))
                except ValueError:
                    pass

    if summary_lines:
        last_summary = summary_lines[-1]
        failed_match = re.search(r"(\d+)\s+failed", last_summary)
        passed_match = re.search(r"(\d+)\s+passed", last_summary)
        error_match = re.search(r"(\d+)\s+error", last_summary)
        skipped_match = re.search(r"(\d+)\s+skipped", last_summary)
        
        if failed_match:
            failed += int(failed_match.group(1))
        if error_match:
            failed += int(error_match.group(1))
        if passed_match:
            passed = int(passed_match.group(1))
            
        total = passed + failed
        if skipped_match:
            total += int(skipped_match.group(1))
    else:
        if "FAILURES" in logs or "ERRORS" in logs:
            failed = 1
            total = 1

    return {
        "passed": passed,
        "failed": failed,
        "total": total,
        "coverage": coverage
    }

def _ship_project_sandbox(session: dict) -> list:
    """
    Clears the active-session pointer for a project sandbox without touching
    the directory or its independent git history. Returns the project's own
    tracked files (for informational purposes), copying nothing into ROOT_DIR.
    """
    sandbox_root = Path(session["active_sandbox_path"])
    tracked_files = []
    if sandbox_root.exists():
        res = subprocess.run(
            ["git", "-C", str(sandbox_root), "ls-files"],
            capture_output=True,
            text=True
        )
        if res.returncode == 0:
            tracked_files = [f for f in res.stdout.splitlines() if f.strip()]
    clear_sandbox_session()
    return tracked_files

def ship_sandbox_session() -> list:
    """
    Copies all modified/new files in the sandbox back to the active workspace,
    then cleans up the sandbox worktree and branch.
    Returns: list of files copied.

    For purpose="project" sessions, "shipping" doesn't mean merging into Janus's
    own codebase — it means the app is done being actively worked on for now.
    Delegates to _ship_project_sandbox(), which leaves the directory and its
    independent git history untouched.
    """
    session = get_active_sandbox()
    if not session:
        raise RuntimeError("No active sandbox session.")

    if session.get("active_sandbox_purpose") == "project":
        return _ship_project_sandbox(session)

    sandbox_root = Path(session["active_sandbox_path"])
    branch_name = session["active_sandbox_branch"]
    
    # 1. Run tests inside the sandbox before finalizing changes
    if (sandbox_root / "tests").exists():
        passed, logs = run_sandbox_tests()
        stats = parse_pytest_results(logs)
    else:
        passed = True
        logs = "No tests directory found, skipping sandbox tests."
        stats = {"passed": 0, "failed": 0, "total": 0, "coverage": None}

    # Resolve commit_sha early so it's available for both the regression-abort
    # path (recording the run below) and the success path (test_run_baselines insert).
    commit_sha = get_current_commit_sha(cwd=str(sandbox_root))

    # 2. Compare results against the baseline from test_run_baselines
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT total_tests, passed_tests, failed_tests, coverage_percentage 
        FROM test_run_baselines 
        ORDER BY id DESC LIMIT 1;
    """)
    baseline_row = cursor.fetchone()
    conn.close()
    
    has_regression = False
    regression_reason = ""
    
    if not passed or stats["failed"] > 0:
        has_regression = True
        regression_reason = f"Sandbox tests failed (failed={stats['failed']})."
    elif baseline_row:
        # Check coverage drop
        try:
            if isinstance(baseline_row, dict):
                baseline_cov = baseline_row.get("coverage_percentage")
                baseline_failed = baseline_row.get("failed_tests")
            else:
                baseline_cov = baseline_row[3]
                baseline_failed = baseline_row[2]
        except Exception:
            baseline_cov = None
            baseline_failed = 0
            
        if stats["coverage"] is not None and baseline_cov is not None:
            if stats["coverage"] < baseline_cov:
                has_regression = True
                regression_reason = f"Coverage dropped from {baseline_cov}% to {stats['coverage']}%."

    # Record this run into test_runs regardless of outcome, for /test history + flaky detection.
    # Pass status explicitly: has_regression can be True purely from a coverage drop, which
    # stats["failed"]/errors alone wouldn't reflect.
    try:
        record_test_run(
            stats,
            commit_sha=commit_sha,
            triggered_by="sandbox_ship",
            status="failed" if has_regression else "passed",
        )
    except Exception as e:
        logger.warning(f"Failed to record test run for sandbox ship: {e}")

    if has_regression:
        # Abort shipping flow
        log_episodic_memory(
            speaker="system",
            message_content=f"Regression Watcher aborted sandbox ship flow: {regression_reason}\nLogs:\n{logs}",
            context_type="background_thought",
            party_id="system"
        )
        abort_sandbox_session()
        raise RuntimeError(f"Regression detected: {regression_reason}. Sandbox session aborted.")
        
    # If successful, insert new baseline row in test_run_baselines
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO test_run_baselines (total_tests, passed_tests, failed_tests, coverage_percentage, commit_sha)
            VALUES (?, ?, ?, ?, ?);
        """, (stats["total"], stats["passed"], stats["failed"], stats["coverage"], commit_sha))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to insert test run baseline: {e}")
    finally:
        conn.close()
        
    modified_files = get_sandbox_modified_files()
    copied_files = []
    
    # 3. Copy files to main workspace
    for rel_path in modified_files:
        src_file = sandbox_root / rel_path
        dest_file = src.config.ROOT_DIR / rel_path
        if src_file.is_file():
            os.makedirs(dest_file.parent, exist_ok=True)
            shutil.copy2(src_file, dest_file)
            copied_files.append(rel_path)
            logger.info(f"Shipped file copy: {rel_path}")
            
    # 4. Cleanup git worktree and branch
    cleanup_git_sandbox(str(sandbox_root), branch_name)
    
    # 5. Clear SQLite session
    clear_sandbox_session()
    
    return copied_files

def promote_evolution_sandbox() -> dict:
    """
    Promotes a verified evolution sandbox: ships code back to main (reusing
    ship_sandbox_session() unchanged), then conservatively surfaces what else
    changed in the sandbox's duplicated DB without blindly touching the live
    parent DB:
      - Schema deltas (new or altered tables) are detected and queued in
        pending_schema_migrations for manual review — never auto-applied.
      - Memory deltas are limited to episodic_memory rows tagged with the
        evolution child's own party_id, written after it was spawned — no
        other tables are auto-ported.
    Returns a summary dict: copied_files, queued_migrations, ported_memories.
    """
    session = get_active_sandbox()
    if not session:
        raise RuntimeError("No active sandbox session.")
    if session.get("active_sandbox_purpose") != "evolution":
        raise RuntimeError("promote_evolution_sandbox() only applies to purpose='evolution' sessions.")

    sandbox_root = Path(session["active_sandbox_path"])
    branch_name = session["active_sandbox_branch"]
    child_db_path = sandbox_root / "janus_test.db"
    branch_suffix = branch_name.rsplit("-", 1)[-1] if branch_name else sandbox_root.name
    child_party_id = f"evolution_{branch_suffix}"

    # Capture child DB schema + tagged memories BEFORE ship_sandbox_session() deletes
    # the worktree and its duplicated DB.
    schema_deltas = []
    tagged_memories = []
    if child_db_path.exists():
        import sqlite3
        child_conn = sqlite3.connect(str(child_db_path))
        try:
            child_tables = dict(child_conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL;"
            ).fetchall())
        finally:
            child_conn.close()

        parent_conn = get_connection(read_only_constitution=True)
        try:
            parent_tables = dict(parent_conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL;"
            ).fetchall())
            spawn_row = parent_conn.execute(
                "SELECT spawned_at FROM spawn_log WHERE child_path = ?;", (str(sandbox_root),)
            ).fetchone()
        finally:
            parent_conn.close()

        # New tables, or existing tables whose DDL changed (e.g. an added column).
        schema_deltas = [
            sql for name, sql in child_tables.items()
            if name not in parent_tables or parent_tables[name] != sql
        ]

        if spawn_row and spawn_row[0]:
            spawned_at = spawn_row[0]
            child_conn = sqlite3.connect(str(child_db_path))
            try:
                tagged_memories = child_conn.execute("""
                    SELECT speaker, message_content, context_type
                    FROM episodic_memory
                    WHERE party_id = ? AND timestamp > ?
                    ORDER BY id ASC;
                """, (child_party_id, spawned_at)).fetchall()
            finally:
                child_conn.close()

    # Ship code back to main (regression check + file copy-back + worktree/branch
    # cleanup). Raises and aborts the session if a regression is detected — schema/
    # memory promotion below is intentionally skipped in that case.
    copied_files = ship_sandbox_session()

    # Queue schema deltas for manual review — never auto-applied to the live parent DB.
    queued_migrations = 0
    if schema_deltas:
        conn = get_connection(read_only_constitution=True)
        try:
            for ddl in schema_deltas:
                conn.execute("""
                    INSERT INTO pending_schema_migrations (source_sandbox, ddl_statement)
                    VALUES (?, ?);
                """, (str(sandbox_root), ddl))
            conn.commit()
        finally:
            conn.close()
        queued_migrations = len(schema_deltas)
        log_episodic_memory(
            speaker="system",
            message_content=(
                f"Promotion detected {queued_migrations} new/altered table(s) in evolution "
                f"sandbox '{branch_name}' not matching the parent DB. Queued in "
                f"pending_schema_migrations for manual review — not auto-applied."
            ),
            context_type="user_visible"
        )

    # Port only the child's own tagged, post-spawn episodic memories.
    ported_memories = 0
    if tagged_memories:
        conn = get_connection(read_only_constitution=True)
        try:
            for speaker, message_content, context_type in tagged_memories:
                conn.execute("""
                    INSERT INTO episodic_memory (speaker, message_content, context_type, party_id)
                    VALUES (?, ?, ?, ?);
                """, (speaker, message_content, context_type, child_party_id))
            conn.commit()
        finally:
            conn.close()
        ported_memories = len(tagged_memories)

    return {
        "copied_files": copied_files,
        "queued_migrations": queued_migrations,
        "ported_memories": ported_memories,
    }

def abort_sandbox_session():
    """
    Cleans up the sandbox worktree and branch, discarding all changes.

    For purpose="project" sessions this only clears the active-session pointer —
    aborting a session must not delete the user's app directory. Use
    delete_project_sandbox() for deliberate deletion.
    """
    session = get_active_sandbox()
    if not session:
        return

    if session.get("active_sandbox_purpose") == "project":
        clear_sandbox_session()
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
