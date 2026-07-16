from unittest.mock import patch

import pytest

import src.config
import src.memory
from src.codebase import index_codebase, parse_python_structure, query_codebase_context


@pytest.fixture(autouse=True)
def setup_test_vector_db(tmp_path):
    """Isolate vector db directories for codebase tests."""
    orig_path = src.config.VECTOR_DB_PATH
    src.config.VECTOR_DB_PATH = str(tmp_path / "test_chromadb_codebase")
    src.memory._chroma_client = None
    src.memory._collections = {}
    yield
    src.config.VECTOR_DB_PATH = orig_path

@pytest.fixture
def mock_embeddings():
    """Mock OpenAI embeddings to run locally."""
    with patch("src.memory.get_embeddings") as mock_get:
        mock_get.return_value = [[0.12] * 384]
        yield mock_get

def test_parse_python_structure():
    """Verify class methods and top-level functions are correctly parsed using AST."""
    code = """
'''Module docstring.'''
class DatabaseManager:
    '''Manages database connection.'''
    def __init__(self, db_path):
        '''Initialize connection.'''
        pass

    def query(self, sql):
        pass

def init_db():
    '''Initializes database.'''
    pass
"""
    result = parse_python_structure(code)

    assert "Module docstring." in result
    assert "class DatabaseManager" in result
    assert "def __init__(self, db_path)" in result
    assert "def query(self, sql)" in result
    assert "def init_db()" in result

def test_index_and_query_codebase(tmp_path, mock_embeddings, monkeypatch):
    """Verify index_codebase walks workspace, indexes files, and query_codebase_context returns context."""
    # Create temp project structure
    project_root = tmp_path / "project"
    project_root.mkdir()

    src_dir = project_root / "src"
    src_dir.mkdir()

    file_py = src_dir / "utils.py"
    file_py.write_text("def helper_func(): pass")

    file_md = project_root / "README.md"
    file_md.write_text("# Readme content text")

    # Ignore folder that should be bypassed
    venv_dir = project_root / ".venv"
    venv_dir.mkdir()
    file_ignored = venv_dir / "lib.py"
    file_ignored.write_text("def should_ignore(): pass")

    # Configure config and run indexing
    monkeypatch.setattr(src.config, "ROOT_DIR", project_root)

    index_codebase(workspace_dir=project_root)

    # Query context
    context = query_codebase_context("helper_func", limit=2)

    assert "File: src/utils.py" in context
    assert "def helper_func()" in context

    # Check that ignored paths were not indexed
    context_ignored = query_codebase_context("should_ignore", limit=5)
    assert "should_ignore" not in context_ignored

def test_reindex_refreshes_changed_file(tmp_path, mock_embeddings, monkeypatch):
    """Verify a second index_codebase run replaces the stored summary of a changed file (issue #134)."""
    project_root = tmp_path / "project"
    src_dir = project_root / "src"
    src_dir.mkdir(parents=True)

    target = src_dir / "crypto.py"
    target.write_text('def encrypt_api_key(key):\n    """Encrypts an API key using XOR."""\n')

    monkeypatch.setattr(src.config, "ROOT_DIR", project_root)
    index_codebase(workspace_dir=project_root)

    collection = src.memory.get_collection("janus_codebase")
    doc = collection.get(ids=["code_src_crypto.py"])["documents"][0]
    assert "XOR" in doc

    target.write_text('def encrypt_api_key(key):\n    """Encrypts an API key with Fernet."""\n')
    index_codebase(workspace_dir=project_root)

    doc = collection.get(ids=["code_src_crypto.py"])["documents"][0]
    assert "Fernet" in doc
    assert "XOR" not in doc

def test_reindex_prunes_deleted_files(tmp_path, mock_embeddings, monkeypatch):
    """Verify index entries for files removed from the workspace are deleted on re-index (issue #134)."""
    project_root = tmp_path / "project"
    src_dir = project_root / "src"
    src_dir.mkdir(parents=True)

    keeper = src_dir / "keeper.py"
    keeper.write_text("def keep_me(): pass")
    goner = src_dir / "goner.py"
    goner.write_text("def delete_me(): pass")

    monkeypatch.setattr(src.config, "ROOT_DIR", project_root)
    index_codebase(workspace_dir=project_root)

    collection = src.memory.get_collection("janus_codebase")
    assert set(collection.get(ids=["code_src_keeper.py", "code_src_goner.py"])["ids"]) == {
        "code_src_keeper.py", "code_src_goner.py"
    }

    goner.unlink()
    index_codebase(workspace_dir=project_root)

    remaining = collection.get()["ids"]
    assert "code_src_goner.py" not in remaining
    assert "code_src_keeper.py" in remaining
