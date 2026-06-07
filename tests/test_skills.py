import pytest
import json
from unittest.mock import patch
import src.config
import src.memory
from src.database import init_db, get_connection
from src.skills import DynamicSkillExecutor, has_role

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
        mock_get.return_value = [[0.1] * 384]
        yield mock_get

def test_has_role():
    """Verify that role hierarchies are checked correctly."""
    assert has_role('user1', 'observer')
    assert has_role('user1', 'user')
    assert not has_role('user1', 'contributor')
    assert not has_role('user1', 'admin')

    assert has_role('contrib1', 'user')
    assert has_role('contrib1', 'contributor')
    assert not has_role('contrib1', 'admin')

    assert has_role('admin1', 'admin')
    assert has_role(None, 'observer')
    assert not has_role(None, 'user')
    assert has_role('system', 'admin')

def test_executor_basic_execution():
    """Verify that a registered dynamic skill executes successfully and isolates namespaces."""
    conn = get_connection(read_only_constitution=False)
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES (
        'test_add', 'Test Add', 'Adds two numbers', '{}',
        'def add_nums(x, y):\n    return x + y', 'add_nums', 'user'
    );
    """)
    conn.commit()
    conn.close()

    res = DynamicSkillExecutor.execute('test_add', {'x': 10, 'y': 20}, party_id='contrib1')
    assert res['success']
    assert res['result'] == 30

def test_executor_permission_veto():
    """Verify that role restrictions veto unauthorized executions."""
    conn = get_connection(read_only_constitution=False)
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES (
        'test_admin_only', 'Admin Only', 'Private skill', '{}',
        'def run():\n    return "secret"', 'run', 'admin'
    );
    """)
    conn.commit()
    conn.close()

    res = DynamicSkillExecutor.execute('test_admin_only', {}, party_id='user1')
    assert not res['success']
    assert 'Security Veto' in res['error']

    res = DynamicSkillExecutor.execute('test_admin_only', {}, party_id='admin1')
    assert res['success']
    assert res['result'] == 'secret'

def test_executor_traceback_mapping():
    """Verify that custom traceback line offset mapping works for dynamic skill errors."""
    conn = get_connection(read_only_constitution=False)
    code = (
        "def fail_skill():\n"
        "    x = 1\n"
        "    y = x / 0\n"
        "    return y"
    )
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES ('test_fail', 'Fail Skill', 'Throws division error', '{}', ?, 'fail_skill', 'user');
    """, (code,))
    conn.commit()
    conn.close()

    res = DynamicSkillExecutor.execute('test_fail', {}, party_id='user1')
    assert not res['success']
    assert "ZeroDivisionError" in res['error']
    assert "File <dynamic_skill>, line 3, in fail_skill" in res['error']
    assert "y = x / 0" in res['error']

def test_sdk_database_authorizer():
    """Verify that SafeDB.query honors read-only core_constitution authorizer."""
    conn = get_connection(read_only_constitution=False)
    code = "def write():\n    sdk['db'].query(\"DELETE FROM core_constitution WHERE rule_key = 'xxx'\")\n    return 'ok'"
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES ('db_write_test', 'DB Write Test', 'Tries to edit constitution', '{}', ?, 'write', 'contributor');
    """, (code,))
    conn.commit()
    conn.close()

    res = DynamicSkillExecutor.execute('db_write_test', {}, party_id='contrib1')
    assert not res['success']
    err_lower = res['error'].lower()
    assert any(term in err_lower for term in ("databaseaccessexception", "deny", "authorizer", "safety", "violation"))

def test_sdk_memory_isolation(mock_embeddings):
    """Verify that SafeMemory.add inserts party_id into metadata when party_id is scoped."""
    conn = get_connection(read_only_constitution=False)
    conn.execute("""
    INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
    VALUES (
        'mem_add_test', 'Memory Add Test', 'Adds memory scoped by party', '{}',
        'def run():\n    sdk["memory"].add("scoped memory content", {"tag": "test"}, "scoped_1")\n    return "done"',
        'run', 'contributor'
    );
    """)
    conn.commit()
    conn.close()

    # Bob (contrib1) running it
    res = DynamicSkillExecutor.execute('mem_add_test', {}, party_id='contrib1')
    assert res['success']

    from src.memory import get_collection
    col = get_collection("janus_long_term")
    mem_records = col.get(ids=["scoped_1"])
    assert mem_records["metadatas"][0]["party_id"] == "contrib1"
