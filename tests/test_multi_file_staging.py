import os
import shutil
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import src.config
from src.database import init_db, log_episodic_memory
from src.persona import get_last_persona_message, parse_proposed_changes
from src.self_modification import (
    stage_and_test_multi,
    generate_multi_diff,
    apply_staged_multi
)

if os.environ.get("JANUS_TEST_MODE") == "1":
    pytest.skip("Skip self-modification tests during staged validation runs to avoid nested staging loops", allow_module_level=True)

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus_multi.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

@pytest.fixture
def setup_test_workspace(tmp_path):
    """Isolate project root config for staging/testing."""
    project_root = tmp_path / "project_root"
    project_root.mkdir()
    
    src_dir = project_root / "src"
    src_dir.mkdir()
    
    tests_dir = project_root / "tests"
    tests_dir.mkdir()
    
    # Create dummy files
    dummy_file = src_dir / "calc.py"
    dummy_file.write_text("def multiply(a, b): return a * b\n")
    
    dummy_test = tests_dir / "test_calc.py"
    dummy_test.write_text("from src.calc import multiply\ndef test_multiply(): assert multiply(2, 3) == 6\n")
    
    orig_root = src.config.ROOT_DIR
    src.config.ROOT_DIR = project_root
    yield project_root
    src.config.ROOT_DIR = orig_root

def test_get_last_persona_message():
    """Verify retrieval of the last message spoken by the persona."""
    log_episodic_memory("user", "Hello", "user_visible")
    log_episodic_memory("persona", "How can I help you?", "user_visible")
    log_episodic_memory("user", "Do something else", "user_visible")
    log_episodic_memory("persona", "I did something else.", "user_visible")
    
    msg = get_last_persona_message()
    assert msg == "I did something else."

@patch("src.persona.query_agent")
def test_parse_proposed_changes(mock_query, setup_test_workspace):
    """Verify parse_proposed_changes correctly requests and parses proposed changes as JSON."""
    mock_query.return_value = """
    {
      "files": {
        "src/calc.py": "def multiply(a, b): return a * b\\ndef add(a, b): return a + b\\n",
        "tests/test_calc.py": "from src.calc import multiply, add\\ndef test_multiply(): assert multiply(2, 3) == 6\\ndef test_add(): assert add(2, 3) == 5\\n"
      }
    }
    """
    
    msg_content = "Please modify src/calc.py to add an add function and update tests/test_calc.py"
    changes = parse_proposed_changes(msg_content)
    
    assert "src/calc.py" in changes
    assert "tests/test_calc.py" in changes
    assert "def add(a, b):" in changes["src/calc.py"]
    assert "def test_add():" in changes["tests/test_calc.py"]
    mock_query.assert_called_once()

def test_generate_multi_diff(setup_test_workspace):
    """Verify generate_multi_diff correctly produces a unified diff for multiple changes."""
    modifications = {
        "src/calc.py": "def multiply(a, b): return a * b\n# added comment\n",
        "src/new_file.py": "def hello(): print('hello')\n"
    }
    diff = generate_multi_diff(modifications)
    
    assert "a/src/calc.py" in diff
    assert "b/src/calc.py" in diff
    assert "+# added comment" in diff
    assert "a/src/new_file.py" in diff
    assert "b/src/new_file.py" in diff
    assert "+def hello(): print('hello')" in diff

def test_stage_and_test_multi(setup_test_workspace):
    """Verify stage_and_test_multi successfully stages multiple files and executes pytest."""
    # Write a failing change first
    modifications = {
        "src/calc.py": "def multiply(a, b): return a * b + 10\n", # will break existing test
    }
    
    passed, logs, temp_dir = stage_and_test_multi(modifications)
    assert not passed
    assert temp_dir is not None
    assert Path(temp_dir).exists()
    shutil.rmtree(temp_dir)
    
    # Write a passing change
    modifications = {
        "src/calc.py": "def multiply(a, b): return a * b\ndef add(a, b): return a + b\n",
        "tests/test_calc.py": "from src.calc import multiply, add\ndef test_multiply(): assert multiply(2, 3) == 6\ndef test_add(): assert add(2, 3) == 5\n"
    }
    
    passed, logs, temp_dir = stage_and_test_multi(modifications)
    assert passed
    assert Path(temp_dir).exists()
    shutil.rmtree(temp_dir)

def test_apply_staged_multi(setup_test_workspace):
    """Verify apply_staged_multi accurately copies files back from staging folder."""
    # Generate temp folder with changes manually
    staging_dir = setup_test_workspace / "dummy_staging"
    staging_dir.mkdir()
    
    staged_src = staging_dir / "src"
    staged_src.mkdir()
    staged_file = staged_src / "calc.py"
    staged_file.write_text("def multiply(a, b): return a * b * 10\n")
    
    modifications = {
        "src/calc.py": "def multiply(a, b): return a * b * 10\n"
    }
    
    apply_staged_multi(str(staging_dir), modifications)
    
    # Verify change was written back
    calc_path = setup_test_workspace / "src" / "calc.py"
    assert calc_path.read_text() == "def multiply(a, b): return a * b * 10\n"
    shutil.rmtree(staging_dir)
