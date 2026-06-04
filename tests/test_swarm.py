import pytest
import src.config
from src.database import (
    init_db,
    get_connection,
    send_swarm_message,
    get_pending_swarm_messages,
    mark_swarm_message_processed,
    register_helper_agent,
    deactivate_helper_agent
)

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB for testing."""
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

def test_message_bus_operations():
    """Verify messages can be sent, retrieved, and processed in the SQLite message bus."""
    # 1. Send task request
    send_swarm_message("proposer", "explorer", "task_request", "Search for Git Hook security")
    
    # 2. Retrieve pending messages for explorer
    pending = get_pending_swarm_messages("explorer")
    assert len(pending) == 1
    msg_id, sender_id, msg_type, content, _ = pending[0]
    assert sender_id == "proposer"
    assert msg_type == "task_request"
    assert content == "Search for Git Hook security"
    
    # 3. Process the message
    mark_swarm_message_processed(msg_id)
    
    # 4. Verify no pending messages remain
    pending_after = get_pending_swarm_messages("explorer")
    assert len(pending_after) == 0

def test_dynamic_helper_agent_registry():
    """Verify helper agents can be dynamically registered and deactivated."""
    agent_id = "test_helper"
    name = "Test Helper Agent"
    prompt = "You are a test helper agent."
    
    # 1. Register agent
    register_helper_agent(agent_id, name, prompt)
    
    # Verify in DB
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT agent_name, system_prompt, is_active FROM agent_registry WHERE agent_id = ?;", (agent_id,))
    row = cursor.fetchone()
    conn.close()
    
    assert row is not None
    assert row[0] == name
    assert row[1] == prompt
    assert row[2] == 1
    
    # 2. Deactivate agent
    deactivate_helper_agent(agent_id)
    
    # Verify in DB
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_active FROM agent_registry WHERE agent_id = ?;", (agent_id,))
    row = cursor.fetchone()
    conn.close()
    
    assert row is not None
    assert row[0] == 0
