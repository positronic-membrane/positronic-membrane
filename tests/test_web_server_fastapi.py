from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

import src.config
from src.auth import create_access_token
from src.database import get_connection, init_db
from src.web_server import app


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolates the DB path for FastAPI web server tests."""
    temp_db = tmp_path / "test_janus_fastapi.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)

    init_db()

    # Insert test users with enrollment keys (public_key)
    conn = get_connection()
    now = datetime.now(UTC).isoformat()
    # Admin user
    conn.execute(
        "INSERT INTO parties (id, name, role, created_at, last_seen, public_key) VALUES (?, ?, ?, ?, ?, ?)",
        ("admin-uuid", "Alice", "admin", now, now, "admin-key")
    )
    # Standard user
    conn.execute(
        "INSERT INTO parties (id, name, role, created_at, last_seen, public_key) VALUES (?, ?, ?, ?, ?, ?)",
        ("user-uuid", "Bob", "user", now, now, "user-key")
    )
    conn.commit()
    conn.close()

    yield
    src.config.DB_PATH = orig_db_path


def test_token_generation():
    """Verify that exchanging party_id/username and key returns a valid JWT access token."""
    client = TestClient(app)
    # Invalid ID
    resp = client.post("/api/v1/auth/token", json={"username_or_id": "invalid-uuid", "enrollment_key": "user-key"})
    assert resp.status_code == 401

    # Valid User ID but wrong key
    resp = client.post("/api/v1/auth/token", json={"username_or_id": "user-uuid", "enrollment_key": "wrong-key"})
    assert resp.status_code == 401

    # Valid User ID and key
    resp = client.post("/api/v1/auth/token", json={"username_or_id": "user-uuid", "enrollment_key": "user-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

    # Valid Username and key
    resp = client.post("/api/v1/auth/token", json={"username_or_id": "Bob", "enrollment_key": "user-key"})
    assert resp.status_code == 200


def test_jwt_authentication_protection():
    """Verify that multi-party v1 routes enforce authentication."""
    client = TestClient(app)

    # Verify that with REQUIRE_AUTH=True (default), accessing without headers is rejected
    import src.config
    orig_require = src.config.REQUIRE_AUTH
    try:
        src.config.REQUIRE_AUTH = True
        resp = client.get("/api/v1/party/admin-uuid")
        assert resp.status_code == 401

        # Access with invalid JWT -> fails
        resp = client.get(
            "/api/v1/party/admin-uuid",
            headers={"Authorization": "Bearer invalidtoken"}
        )
        assert resp.status_code == 401

        # Access with valid admin JWT -> succeeds
        admin_token = create_access_token("admin-uuid", "admin")
        resp = client.get(
            "/api/v1/party/admin-uuid",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert resp.status_code == 200

        # POST /api/v1/party/register with standard user JWT (insufficient permissions) -> fails
        token = create_access_token("user-uuid", "user")
        resp = client.post(
            "/api/v1/party/register",
            json={"name": "Charlie", "role": "user"},
            headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403

        # POST /api/v1/party/register with admin JWT -> succeeds
        resp = client.post(
            "/api/v1/party/register",
            json={"name": "Charlie", "role": "user"},
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "Charlie"
    finally:
        src.config.REQUIRE_AUTH = orig_require


def test_local_admin_fallback():
    """Verify that legacy endpoints allow access without headers using the local admin fallback when REQUIRE_AUTH is False, but reject when True."""
    client = TestClient(app)
    import src.config
    orig_require = src.config.REQUIRE_AUTH
    try:
        # Under REQUIRE_AUTH = True, access is rejected
        src.config.REQUIRE_AUTH = True
        resp = client.get("/api/history")
        assert resp.status_code == 401

        # Under REQUIRE_AUTH = False, legacy local admin fallback allows access without headers
        src.config.REQUIRE_AUTH = False
        resp = client.get("/api/history")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

        # GET /api/deliberations
        resp = client.get("/api/deliberations")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
    finally:
        src.config.REQUIRE_AUTH = orig_require


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

    import src.config
    orig_require = src.config.REQUIRE_AUTH
    try:
        # If REQUIRE_AUTH=True, connecting without a token fails
        src.config.REQUIRE_AUTH = True
        with pytest.raises(WebSocketDenialResponse):
            with client.websocket_connect("/ws/deliberations") as ws:
                pass

        # If REQUIRE_AUTH=True, connecting with a valid token succeeds
        token = create_access_token("user-uuid", "user")
        with client.websocket_connect(f"/ws/deliberations?token={token}") as ws:
            data = ws.receive_json()
            assert data["action"] == "test_action"
    finally:
        src.config.REQUIRE_AUTH = orig_require


def test_websocket_chat():
    """Verify WebSocket chat endpoint thinking and response cycles."""
    client = TestClient(app)

    from unittest.mock import patch
    with patch("src.persona.generate_persona_response_autonomous") as mock_persona:
        mock_persona.return_value = "Response from agent!"

        import src.config
        orig_require = src.config.REQUIRE_AUTH
        try:
            # If REQUIRE_AUTH=True, connecting without a token fails
            src.config.REQUIRE_AUTH = True
            with pytest.raises(WebSocketDenialResponse):
                with client.websocket_connect("/ws/chat") as ws:
                    pass

            # If REQUIRE_AUTH=True, connecting with a valid token succeeds
            token = create_access_token("user-uuid", "user")
            with client.websocket_connect(f"/ws/chat?token={token}") as ws:
                ws.send_json({"message": "Hello Janus"})

                # First should be thinking event
                evt1 = ws.receive_json()
                assert evt1["event"] == "thinking"

                # Second should be response event
                evt2 = ws.receive_json()
                assert evt2["event"] == "response"
                assert evt2["message"] == "Response from agent!"
        finally:
            src.config.REQUIRE_AUTH = orig_require


def test_rate_limiting():
    """Verify that hitting the API endpoints in rapid succession triggers a 429 Too Many Requests response."""
    client = TestClient(app)
    import src.config

    # Save config and override
    orig_requests = src.config.RATE_LIMIT_REQUESTS
    orig_window = src.config.RATE_LIMIT_WINDOW

    src.config.RATE_LIMIT_REQUESTS = 3
    src.config.RATE_LIMIT_WINDOW = 2

    # Clear history for this test IP to isolate it
    from src.web_server import ip_request_history
    ip_request_history.clear()

    try:
        # First 3 requests to a public API endpoint succeed
        for _ in range(3):
            resp = client.get("/api/v1/bootstrap/status")
            assert resp.status_code == 200

        # 4th request triggers 429
        resp = client.get("/api/v1/bootstrap/status")
        assert resp.status_code == 429
        assert resp.json()["detail"] == "Too many requests. Please try again later."
    finally:
        src.config.RATE_LIMIT_REQUESTS = orig_requests
        src.config.RATE_LIMIT_WINDOW = orig_window


def test_header_resolution_isolation():
    # Enable Auth for test
    from unittest.mock import patch

    import src.config
    from src.database import get_connection

    original_require_auth = src.config.REQUIRE_AUTH
    src.config.REQUIRE_AUTH = True

    client = TestClient(app)
    db_conn = get_connection(read_only_constitution=False)

    # The test exercises auth header routing, not LLM responses; mock the
    # persona so the handler returns 200 without a real LLM endpoint.
    with patch("src.persona.generate_persona_response_autonomous", return_value="ok"):
        try:
            # 1. Insert Party A matching via public_key column
            party_a_id = "party_a"
            db_conn.execute(
                "INSERT INTO parties (id, name, role, public_key, metadata) VALUES (?, ?, ?, ?, ?);",
                (party_a_id, "User A", "user", "api_key_a", '{}')
            )
            # Seed profile
            db_conn.execute(
                "INSERT INTO interaction_profiles (party_id, response_style, tone_bias) VALUES (?, ?, ?);",
                (party_a_id, "concise", "sarcastic")
            )

            # 2. Insert Party B matching via metadata JSON api_key
            party_b_id = "party_b"
            db_conn.execute(
                "INSERT INTO parties (id, name, role, public_key, metadata) VALUES (?, ?, ?, ?, ?);",
                (party_b_id, "User B", "user", None, '{"api_key": "api_key_b"}')
            )

            # 3. Insert Party C matching via metadata JSON device_fingerprint
            party_c_id = "party_c"
            db_conn.execute(
                "INSERT INTO parties (id, name, role, public_key, metadata) VALUES (?, ?, ?, ?, ?);",
                (party_c_id, "User C", "user", None, '{"device_fingerprint": "fingerprint_c"}')
            )
            db_conn.commit()

            # Clear LRU caches to make sure it loads fresh
            import src.web_server
            src.web_server.resolve_party_by_api_key.cache_clear()
            src.web_server.resolve_party_by_fingerprint.cache_clear()

            # Match A via X-API-Key (public_key column)
            resp = client.post("/api/chat", json={"message": "ping"}, headers={"X-API-Key": "api_key_a"})
            assert resp.status_code == 200

            # Match B via X-API-Key (metadata JSON)
            resp = client.post("/api/chat", json={"message": "ping"}, headers={"X-API-Key": "api_key_b"})
            assert resp.status_code == 200

            # Match C via X-Device-Fingerprint (metadata JSON)
            resp = client.post("/api/chat", json={"message": "ping"}, headers={"X-Device-Fingerprint": "fingerprint_c"})
            assert resp.status_code == 200

            # Hierarchy: Check X-API-Key wins over X-Device-Fingerprint
            # Key 'api_key_a' (party_a, user) + Fingerprint 'fingerprint_c' (party_c, user)
            # If API Key wins, we authenticate as party_a
            from unittest.mock import MagicMock

            import src.web_server as ws_module
            req_mock = MagicMock()
            req_mock.headers = {
                "X-API-Key": "api_key_a",
                "X-Device-Fingerprint": "fingerprint_c"
            }
            resolved = ws_module.get_current_party(req_mock)
            assert resolved["party_id"] == party_a_id

        finally:
            db_conn.close()
            src.config.REQUIRE_AUTH = original_require_auth


def test_metrics_endpoint_unauthenticated():
    """GET /metrics is a Prometheus-scrape-style endpoint — no auth required."""
    client = TestClient(app)
    orig_require = src.config.REQUIRE_AUTH
    try:
        src.config.REQUIRE_AUTH = True
        resp = client.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()
        for key in (
            "llm_calls_total", "llm_calls_failed_total", "daemon_cycles_total",
            "skills_executed_total", "skills_failed_total", "http_requests_total",
            "daemon_last_cycle_timestamp", "episodic_memory_rows", "active_goals_count",
            "goals_checkpoints_completed_total", "goals_checkpoints_completed_autonomously",
        ):
            assert key in data
    finally:
        src.config.REQUIRE_AUTH = orig_require


def test_api_system_metrics_requires_auth():
    client = TestClient(app)
    orig_require = src.config.REQUIRE_AUTH
    try:
        src.config.REQUIRE_AUTH = True
        resp = client.get("/api/system/metrics")
        assert resp.status_code == 401

        token = create_access_token("some-user-uuid", "user")
        resp = client.get("/api/system/metrics", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert "llm_calls_total" in resp.json()
    finally:
        src.config.REQUIRE_AUTH = orig_require


def test_registry_update_allow_offbox_requires_admin_role():
    """Issue #108: allow_offbox is operator-set only — a contributor-role
    party (the endpoint's normal minimum role for target_model updates) must
    be rejected when it tries to also set allow_offbox; an admin-role party
    must succeed and have the value persisted."""
    client = TestClient(app)
    orig_require = src.config.REQUIRE_AUTH
    try:
        src.config.REQUIRE_AUTH = True

        contributor_token = create_access_token("contributor-uuid", "contributor")
        resp = client.post(
            "/api/registry/update",
            json={"agent_id": "proposer", "allow_offbox": True},
            headers={"Authorization": f"Bearer {contributor_token}"},
        )
        assert resp.status_code == 403

        # A contributor may still update target_model alone (unaffected by the new gate).
        resp = client.post(
            "/api/registry/update",
            json={"agent_id": "proposer", "model": "some-model"},
            headers={"Authorization": f"Bearer {contributor_token}"},
        )
        assert resp.status_code == 200

        admin_token = create_access_token("admin-uuid", "admin")
        resp = client.post(
            "/api/registry/update",
            json={"agent_id": "proposer", "allow_offbox": True},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200

        conn = get_connection(read_only_constitution=True)
        row = conn.execute(
            "SELECT allow_offbox FROM agent_registry WHERE agent_id = 'proposer';"
        ).fetchone()
        conn.close()
        assert row[0] == 1
    finally:
        src.config.REQUIRE_AUTH = orig_require

