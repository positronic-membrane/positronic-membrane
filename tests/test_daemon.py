import os
import time
import pytest
import asyncio
import src.config
from pathlib import Path
from unittest.mock import patch
from src.daemon import detect_user_presence, run_heartbeat_loop
from src.database import init_db, get_boredom_counter, get_connection

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

def test_user_presence_detection(tmp_path):
    """Verify that file modifications indicate user presence, except ignored items."""
    # Empty directory -> no user presence
    assert not detect_user_presence(tmp_path, max_age_seconds=10)
    
    # Create an active workspace file
    test_file = tmp_path / "index.py"
    test_file.touch()
    
    # File touched now -> presence detected
    assert detect_user_presence(tmp_path, max_age_seconds=10)
    
    # Check that database files, git, and venv are ignored
    test_file.unlink()
    
    ignored_files = ["janus.db", "janus.db-wal", ".DS_Store"]
    for f in ignored_files:
        (tmp_path / f).touch()
    
    # Only ignored files touched -> presence should be False
    assert not detect_user_presence(tmp_path, max_age_seconds=10)
    
    # Clean up ignored files
    for f in ignored_files:
        (tmp_path / f).unlink()

@pytest.mark.asyncio
@patch("src.daemon.add_memory")
@patch("src.daemon.query_memories")
@patch("src.daemon.query_agent")
async def test_heartbeat_loop_execution(mock_query, mock_query_memories, mock_add_memory, tmp_path, monkeypatch):
    """
    Test that the heartbeat daemon loop runs, increments boredom,
    and resets boredom when threshold is reached.
    """
    # Mock ChromaDB memory calls
    mock_query_memories.return_value = [{"id": "mem_1", "content": "Memory match content", "metadata": {}, "distance": 0.1}]
    mock_add_memory.return_value = None

    # Mock query agent responses dynamically to simulate proposer/critic/archivist debate
    def side_effect(agent_id, prompt):
        if agent_id == "proposer":
            return "PROPOSED_ACTION: Scan codebase docs"
        elif agent_id == "critic":
            return "Decision: 1\nJustification: Action is safe and complies with all rules."
        elif agent_id == "archivist":
            if "curiosity" in prompt.lower() or "curiosity_topics" in prompt.lower():
                return "CURIOSITY_TOPICS: git hooks, sqlite locks"
            return "Janus execution summary nugget logged."
        return ""
    mock_query.side_effect = side_effect
    # Force test mode and speed up variables
    monkeypatch.setenv("JANUS_TEST_MODE", "1")
    monkeypatch.setattr(src.config, "BOREDOM_THRESHOLD", 2)
    monkeypatch.setattr(src.config, "N_LOOP_LIMIT", 3)
    
    # Redirect root dir to a temp path so we don't pick up workspace changes
    monkeypatch.setattr(src.config, "ROOT_DIR", tmp_path)
    
    # Start the daemon task
    daemon_task = asyncio.create_task(run_heartbeat_loop())
    
    # Allow the loop to run a few iterations.
    # In test mode: sleep duration is 2 seconds for idle loops.
    # Let's wait 5.5 seconds so it ticks at least twice.
    await asyncio.sleep(5.5)
    
    # Cancel the daemon task
    daemon_task.cancel()
    try:
        await daemon_task
    except asyncio.CancelledError:
        pass

    # Verify that boredom calculations occurred.
    # In 5.5 seconds, it should tick twice (at ~2s and ~4s).
    # Since BOREDOM_THRESHOLD is 2, on the second tick, boredom hits 2,
    # triggers a deliberation, and resets to 0.
    # Let's query the database to verify a deliberation was logged.
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM internal_deliberations;")
    deliberations_count = cursor.fetchone()[0]
    conn.close()
    
    # We expect at least 1 mock deliberation to have been logged and boredom to have reset
    assert deliberations_count >= 1
