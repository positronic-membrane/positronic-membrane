import os
import shutil
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import src.config
from src.database import init_db, log_episodic_memory
from src.persona import get_recent_persona_messages, parse_proposed_changes
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

def test_get_recent_persona_messages():
    """Verify retrieval and concatenation of recent messages spoken by the persona."""
    log_episodic_memory("user", "Hello", "user_visible")
    log_episodic_memory("persona", "How can I help you?", "user_visible")
    log_episodic_memory("user", "Do something else", "user_visible")
    log_episodic_memory("persona", "I did something else.", "user_visible")
    
    # Test default/1 limit
    msg1 = get_recent_persona_messages(1)
    assert msg1 == "I did something else."
    
    # Test limit=2
    msg2 = get_recent_persona_messages(2)
    assert msg2 == "How can I help you?\n\nI did something else."

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

def test_regex_extract_failing_tests():
    import re
    logs = """
    ============================= test session starts ==============================
    collected 3 items
    
    tests/test_memory.py::test_add_and_query_memory FAILED
    tests/test_database.py::test_database_initialization PASSED
    tests/test_persona.py::test_detect_metacognitive_intent FAILED
    """
    failing_tests = []
    for match in re.findall(r"(?:FAILED|ERROR)\s+(tests/test_[a-zA-Z0-9_-]+\.py)|(tests/test_[a-zA-Z0-9_-]+\.py)::\S+\s+(?:FAILED|ERROR)", logs):
        failing_tests.append(match[0] or match[1])
    failing_tests = sorted(list(set(failing_tests)))
    assert failing_tests == ["tests/test_memory.py", "tests/test_persona.py"]

@pytest.mark.asyncio
@patch("src.persona.get_recent_persona_messages")
@patch("src.persona.parse_proposed_changes")
@patch("src.persona.query_agent")
@patch("src.persona.get_input")
@patch("src.self_modification.stage_and_test_multi")
@patch("src.self_modification.generate_multi_diff")
@patch("shutil.rmtree")
async def test_staging_caching_and_self_healing(
    mock_rmtree,
    mock_generate_multi_diff,
    mock_stage_and_test_multi,
    mock_get_input,
    mock_query_agent,
    mock_parse_proposed_changes,
    mock_get_recent_persona_messages,
    setup_test_workspace
):
    # 1. Setup mocks
    mock_get_recent_persona_messages.return_value = "Modify src/calc.py"
    mock_parse_proposed_changes.return_value = {
        "src/calc.py": "def add(a, b): return a + b\n"
    }
    
    # query_agent mock behavior:
    # First, it gets called with "critic" to audit src/calc.py (approved).
    # Then proposer for self-healing tests/test_calc.py.
    # Then critic to audit tests/test_calc.py (approved).
    def mock_query_agent_side_effect(agent_name, prompt, **kwargs):
        if agent_name == "critic":
            return "CRITIC_DECISION: APPROVED | Justification: Looks good."
        elif agent_name == "proposer":
            return "def test_add(): assert add(2, 3) == 5\n"
        return "mock response"
    mock_query_agent.side_effect = mock_query_agent_side_effect
    
    # get_input mock behavior:
    # 1. "User >> ": "/stage" -> starts staging
    # 2. "Selection >> ": "y" -> stages and runs tests (which will fail)
    # 3. "Pre-existing test file(s) failed...": "y" -> confirms self-healing
    # 4. "Selection >> ": "y" -> runs audit again (with cache)
    # 5. "Approve and commit...": "n" -> aborts
    # 6. "User >> ": "/exit" -> exits chat
    inputs = [
        "/stage",
        "y",
        "y",
        "y",
        "n",
        "/exit"
    ]
    def mock_get_input_side_effect(prompt):
        if inputs:
            return inputs.pop(0)
        return "/exit"
    mock_get_input.side_effect = mock_get_input_side_effect
    
    # stage_and_test_multi mock behavior:
    # First call: fails with tests/test_calc.py failing
    # Second call: passes
    call_count = 0
    def mock_stage_and_test_side_effect(proposed_mods):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return False, "tests/test_calc.py::test_add FAILED", "dummy_temp_dir"
        return True, "All passed", "dummy_temp_dir"
    mock_stage_and_test_multi.side_effect = mock_stage_and_test_side_effect
    
    mock_generate_multi_diff.return_value = "dummy diff"
    
    # Write a dummy test file in workspace so self-healing can read it
    test_file_path = setup_test_workspace / "tests" / "test_calc.py"
    test_file_path.write_text("def test_add(): assert False\n")
    
    # Run the chat loop
    from src.persona import run_persona_chat
    await run_persona_chat()
    
    # Assertions
    # 1. stage_and_test_multi was called twice
    assert mock_stage_and_test_multi.call_count == 2
    
    # 2. query_agent critic was called for src/calc.py and tests/test_calc.py,
    # but NOT twice for src/calc.py (due to caching)
    critic_calls = [
        call for call in mock_query_agent.call_args_list 
        if call[0][0] == "critic"
    ]
    # Total critic calls should be 2:
    # Call 1: src/calc.py (first selection 'y')
    # Call 2: tests/test_calc.py (second selection 'y', after self-healing added it and invalidated cache)
    assert len(critic_calls) == 2
    assert "src/calc.py" in critic_calls[0][0][1]
    assert "tests/test_calc.py" in critic_calls[1][0][1]
    
    # 3. self-healed file was added to proposed_mods
    # The last call to stage_and_test_multi should have both src/calc.py and tests/test_calc.py
    last_mods_tested = mock_stage_and_test_multi.call_args_list[-1][0][0]
    assert "src/calc.py" in last_mods_tested
    assert "tests/test_calc.py" in last_mods_tested
    assert last_mods_tested["tests/test_calc.py"] == "def test_add(): assert add(2, 3) == 5\n"


@pytest.mark.asyncio
@patch("src.persona.get_recent_persona_messages")
@patch("src.persona.parse_proposed_changes")
@patch("src.persona.get_input")
@patch("src.persona.query_agent")
async def test_stage_with_limit_argument(
    mock_query_agent,
    mock_get_input,
    mock_parse_proposed_changes,
    mock_get_recent_persona_messages,
    setup_test_workspace
):
    """Verify that /stage with a limit argument calls get_recent_persona_messages with that limit."""
    mock_get_recent_persona_messages.return_value = "Modify src/calc.py"
    mock_parse_proposed_changes.return_value = {}  # Trigger early abort
    
    # Input is /stage 3, then exit
    inputs = ["/stage 3", "/exit"]
    def mock_get_input_side_effect(prompt):
        if inputs:
            return inputs.pop(0)
        return "/exit"
    mock_get_input.side_effect = mock_get_input_side_effect
    
    from src.persona import run_persona_chat
    await run_persona_chat()
    
    mock_get_recent_persona_messages.assert_called_with(3)


@pytest.mark.asyncio
@patch("src.persona.get_recent_persona_messages")
@patch("src.persona.get_input")
async def test_stage_with_invalid_limit_argument(
    mock_get_input,
    mock_get_recent_persona_messages,
    setup_test_workspace
):
    """Verify that /stage with an invalid argument prints an error and doesn't query messages."""
    inputs = ["/stage -5", "/stage abc", "/exit"]
    def mock_get_input_side_effect(prompt):
        if inputs:
            return inputs.pop(0)
        return "/exit"
    mock_get_input.side_effect = mock_get_input_side_effect
    
    from src.persona import run_persona_chat
    await run_persona_chat()
    
    mock_get_recent_persona_messages.assert_not_called()

