import pytest
import json
from unittest.mock import patch, MagicMock
import src.config
import src.memory
from src.database import init_db, get_connection
from src.daemon import parse_action, run_interval_skills, _last_executed_intervals

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB for testing."""
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

@pytest.fixture(autouse=True)
def setup_test_vector_db(tmp_path):
    """Isolates the ChromaDB persistent directory."""
    orig_path = src.config.VECTOR_DB_PATH
    src.config.VECTOR_DB_PATH = str(tmp_path / "test_chromadb")
    src.memory._chroma_client = None
    src.memory._collections = {}
    yield
    src.config.VECTOR_DB_PATH = orig_path

@pytest.fixture
def mock_embeddings():
    """Mock OpenAI embeddings endpoint."""
    with patch("src.memory.get_embeddings") as mock_get:
        mock_get.return_value = [[0.15] * 384]
        yield mock_get

def test_parse_action_json():
    """Test parse_action with valid and fallback JSON formats."""
    # 1. Straight JSON
    s, args, err = parse_action('{"skill_id": "web_search", "arguments": {"query": "test"}}')
    assert s == "web_search"
    assert args == {"query": "test"}
    assert err is None

    # 2. Wrapped in markdown code fence
    s, args, err = parse_action('```json\n{"tool": "modify_code", "args": {"path": "x.py"}}\n```')
    assert s == "modify_code"
    assert args == {"path": "x.py"}
    assert err is None

    # 3. Outer text + JSON block
    s, args, err = parse_action('I will execute: {"tool_name": "scan_workspace", "arguments": {}}')
    assert s == "scan_workspace"
    assert args == {}
    assert err is None

def test_parse_action_legacy_and_mock():
    """Test parse_action with legacy format statements and generic mock fallback."""
    # 1. Legacy web_search
    s, args, err = parse_action("web_search: positronic membrane")
    assert s == "web_search"
    assert args == {"query": "positronic membrane"}
    assert err is None

    # 2. Legacy modify_code
    s, args, err = parse_action("modify_code: src/main.py | print('hello')")
    assert s == "modify_code"
    assert args == {"rel_path": "src/main.py", "proposed_code": "print('hello')"}
    assert err is None

    # 2b. Legacy drafts and document memory tools
    s, args, err = parse_action("write_draft_file: notes.md | hello world")
    assert s == "write_draft_file"
    assert args == {"filename": "notes.md", "content": "hello world"}
    assert err is None

    s, args, err = parse_action("read_draft_file: notes.md")
    assert s == "read_draft_file"
    assert args == {"filename": "notes.md"}
    assert err is None

    s, args, err = parse_action("list_draft_files")
    assert s == "list_draft_files"
    assert args == {}
    assert err is None

    s, args, err = parse_action("commit_draft_to_db: notes.md | My Title")
    assert s == "commit_draft_to_db"
    assert args == {"filename": "notes.md", "doc_title": "My Title"}
    assert err is None

    s, args, err = parse_action("checkout_db_to_draft: My Title | notes.md")
    assert s == "checkout_db_to_draft"
    assert args == {"doc_title": "My Title", "filename": "notes.md"}
    assert err is None

    s, args, err = parse_action("document_memory: get | My Title")
    assert s == "document_memory"
    assert args == {"action": "get", "title": "My Title"}
    assert err is None

    s, args, err = parse_action("document_memory: list")
    assert s == "document_memory"
    assert args == {"action": "list", "tag_filter": None}
    assert err is None

    s, args, err = parse_action("document_memory: list | my-tag")
    assert s == "document_memory"
    assert args == {"action": "list", "tag_filter": "my-tag"}
    assert err is None

    # 3. Malformed tool keyword
    s, args, err = parse_action("modify_code without separator or arguments")
    assert s is None
    assert "uses incorrect syntax" in err

    # 3b. Malformed JSON block containing tool keywords
    s, args, err = parse_action('I will execute: {"skill_id": "web_search", "arguments": "query": "test"}')
    assert s is None
    assert "Failed to parse JSON action block" in err

    # 4. Completely random mock action
    s, args, err = parse_action("Sing a beautiful song about robots")
    assert s is None
    assert "Action successfully run" in err

@patch("src.memory.query_memories")
def test_llm_prompt_retrieval(mock_query):
    """Verify query_agent retrieves semantic skills when proposer or explorer is queried."""
    from src.llm import query_agent
    mock_query.return_value = [
        {"id": "test_skill", "content": "Skill: Test\nDescription: info\nParameters Schema: {}", "metadata": {}, "distance": 0.1}
    ]
    with patch("src.llm.OpenAI") as mock_openai:
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "PROPOSED_ACTION: test_skill"
        mock_client.chat.completions.create.return_value = mock_resp

        query_agent("proposer", "Build a new project feature")
        mock_query.assert_called_with("Build a new project feature", limit=5, collection_name="janus_skills")

@patch("src.skills.DynamicSkillExecutor.execute")
def test_run_interval_skills(mock_execute):
    """Verify that run_interval_skills detects and executes elapsed interval tasks."""
    conn = get_connection(read_only_constitution=False)
    conn.execute("DELETE FROM agent_skills;")
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role, trigger_type, trigger_config)
    VALUES ('cron_test', 'Cron Test', 'Interval check', '{}', 'def run(): pass', 'run', 'contributor', 'interval', '{"interval_seconds": 120}');
    """)
    conn.commit()
    conn.close()

    mock_execute.return_value = {"success": True, "result": "cron ok"}
    _last_executed_intervals.clear()

    # First run: should trigger (test mode scales interval down)
    run_interval_skills()
    assert mock_execute.call_count == 1
    mock_execute.assert_called_with("cron_test", {}, party_id="system")

    # Second run: should not trigger (interval not elapsed yet)
    run_interval_skills()
    assert mock_execute.call_count == 1
