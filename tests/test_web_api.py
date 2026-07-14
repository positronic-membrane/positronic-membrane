import json
import socket
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

import src.config
from src.database import init_db


def get_free_port():
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port

@pytest.fixture(scope="module")
def web_server():
    # Setup test DB path config
    import tempfile
    from pathlib import Path
    temp_db_dir = tempfile.mkdtemp()
    temp_db = Path(temp_db_dir) / "test_janus_web.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)

    # Disable authentication requirement during legacy API testing
    orig_require = src.config.REQUIRE_AUTH
    src.config.REQUIRE_AUTH = False

    init_db()

    port = get_free_port()
    import uvicorn

    from src.web_server import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import time
    time.sleep(0.5)

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)

    src.config.DB_PATH = orig_db_path
    src.config.REQUIRE_AUTH = orig_require
    import shutil
    shutil.rmtree(temp_db_dir, ignore_errors=True)

def test_get_history(web_server):
    url = f"{web_server}/api/history"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert isinstance(data, list)

def test_get_deliberations(web_server):
    url = f"{web_server}/api/deliberations"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert isinstance(data, list)

@patch("src.sandbox_session.get_active_sandbox")
@patch("src.sandbox_session.get_sandbox_modified_files")
def test_sandbox_status(mock_mod, mock_sb, web_server):
    mock_sb.return_value = {
        "active_sandbox_path": "/path/to/sb",
        "active_sandbox_branch": "janus/sandbox-test",
        "active_sandbox_status": "passed",
        "active_sandbox_test_logs": "all passed"
    }
    mock_mod.return_value = ["src/config.py"]

    url = f"{web_server}/api/sandbox/status"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["active"] is True
        assert data["branch"] == "janus/sandbox-test"
        assert data["modified"] == ["src/config.py"]

def test_stage_status_returns_410(web_server):
    """Verify that the /api/stage/status endpoint returns 410 Gone (V3-T3: direct modification removed)."""
    import urllib.error
    url = f"{web_server}/api/stage/status"
    req = urllib.request.Request(url, method="GET")
    try:
        urllib.request.urlopen(req)
        raise AssertionError("Expected HTTPError 410")
    except urllib.error.HTTPError as e:
        assert e.code == 410

def test_get_constitution(web_server):
    url = f"{web_server}/api/constitution"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert isinstance(data, list)

def test_get_registry(web_server):
    url = f"{web_server}/api/registry"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert isinstance(data, list)
        assert len(data) > 0
        assert any(agent["id"] == "proposer" for agent in data)

def test_constitution_amend(web_server):
    url = f"{web_server}/api/constitution/amend"
    payload = json.dumps({"key": "TEST_RULE", "text": "This is a web api boundary rule."}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["success"] is True

def test_constitution_delete(web_server):
    url_amend = f"{web_server}/api/constitution/amend"
    payload_amend = json.dumps({"key": "DELETE_ME_API", "text": "To be deleted."}).encode("utf-8")
    req_amend = urllib.request.Request(url_amend, data=payload_amend, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req_amend) as resp:
        assert resp.status == 200

    url_delete = f"{web_server}/api/constitution/delete"
    payload_delete = json.dumps({"key": "DELETE_ME_API"}).encode("utf-8")
    req_delete = urllib.request.Request(url_delete, data=payload_delete, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req_delete) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["success"] is True

def test_registry_update(web_server):
    url = f"{web_server}/api/registry/update"
    payload = json.dumps({"agent_id": "proposer", "model": "qwen2.5-coder:1.5b"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["success"] is True

@patch("src.persona.generate_persona_response_autonomous")
def test_post_chat_normal(mock_persona, web_server):
    mock_persona.return_value = "Hello from persona"
    url = f"{web_server}/api/chat"
    payload = json.dumps({"message": "Hello"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["response"] == "Hello from persona"

@patch("src.persona.handle_web_slash_command")
def test_post_chat_slash_command(mock_slash, web_server):
    async def mock_async_resp(user_msg):
        return "Command completed successfully"
    mock_slash.side_effect = mock_async_resp

    url = f"{web_server}/api/chat"
    payload = json.dumps({"message": "/sandbox status"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["response"] == "Command completed successfully"
