from unittest.mock import patch

import pytest

import src.config
import src.memory
from src.database import get_connection, init_db
from src.persona import handle_web_slash_command


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB for testing."""
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()

    # Create test parties
    conn = get_connection(read_only_constitution=False)
    conn.execute("INSERT INTO parties (id, name, role, public_key) VALUES ('user1', 'Alice', 'user', 'key1');")
    conn.execute("INSERT INTO parties (id, name, role, public_key) VALUES ('contrib1', 'Bob', 'contributor', 'key2');")
    conn.execute("INSERT INTO parties (id, name, role, public_key) VALUES ('admin1', 'Charlie', 'admin', 'key3');")
    conn.commit()
    conn.close()

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

@pytest.mark.asyncio
async def test_skills_list_command():
    """Verify that /skills command lists active skills from database."""
    res = await handle_web_slash_command("/skills")
    assert "Active Swarm Skills" in res
    assert "`web_search`" in res
    assert "`fetch_url`" in res
    assert "`modify_code`" not in res

@pytest.mark.asyncio
async def test_runskill_command_successful_execution():
    """Verify that /runskill executes manual skill correctly using resolved party ID."""
    conn = get_connection(read_only_constitution=False)
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES ('chat_test', 'Chat Test', 'Manual test skill', '{}', 'def run(): return "success_run"', 'run', 'user');
    """)
    conn.commit()
    conn.close()

    res = await handle_web_slash_command("/runskill chat_test")
    assert "success_run" in res

@pytest.mark.asyncio
async def test_runskill_command_invalid_json():
    """Verify that /runskill validates arguments JSON structure."""
    res = await handle_web_slash_command("/runskill web_search {invalid_json")
    assert "[Error] Invalid JSON arguments" in res

@pytest.mark.asyncio
async def test_runskill_command_permission_veto():
    """Verify that /runskill respects required role configuration."""
    conn = get_connection(read_only_constitution=False)
    conn.execute("DELETE FROM parties;")
    conn.execute("INSERT INTO parties (id, name, role, public_key) VALUES ('user1', 'Alice', 'user', 'key1');")
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES ('admin_only', 'Admin', 'Admin restricted', '{}', 'def run(): return "ok"', 'run', 'admin');
    """)
    conn.commit()
    conn.close()

    res = await handle_web_slash_command("/runskill admin_only")
    assert "Security Veto" in res
