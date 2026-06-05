import os
import json
import socket
import threading
import urllib.request
import urllib.error
import pytest
from unittest.mock import MagicMock, patch
from http.server import ThreadingHTTPServer

import src.config
from src.database import (
    init_db,
    get_connection,
    get_agent_rules,
    get_all_agent_rules,
    add_agent_rule,
    toggle_agent_rule,
    delete_agent_rule
)
from src.llm import query_agent
from src.web_server import JanusRequestHandler

def get_free_port():
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port

@pytest.fixture
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus_rules.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

@pytest.fixture(scope="module")
def web_server():
    # Setup test DB path config
    import tempfile
    from pathlib import Path
    temp_db_dir = tempfile.mkdtemp()
    temp_db = Path(temp_db_dir) / "test_janus_rules_web.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    
    init_db()
    
    port = get_free_port()
    server = ThreadingHTTPServer(("localhost", port), JanusRequestHandler)
    
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    
    yield f"http://localhost:{port}"
    
    server.shutdown()
    server.server_close()
    thread.join()
    
    src.config.DB_PATH = orig_db_path
    import shutil
    shutil.rmtree(temp_db_dir, ignore_errors=True)

def test_default_seeded_rule(setup_test_db):
    """Verify that the database seeds the default persona and proposer rules successfully."""
    persona_rules = get_agent_rules("persona")
    assert len(persona_rules) == 1
    assert persona_rules[0]["key"] == "verify_live_codebase"
    assert "Always check the live code base" in persona_rules[0]["text"]

    proposer_rules = get_agent_rules("proposer")
    assert len(proposer_rules) == 3
    keys = {r["key"] for r in proposer_rules}
    assert keys == {"verify_file_existence", "strict_tool_syntax", "dependency_check"}

def test_agent_rules_crud_helpers(setup_test_db):
    """Verify database CRUD functions for agent rules."""
    # Create
    add_agent_rule("proposer", "test_rule_1", "This is rule text 1.")
    add_agent_rule("proposer", "test_rule_2", "This is rule text 2.")
    
    rules = get_agent_rules("proposer")
    # 3 seeded proposer rules + 2 added proposer rules = 5
    assert len(rules) == 5
    
    # Read All
    all_rules = get_all_agent_rules()
    # 1 seeded persona + 3 seeded proposer + 2 added proposer = 6
    assert len(all_rules) >= 6
    
    # Toggle (deactivate)
    toggle_agent_rule("test_rule_1", False)
    rules_after_toggle = get_agent_rules("proposer")
    assert len(rules_after_toggle) == 4
    
    # Toggle (reactivate)
    toggle_agent_rule("test_rule_1", True)
    rules_after_reactivate = get_agent_rules("proposer")
    assert len(rules_after_reactivate) == 5
    
    # Delete
    delete_agent_rule("test_rule_1")
    rules_after_delete = get_agent_rules("proposer")
    assert len(rules_after_delete) == 4

@patch("src.llm.OpenAI")
def test_query_agent_compiles_rules(mock_openai_class, setup_test_db):
    """Verify that query_agent compiles and appends active rules into the system prompt."""
    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client
    
    mock_choice = MagicMock()
    mock_choice.message.content = "Action response"
    mock_client.chat.completions.create.return_value.choices = [mock_choice]
    
    # Add rules for proposer
    add_agent_rule("proposer", "guideline_a", "Guideline A text.")
    add_agent_rule("proposer", "guideline_b", "Guideline B text.")
    
    resp = query_agent("proposer", "Trigger step")
    assert resp == "Action response"
    
    # Verify the parameters passed to chat completions
    mock_client.chat.completions.create.assert_called_once()
    kwargs = mock_client.chat.completions.create.call_args[1]
    
    system_prompt = kwargs["messages"][0]["content"]
    assert "You are the Proposer" in system_prompt
    assert "### Rules & Guidelines:" in system_prompt
    assert "- Guideline A text." in system_prompt
    assert "- Guideline B text." in system_prompt

def test_api_get_rules(web_server):
    """Verify that GET /api/registry/rules retrieves all rules."""
    url = f"{web_server}/api/registry/rules"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert isinstance(data, list)
        # Verify default persona rule is there
        persona_rule = next((r for r in data if r["key"] == "verify_live_codebase"), None)
        assert persona_rule is not None
        assert persona_rule["agent_id"] == "persona"
        assert persona_rule["is_active"] is True

def test_api_rules_update(web_server):
    """Verify that POST /api/registry/rules/update executes add, toggle, and delete correctly."""
    url = f"{web_server}/api/registry/rules/update"
    
    # 1. Add rule
    payload_add = {
        "action": "add",
        "agent_id": "critic",
        "rule_key": "critic_test_rule",
        "rule_text": "Verify performance stats."
    }
    req_add = urllib.request.Request(
        url,
        data=json.dumps(payload_add).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req_add) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["success"] is True

    # Check rule was added
    url_get = f"{web_server}/api/registry/rules"
    with urllib.request.urlopen(url_get) as resp:
        rules = json.loads(resp.read().decode("utf-8"))
        rule = next((r for r in rules if r["key"] == "critic_test_rule"), None)
        assert rule is not None
        assert rule["agent_id"] == "critic"
        assert rule["is_active"] is True

    # 2. Toggle rule
    payload_toggle = {
        "action": "toggle",
        "rule_key": "critic_test_rule",
        "is_active": False
    }
    req_toggle = urllib.request.Request(
        url,
        data=json.dumps(payload_toggle).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req_toggle) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["success"] is True

    # Check rule was toggled off
    with urllib.request.urlopen(url_get) as resp:
        rules = json.loads(resp.read().decode("utf-8"))
        rule = next((r for r in rules if r["key"] == "critic_test_rule"), None)
        assert rule is not None
        assert rule["is_active"] is False

    # 3. Delete rule
    payload_delete = {
        "action": "delete",
        "rule_key": "critic_test_rule"
    }
    req_delete = urllib.request.Request(
        url,
        data=json.dumps(payload_delete).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req_delete) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["success"] is True

    # Check rule was deleted
    with urllib.request.urlopen(url_get) as resp:
        rules = json.loads(resp.read().decode("utf-8"))
        rule = next((r for r in rules if r["key"] == "critic_test_rule"), None)
        assert rule is None
