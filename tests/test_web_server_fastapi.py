import pytest
import json
import uuid
import sqlite3
from datetime import datetime, UTC
from fastapi.testclient import TestClient

import src.config
from src.database import init_db, get_connection
from src.web_server import app
from src.auth import create_access_token

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolates the DB path for FastAPI web server tests."""
    temp_db = tmp_path / "test_janus_fastapi.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    
    init_db()
    
    # Insert test users
    conn = get_connection()
    now = datetime.now(UTC).isoformat()
    # Admin user
    conn.execute(
        "INSERT INTO parties (id, name, role, created_at, last_seen) VALUES (?, ?, ?, ?, ?)",
        ("admin-uuid", "Alice", "admin", now, now)
    )
    # Standard user
    conn.execute(
        "INSERT INTO parties (id, name, role, created_at, last_seen) VALUES (?, ?, ?, ?, ?)",
        ("user-uuid", "Bob", "user", now, now)
    )
    conn.commit()
    conn.close()
    
    yield
    src.config.DB_PATH = orig_db_path


def test_token_generation():
    """Verify that exchanging party_id returns a valid JWT access token."""
    client = TestClient(app)
    # Invalid ID
    resp = client.post("/api/v1/auth/token", json={"party_id": "invalid-uuid"})
    assert resp.status_code == 404
    
    # Valid User ID
    resp = client.post("/api/v1/auth/token", json={"party_id": "user-uuid"})
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_jwt_authentication_protection():
    """Verify that multi-party v1 routes enforce authentication."""
    client = TestClient(app)
    
    # GET /api/v1/party/admin-uuid with no headers -> fallback to local_user (which has admin role) -> succeeds
    resp = client.get("/api/v1/party/admin-uuid")
    assert resp.status_code == 200
    
    # GET /api/v1/party/admin-uuid with invalid JWT -> fails
    resp = client.get(
        "/api/v1/party/admin-uuid",
        headers={"Authorization": "Bearer invalidtoken"}
    )
    assert resp.status_code == 401
    
    # POST /api/v1/party/register with standard user JWT (insufficient permissions) -> fails
    token = create_access_token("user-uuid", "user")
    resp = client.post(
        "/api/v1/party/register",
        json={"name": "Charlie", "role": "user"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403
    
    # POST /api/v1/party/register with admin JWT -> succeeds
    admin_token = create_access_token("admin-uuid", "admin")
    resp = client.post(
        "/api/v1/party/register",
        json={"name": "Charlie", "role": "user"},
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "Charlie"


def test_local_admin_fallback():
    """Verify that legacy endpoints allow access without headers using the local admin fallback."""
    client = TestClient(app)
    
    # GET /api/history
    resp = client.get("/api/history")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    
    # GET /api/deliberations
    resp = client.get("/api/deliberations")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_websocket_deliberations():
    """Verify that /ws/deliberations WebSocket connects and polls internal deliberations."""
    client = TestClient(app)
    
    # Write a dummy deliberation to verify polling picks it up
    conn = get_connection()
    conn.execute(
        "INSERT INTO internal_deliberations (proposed_action, agent_debate_json, critic_decision, utility_score, justification) "
        "VALUES (?, ?, ?, ?, ?)",
        ("test_action", "{}", 1, 0.95, "Looks good")
    )
    conn.commit()
    conn.close()
    
    with client.websocket_connect("/ws/deliberations") as ws:
        # Should receive the newly added deliberation as json
        data = ws.receive_json()
        assert data["action"] == "test_action"
        assert data["decision"] == 1
        assert data["utility"] == 0.95
        assert data["justification"] == "Looks good"


def test_websocket_chat():
    """Verify WebSocket chat endpoint thinking and response cycles."""
    client = TestClient(app)
    
    # Mock generating response
    from unittest.mock import patch
    with patch("src.web_server.generate_persona_response_autonomous") as mock_persona:
        mock_persona.return_value = "Response from agent!"
        
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"message": "Hello Janus"})
            
            # First should be thinking event
            evt1 = ws.receive_json()
            assert evt1["event"] == "thinking"
            
            # Second should be response event
            evt2 = ws.receive_json()
            assert evt2["event"] == "response"
            assert evt2["message"] == "Response from agent!"
            
        mock_persona.assert_called_once_with("Hello Janus")
