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

def apply_staged_change(temp_dir_path: str, rel_path: str):
    """
    Copies the validated file back from the staging directory to the active workspace.
    """
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
    Copies the validated files back from the staging directory to the active workspace.
    """
    from src.config import get_effective_workspace_root
    for rel_path in modifications:
        src_file = Path(temp_dir_path) / rel_path
        dest_file = get_effective_workspace_root() / rel_path
        
        logger.info(f"Applying validated staged changes: copy '{src_file}' -> '{dest_file}'")
        os.makedirs(dest_file.parent, exist_ok=True)
        shutil.copy2(src_file, dest_file)
    logger.info("All staged changes applied successfully.")

