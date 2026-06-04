import pytest
from unittest.mock import patch
import src.config
import src.memory
from src.memory import add_memory, query_memories, get_collection

@pytest.fixture(autouse=True)
def setup_test_vector_db(tmp_path):
    """
    Isolates the ChromaDB persistent directory to a temporary path
    for the duration of each test.
    """
    orig_path = src.config.VECTOR_DB_PATH
    src.config.VECTOR_DB_PATH = str(tmp_path / "test_chromadb")
    
    # Reset internal memory client cache to force re-initialization
    src.memory._chroma_client = None
    src.memory._collection = None
    
    yield
    
    src.config.VECTOR_DB_PATH = orig_path

@pytest.fixture
def mock_embeddings():
    """Mock the OpenAI embeddings API endpoint to avoid network calls."""
    with patch("src.memory.get_embeddings") as mock_get:
        # Return a simple mock vector (length 1536 is standard for OpenAI, but Chroma accepts any size)
        mock_get.return_value = [[0.15] * 384]
        yield mock_get

def test_add_and_query_memory(mock_embeddings):
    """Verify that adding and querying memories in ChromaDB returns correctly formatted structures."""
    content = "Project Janus uses local SQLite transactional containers."
    metadata = {"tags": "db, architecture", "source_id": "test_1"}
    memory_id = "mem_1"
    
    # Store memory
    add_memory(content, metadata, memory_id)
    
    # Query memory
    matches = query_memories("sqlite containers", limit=1)
    
    assert len(matches) == 1
    match = matches[0]
    assert match["id"] == memory_id
    assert match["content"] == content
    assert match["metadata"]["tags"] == "db, architecture"
    assert match["metadata"]["source_id"] == "test_1"
    assert "distance" in match

def test_memory_threshold_filtering(mock_embeddings):
    """Verify that query_memories filters out matches with distance exceeding the threshold."""
    content = "Project Janus uses local SQLite transactional containers."
    add_memory(content, {"tags": "test"}, "mem_filter_test")
    
    # Threshold = 1.0 (default), should match because distance is 0.0 <= 1.0
    matches = query_memories("sqlite containers", limit=1)
    assert len(matches) == 1
    
    # Set threshold to -1.0, should filter out because distance is 0.0 > -1.0
    orig_threshold = src.config.MEMORY_RELEVANCE_THRESHOLD
    try:
        src.config.MEMORY_RELEVANCE_THRESHOLD = -1.0
        matches = query_memories("sqlite containers", limit=1)
        assert len(matches) == 0
    finally:
        src.config.MEMORY_RELEVANCE_THRESHOLD = orig_threshold
