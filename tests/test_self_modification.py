import os
import shutil
from pathlib import Path

import pytest

import src.config
from src.self_modification import apply_staged_change, generate_diff, stage_and_test

if os.environ.get("JANUS_TEST_MODE") == "1":
    pytest.skip("Skip self-modification tests during staged validation runs to avoid nested staging loops", allow_module_level=True)

@pytest.fixture(autouse=True)
def setup_test_context(tmp_path, monkeypatch):
    """Isolate workspace paths and config for self-modification testing."""
    # Create mock project structures
    project_root = tmp_path / "project_root"
    project_root.mkdir()

    src_dir = project_root / "src"
    src_dir.mkdir()

    # Create a dummy python file and dummy test file so we can run a mock pytest
    dummy_file = src_dir / "utils.py"
    dummy_file.write_text("def add(a, b): return a + b\n")

    test_dir = project_root / "tests"
    test_dir.mkdir()
    test_file = test_dir / "test_utils.py"
    test_file.write_text("from src.utils import add\ndef test_add(): assert add(1, 2) == 3\n")

    # Mock virtualenv pytest path so we don't need real virtualenv inside temp folder
    venv_bin = project_root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    pytest_bin = venv_bin / "pytest"

    # Write a simple mock shell script that runs pytest against current path
    import sys
    script_content = f"#!{sys.executable}\nimport sys, pytest\nsys.exit(pytest.main(sys.argv[1:]))\n"
    pytest_bin.write_text(script_content)
    pytest_bin.chmod(0o755)

    monkeypatch.setattr(src.config, "ROOT_DIR", project_root)
    yield project_root

def test_generate_diff():
    """Verify that generate_diff produces standard unified diff outputs."""
    rel_path = "src/utils.py"
    proposed = "def add(a, b): return a + b + 1\n"

    diff = generate_diff(rel_path, proposed)

    assert "--- a/src/utils.py" in diff
    assert "+++ b/src/utils.py" in diff
    assert "-def add(a, b): return a + b" in diff
    assert "+def add(a, b): return a + b + 1" in diff

def test_stage_and_test_passed(tmp_path):
    """Verify that stage_and_test copies codebase, updates the staged file, and runs pytest."""
    rel_path = "src/utils.py"
    proposed = "def add(a, b): return a + b\n" # Safe change that passes test

    passed, logs, temp_dir = stage_and_test(rel_path, proposed)

    assert passed
    assert "test_add" in logs
    assert os.path.exists(temp_dir)

    # Staged copy should have the proposed contents
    staged_file = Path(temp_dir) / rel_path
    assert staged_file.read_text() == proposed

    # Cleanup
    shutil.rmtree(temp_dir)

def test_stage_and_test_failed(tmp_path):
    """Verify that stage_and_test detects test failures for bad code modifications."""
    rel_path = "src/utils.py"
    proposed = "def add(a, b): return a + b + 1\n" # Change breaks test assertions

    passed, logs, temp_dir = stage_and_test(rel_path, proposed)

    assert not passed
    assert "failed" in logs.lower()

    # Cleanup
    shutil.rmtree(temp_dir)

def test_apply_staged_change(tmp_path):
    """Verify that apply_staged_change moves the staged file back to the live codebase."""
    rel_path = "src/utils.py"
    temp_dir = tmp_path / "temp_stage"
    temp_dir.mkdir()

    staged_src = temp_dir / rel_path
    staged_src.parent.mkdir(parents=True, exist_ok=True)
    staged_src.write_text("def add(a, b): return 999\n")

    apply_staged_change(str(temp_dir), rel_path)

    live_file = src.config.ROOT_DIR / rel_path
    assert live_file.read_text() == "def add(a, b): return 999\n"
