from unittest.mock import patch

import pytest

import src.config
import src.memory
from src.database import init_db
from src.memory import add_memory, get_collection, query_memories


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

    # Reset internal memory client cache to force re-initialization. The
    # collections cache must be cleared too — its wrappers hold collection
    # handles bound to the previous client/path.
    src.memory._chroma_client = None
    src.memory._collections = {}

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
    from src.memory import consolidate_memories
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


# --- Consolidating from test_v1_priority1.py ---

def test_episodic_memory_cleanup_ttl():
    import datetime

    from src.database import get_connection
    from src.skills import DynamicSkillExecutor

    # Clear episodic memory first
    conn = get_connection(read_only_constitution=False)
    import sqlite3
    conn.row_factory = sqlite3.Row
    conn.execute("DELETE FROM episodic_memory;")
    conn.commit()

    # Seed retention_days config
    conn.execute(
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) VALUES ('memory.retention_days', '30', 1);"
    )
    # Clear last run time to ensure it triggers
    conn.execute(
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) VALUES ('memory.last_cleanup_time', '', 1);"
    )
    conn.commit()

    # Helper function to insert memory with specific timestamp
    def insert_old_memory(speaker, content, days_ago):
        target_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days_ago)
        ts_str = target_time.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO episodic_memory (speaker, message_content, context_type, timestamp) VALUES (?, ?, 'user_visible', ?);",
            (speaker, content, ts_str)
        )
        conn.commit()

    # 1. Insert recent memory (10 days old)
    insert_old_memory("user", "Hello 10 days ago", 10)
    # 2. Insert expired memory (40 days old)
    insert_old_memory("user", "Hello 40 days ago", 40)

    # Verify both exist
    rows = conn.execute("SELECT message_content FROM episodic_memory;").fetchall()
    assert len(rows) == 2

    # Execute episodic memory cleanup skill
    res = DynamicSkillExecutor.execute("cleanup_episodic_memory", {}, party_id="system")
    assert res["success"] is True
    assert "Episodic memory cleanup complete" in res["result"]

    # Verify only recent memory remains
    rows_after = conn.execute("SELECT message_content FROM episodic_memory;").fetchall()
    assert len(rows_after) == 1
    assert rows_after[0]["message_content"] == "Hello 10 days ago"

    # Verify last_cleanup_time is now populated
    last_cleanup = conn.execute("SELECT config_value FROM system_config WHERE config_key = 'memory.last_cleanup_time';").fetchone()
    assert last_cleanup["config_value"] != ""

    # Re-run immediately: it should skip cleanup
    res_skipped = DynamicSkillExecutor.execute("cleanup_episodic_memory", {}, party_id="system")
    assert res_skipped["success"] is True
    assert "Episodic memory cleanup skipped" in res_skipped["result"]
    conn.close()



def test_add_memory_upsert_refreshes_existing_id(mock_embeddings):
    """add_memory default keeps the first write for a duplicate ID; upsert=True replaces it."""
    add_memory("old content", {"v": "1"}, "stable_id")
    add_memory("new content", {"v": "2"}, "stable_id")  # default add: duplicate ID silently skipped

    collection = get_collection("janus_long_term")
    assert collection.get(ids=["stable_id"])["documents"] == ["old content"]

    add_memory("new content", {"v": "2"}, "stable_id", upsert=True)
    res = collection.get(ids=["stable_id"])
    assert res["documents"] == ["new content"]
    assert res["metadatas"][0]["v"] == "2"
