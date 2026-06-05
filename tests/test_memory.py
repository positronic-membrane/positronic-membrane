import pytest
from unittest.mock import patch
import src.config
import src.memory
from src.database import init_db
from src.memory import add_memory, query_memories, get_collection

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

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

@patch("src.memory.query_agent")
def test_memory_consolidation(mock_query_agent, mock_embeddings):
    """Verify detailed memories are consolidated into a high-level concept."""
    mock_query_agent.return_value = "Swarm researched database configurations."
    
    # 1. Add unconsolidated detailed memories to janus_details
    add_memory("Ran sqlite performance scan.", {"consolidated": "false"}, "detail_1", "janus_details")
    add_memory("Optimized WAL database mode.", {"consolidated": "false"}, "detail_2", "janus_details")
    
    # 2. Trigger consolidation
    from src.memory import consolidate_memories, get_collection
    consolidate_memories(batch_size=2)
    
    # 3. Verify concept added to janus_long_term
    concepts = query_memories("database configurations", limit=1, collection_name="janus_long_term")
    assert len(concepts) == 1
    assert concepts[0]["content"] == "Swarm researched database configurations."
    assert "detail_1" in concepts[0]["metadata"]["detail_ids"]
    assert "detail_2" in concepts[0]["metadata"]["detail_ids"]
    
    # 4. Verify detail records updated to consolidated = "true" in janus_details
    details_col = get_collection("janus_details")
    detail_records = details_col.get(ids=["detail_1", "detail_2"])
    assert detail_records["metadatas"][0]["consolidated"] == "true"
    assert detail_records["metadatas"][1]["consolidated"] == "true"

