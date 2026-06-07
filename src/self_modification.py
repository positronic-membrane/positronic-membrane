import os
import re
import shutil
import difflib
import subprocess
import tempfile
import logging
from pathlib import Path
import src.config

logger = logging.getLogger("JanusSelfModification")

def generate_diff(rel_path: str, proposed_code: str) -> str:
    """
    Generates a unified diff comparing the current file contents
    to the proposed code modifications.
    """
    from src.config import get_effective_workspace_root
    file_path = get_effective_workspace_root() / rel_path
    
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                original_code = f.read()
        except Exception as e:
            original_code = f"(Error reading original file: {e})"
    else:
        original_code = ""

    original_lines = original_code.splitlines(keepends=True)
    proposed_lines = proposed_code.splitlines(keepends=True)
    
    diff = difflib.unified_diff(
        original_lines, 
        proposed_lines, 
        fromfile=f"a/{rel_path}", 
        tofile=f"b/{rel_path}"
    )
    return "".join(diff)

def copy_project_structure(src: Path, dest: Path):
    """
    Recursively copies the source codebase to the destination staging folder,
    ignoring local databases, virtual environments, caches, and git directories.
    """
    ignored_dirs = {".git", ".venv", "venv", "__pycache__", "data", ".pytest_cache"}
    ignored_files = {".DS_Store", "janus.db", "janus.db-journal", "janus.db-wal", "janus.db-shm"}
    
    for root, dirs, files in os.walk(src):
        # Prune directories in-place
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        
        rel_dir = Path(root).relative_to(src)
        dest_dir = dest / rel_dir
        os.makedirs(dest_dir, exist_ok=True)
        
        for file in files:
            if file in ignored_files or file.endswith((".pyc", ".pyo")):
                continue
            shutil.copy2(Path(root) / file, dest_dir / file)

def stage_and_test(rel_path: str, proposed_code: str) -> tuple:
    """
    Creates a temporary directory, copies the codebase into it, applies
    the proposed code changes, and runs the unit tests.
    Returns: (tests_passed: bool, test_logs: str, temp_dir_path: str)
    """
    logger.info(f"Staging code changes for '{rel_path}'...")
    
    # Pre-flight AST validation for Python files
    if rel_path.endswith(".py"):
        valid, err_msg = validate_python_ast(proposed_code)
        if not valid:
            logger.info(f"Pre-flight AST validation failed for '{rel_path}': {err_msg}")
            return False, f"AST Verification Failed:\n{err_msg}", ""
    
    # 1. Create a persistent temporary directory for this staged change
    temp_dir = tempfile.mkdtemp(prefix="janus_stage_")
    temp_dir_path = Path(temp_dir)
    
    try:
        # 2. Copy code structure
        from src.config import get_effective_workspace_root
        copy_project_structure(get_effective_workspace_root(), temp_dir_path)
        
        # 3. Write proposed code to target staged file
        staged_file = temp_dir_path / rel_path
        os.makedirs(staged_file.parent, exist_ok=True)
        with open(staged_file, "w", encoding="utf-8") as f:
            f.write(proposed_code)
            
        logger.info(f"Staged file written. Running pytest in '{temp_dir}'...")
        
        # 4. Resolve absolute path to python/pytest
        pytest_path = str(src.config.ROOT_DIR / ".venv" / "bin" / "pytest")
        if not os.path.exists(pytest_path):
            pytest_path = "pytest"  # Fallback to path resolution
            
        # 5. Run tests inside staging directory
        # Set JANUS_TEST_MODE to 1 so the tests know they are in an isolated loop
        # Also prepend temp_dir_path to PYTHONPATH so python can resolve modules correctly
        env = os.environ.copy()
        env["JANUS_TEST_MODE"] = "1"
        
        # Copy active DB to staging folder for database isolation
        db_src = Path(src.config.DB_PATH)
        if db_src.exists() and db_src.is_file():
            try:
                shutil.copy2(db_src, temp_dir_path / "janus_test.db")
                env["DB_PATH"] = str(temp_dir_path / "janus_test.db")
            except Exception as e:
                logger.warning(f"Could not isolate database for staging: {e}")
        
        current_pythonpath = env.get("PYTHONPATH", "")
        if current_pythonpath:
            env["PYTHONPATH"] = f"{temp_dir_path}{os.pathsep}{current_pythonpath}"
        else:
            env["PYTHONPATH"] = str(temp_dir_path)
        
        result = subprocess.run(
            [pytest_path, "-v"],
            cwd=temp_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=25
        )
        
        passed = (result.returncode == 0)
        logs = result.stdout + "\n" + result.stderr
        
        logger.info(f"Staged tests finished. Passed: {passed}")
        return passed, logs, str(temp_dir_path)
        
    except subprocess.TimeoutExpired:
        logger.warning("Staged tests timed out.")
        return False, "Error: Staged test execution timed out (25s limit).", str(temp_dir_path)
    except Exception as e:
        logger.error(f"Failed during staging and test execution: {e}", exc_info=True)
        return False, f"Staging Error: {e}", str(temp_dir_path)

def push_to_github_and_open_pr(temp_dir_path: str, files: list):
    """
    Creates a new branch on git, copies staging files, stages/commits,
    pushes to GitHub remote, opens a PR, and restores workspace state.
    """
    import subprocess
    import time
    import urllib.request
    import json
    import src.config
    
    root_dir = src.config.ROOT_DIR
    logger.info(f"GITHUB_ENABLED is True. Preparing GitHub PR for changes to files: {files}")
    
    # 1. Get current branch (base branch)
    res = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=root_dir,
        capture_output=True,
        text=True
    )
    base_branch = res.stdout.strip() if res.returncode == 0 else "main"
    if not base_branch:
        base_branch = "main"
        
    branch_name = f"janus-patch-{int(time.time())}"
    logger.info(f"Base branch is '{base_branch}'. Creating temporary branch '{branch_name}'...")
    
    # 2. Checkout new temporary branch
    subprocess.run(["git", "checkout", "-b", branch_name], cwd=root_dir, capture_output=True)
    
    try:
        # 3. Copy files from temp_dir_path to active workspace
        for rel_path in files:
            src_file = Path(temp_dir_path) / rel_path
            dest_file = root_dir / rel_path
            if src_file.exists():
                os.makedirs(dest_file.parent, exist_ok=True)
                shutil.copy2(src_file, dest_file)
                # Stage file
                subprocess.run(["git", "add", rel_path], cwd=root_dir, capture_output=True)
                
        # 4. Commit changes
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "Project Janus"
        env["GIT_AUTHOR_EMAIL"] = "janus@local.net"
        env["GIT_COMMITTER_NAME"] = "Project Janus"
        env["GIT_COMMITTER_EMAIL"] = "janus@local.net"
        
        subprocess.run(
            ["git", "commit", "-m", f"Janus self-modification: updates to {', '.join(files)}"],
            cwd=root_dir,
            capture_output=True,
            env=env
        )
        
        # 5. Push to GitHub remote
        if src.config.GITHUB_ACCESS_TOKEN and src.config.GITHUB_REPO:
            push_url = f"https://x-access-token:{src.config.GITHUB_ACCESS_TOKEN}@github.com/{src.config.GITHUB_REPO}.git"
        else:
            push_url = "origin"
            
        logger.info(f"Pushing branch '{branch_name}' to remote repository...")
        push_res = subprocess.run(["git", "push", push_url, branch_name], cwd=root_dir, capture_output=True, text=True)
        if push_res.returncode != 0:
            logger.error(f"Git push failed: {push_res.stderr or push_res.stdout}")
            raise RuntimeError(f"Git push failed: {push_res.stderr or push_res.stdout}")
            
        # 6. Open Pull Request on GitHub
        if not src.config.GITHUB_REPO:
            logger.warning("GITHUB_REPO is not configured. Skipping PR creation.")
            return
            
        url = f"https://api.github.com/repos/{src.config.GITHUB_REPO}/pulls"
        data = {
            "title": f"Janus Self-Modification: updates to {', '.join(files)}",
            "body": "This Pull Request contains self-modification changes proposed and validated by Project Janus.",
            "head": branch_name,
            "base": base_branch
        }
        headers = {
            "Authorization": f"token {src.config.GITHUB_ACCESS_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "Project-Janus"
        }
        
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        try:
            with urllib.request.urlopen(req) as response:
                resp_data = json.loads(response.read().decode("utf-8"))
                logger.info(f"GitHub Pull Request created successfully: {resp_data.get('html_url')}")
        except Exception as api_err:
            logger.error(f"Failed to open GitHub Pull Request: {api_err}")
            raise api_err
            
    finally:
        # 7. Switch back to base branch and clean up workspace
        logger.info(f"Restoring workspace to branch '{base_branch}'...")
        subprocess.run(["git", "checkout", base_branch], cwd=root_dir, capture_output=True)
        # Delete local temporary branch
        subprocess.run(["git", "branch", "-D", branch_name], cwd=root_dir, capture_output=True)
        
        # Restore modified files to checkout state on base_branch, delete new files
        for rel_path in files:
            dest_file = root_dir / rel_path
            file_exists_in_base = subprocess.run(
                ["git", "cat-file", "-e", f"HEAD:{rel_path}"],
                cwd=root_dir,
                capture_output=True
            ).returncode == 0
            
            if file_exists_in_base:
                subprocess.run(["git", "checkout", "HEAD", "--", rel_path], cwd=root_dir, capture_output=True)
                logger.info(f"Restored file '{rel_path}' to original state in workspace.")
            else:
                if dest_file.exists():
                    dest_file.unlink()
                    logger.info(f"Removed temporary new file '{rel_path}' from workspace.")


def apply_staged_change(temp_dir_path: str, rel_path: str):
    """
    Copies the validated file back from the staging directory to the active workspace,
    or pushes it to GitHub and opens a Pull Request if GITHUB_ENABLED is True.
    """
    import src.config
    if getattr(src.config, "GITHUB_ENABLED", False):
        push_to_github_and_open_pr(temp_dir_path, [rel_path])
        return

    from src.config import get_effective_workspace_root
    src_file = Path(temp_dir_path) / rel_path
    dest_file = get_effective_workspace_root() / rel_path
    
    logger.info(f"Applying validated staged changes: copy '{src_file}' -> '{dest_file}'")
    os.makedirs(dest_file.parent, exist_ok=True)
    shutil.copy2(src_file, dest_file)
    logger.info("Staged change applied successfully.")

def stage_and_test_multi(modifications: dict) -> tuple:
    """
    Creates a temporary directory, copies the codebase into it, applies
    multiple proposed code changes, and runs the unit tests.
    modifications is a dict mapping rel_path -> proposed_code
    Returns: (tests_passed: bool, test_logs: str, temp_dir_path: str)
    """
    logger.info(f"Staging multi-file changes: {list(modifications.keys())}...")
    
    # Pre-flight AST validation for Python files
    ast_errors = []
    for rel_path, proposed_code in modifications.items():
        if rel_path.endswith(".py"):
            valid, err_msg = validate_python_ast(proposed_code)
            if not valid:
                ast_errors.append(f"File: {rel_path}\n{err_msg}")
                
    if ast_errors:
        combined_err = "AST Verification Failed:\n" + "\n\n".join(ast_errors)
        logger.info(f"Pre-flight AST verification failed for multi-file stage: {combined_err}")
        return False, combined_err, ""
    
    # 1. Create a persistent temporary directory for this staged change
    temp_dir = tempfile.mkdtemp(prefix="janus_stage_multi_")
    temp_dir_path = Path(temp_dir)
    
    try:
        # 2. Copy code structure
        from src.config import get_effective_workspace_root
        copy_project_structure(get_effective_workspace_root(), temp_dir_path)
        
        # 3. Write proposed code for each modified file
        for rel_path, proposed_code in modifications.items():
            staged_file = temp_dir_path / rel_path
            os.makedirs(staged_file.parent, exist_ok=True)
            with open(staged_file, "w", encoding="utf-8") as f:
                f.write(proposed_code)
            
        logger.info(f"Staged files written. Running pytest in '{temp_dir}'...")
        
        # 4. Resolve absolute path to python/pytest
        pytest_path = str(src.config.ROOT_DIR / ".venv" / "bin" / "pytest")
        if not os.path.exists(pytest_path):
            pytest_path = "pytest"  # Fallback to path resolution
            
        # 5. Run tests inside staging directory
        env = os.environ.copy()
        env["JANUS_TEST_MODE"] = "1"
        
        # Copy active DB to staging folder for database isolation
        db_src = Path(src.config.DB_PATH)
        if db_src.exists() and db_src.is_file():
            try:
                shutil.copy2(db_src, temp_dir_path / "janus_test.db")
                env["DB_PATH"] = str(temp_dir_path / "janus_test.db")
            except Exception as e:
                logger.warning(f"Could not isolate database for staging: {e}")
        
        current_pythonpath = env.get("PYTHONPATH", "")
        if current_pythonpath:
            env["PYTHONPATH"] = f"{temp_dir_path}{os.pathsep}{current_pythonpath}"
        else:
            env["PYTHONPATH"] = str(temp_dir_path)
        
        result = subprocess.run(
            [pytest_path, "-v"],
            cwd=temp_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=25
        )
        
        passed = (result.returncode == 0)
        logs = result.stdout + "\n" + result.stderr
        
        logger.info(f"Staged tests finished. Passed: {passed}")
        return passed, logs, str(temp_dir_path)
        
    except subprocess.TimeoutExpired:
        logger.warning("Staged tests timed out.")
        return False, "Error: Staged test execution timed out (25s limit).", str(temp_dir_path)
    except Exception as e:
        logger.error(f"Failed during staging and test execution: {e}", exc_info=True)
        return False, f"Staging Error: {e}", str(temp_dir_path)

def generate_multi_diff(modifications: dict) -> str:
    """
    Generates a combined unified diff comparing the current file contents
    to all proposed code modifications.
    """
    diff_parts = []
    from src.config import get_effective_workspace_root
    for rel_path, proposed_code in modifications.items():
        file_path = get_effective_workspace_root() / rel_path
        
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    original_code = f.read()
            except Exception as e:
                original_code = f"(Error reading original file: {e})"
        else:
            original_code = ""

        original_lines = original_code.splitlines(keepends=True)
        proposed_lines = proposed_code.splitlines(keepends=True)
        
        diff = difflib.unified_diff(
            original_lines, 
            proposed_lines, 
            fromfile=f"a/{rel_path}", 
            tofile=f"b/{rel_path}"
        )
        diff_parts.append("".join(diff))
        
    return "\n".join(diff_parts)

def apply_staged_multi(temp_dir_path: str, modifications: dict):
    """
    Copies the validated files back from the staging directory to the active workspace,
    or pushes them to GitHub and opens a Pull Request if GITHUB_ENABLED is True.
    """
    import src.config
    if getattr(src.config, "GITHUB_ENABLED", False):
        push_to_github_and_open_pr(temp_dir_path, list(modifications.keys()))
        return

    from src.config import get_effective_workspace_root
    for rel_path in modifications:
        src_file = Path(temp_dir_path) / rel_path
        dest_file = get_effective_workspace_root() / rel_path
        
        logger.info(f"Applying validated staged changes: copy '{src_file}' -> '{dest_file}'")
        os.makedirs(dest_file.parent, exist_ok=True)
        shutil.copy2(src_file, dest_file)
    logger.info("All staged changes applied successfully.")


def apply_search_replace_blocks(current_content: str, block_text: str) -> str:
    """
    Parses search/replace blocks from 'block_text' and applies them to 'current_content'.
    Format:
    <<<<<<< SEARCH
    [original content]
    =======
    [replacement content]
    >>>>>>> REPLACE
    """
    import re
    pattern = r"<<<<<<< SEARCH\r?\n(.*?)\r?\n=======\r?\n(.*?)\r?\n>>>>>>> REPLACE"
    blocks = re.findall(pattern, block_text, re.DOTALL)
    
    if not blocks:
        raise ValueError("Invalid search/replace block syntax. No blocks could be parsed.")
        
    updated_content = current_content
    for search_part, replace_part in blocks:
        search_norm = search_part.replace("\r\n", "\n")
        content_norm = updated_content.replace("\r\n", "\n")
        
        count = updated_content.count(search_part)
        if count == 0:
            count_norm = content_norm.count(search_norm)
            if count_norm == 0:
                raise ValueError(f"Search block not found in the target content:\n{search_part}")
            elif count_norm > 1:
                raise ValueError(f"Search block matches multiple times ({count_norm}) when normalized. Please make it more specific:\n{search_part}")
            else:
                parts = content_norm.split(search_norm, 1)
                updated_content = parts[0] + replace_part + parts[1]
        elif count > 1:
            raise ValueError(f"Search block matches multiple times ({count}) in the file. Please make it more specific:\n{search_part}")
        else:
            updated_content = updated_content.replace(search_part, replace_part, 1)
            
    return updated_content


def summarize_pytest_logs(logs: str) -> str:
    """
    Extracts only relevant test failure details and tracebacks from raw pytest output
    to prevent large logs from bloating context.
    """
    if not logs:
        return "No logs provided."
        
    lines = logs.splitlines()
    summary_parts = []
    
    in_failures = False
    current_failure = []
    
    for line in lines:
        if line.startswith("___") and line.endswith("___"):
            if current_failure:
                summary_parts.append("\n".join(current_failure))
                current_failure = []
            in_failures = True
            current_failure.append(line)
        elif line.startswith("=== FAILURES ==="):
            in_failures = True
        elif line.startswith("===") and in_failures:
            if current_failure:
                summary_parts.append("\n".join(current_failure))
                current_failure = []
            in_failures = False
        elif in_failures:
            current_failure.append(line)
            
    if current_failure:
        summary_parts.append("\n".join(current_failure))
        
    in_summary_info = False
    summary_info = []
    for line in lines:
        if "short test summary info" in line:
            in_summary_info = True
            summary_info.append(line)
        elif in_summary_info and line.startswith("==="):
            in_summary_info = False
        elif in_summary_info:
            summary_info.append(line)
            
    final_parts = []
    if summary_parts:
        final_parts.append("FAILING TEST DETAILS:\n" + "\n\n".join(summary_parts))
    if summary_info:
        final_parts.append("SHORT TEST SUMMARY INFO:\n" + "\n".join(summary_info))
        
    if not final_parts:
        truncated_raw = "\n".join(lines[-40:])
        return f"RAW TEST LOGS (TRUNCATED):\n{truncated_raw}"
        
    return "\n\n".join(final_parts)


def validate_python_ast(proposed_code: str) -> tuple[bool, str | None]:
    """
    Validates that the proposed code compiles to a valid Python AST.
    Returns (True, None) if valid, or (False, error_message) if syntax validation fails.
    """
    import ast
    try:
        ast.parse(proposed_code)
        return True, None
    except SyntaxError as e:
        error_msg = f"SyntaxError: {e.msg} on line {e.lineno}"
        if e.text:
            error_msg += f"\nCode: {e.text.strip()}"
            if e.offset is not None:
                pointer = " " * (e.offset - 1) + "^"
                error_msg += f"\n      {pointer}"
        return False, error_msg
    except Exception as e:
        return False, f"AST compilation failed: {e}"

