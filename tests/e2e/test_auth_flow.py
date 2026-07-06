"""End-to-end: bootstrap -> enroll -> authenticate -> party isolation -> role gating.

Pure HTTP + SQLite — no LLM or vector-store involvement, so only e2e_client is
used here (the daemon/sandbox-specific fixtures are irrelevant to this file).
"""

import pytest

from src.auth import decode_access_token
from src.role_bootstrap import RoleBootstrap

pytestmark = pytest.mark.e2e


def test_bootstrap_then_enroll_then_authenticate(e2e_client):
    status = e2e_client.get("/api/v1/bootstrap/status")
    assert status.status_code == 200
    assert status.json()["bootstrap_required"] is True

    admin_party_id, admin_key = RoleBootstrap().create_root_admin()

    token_resp = e2e_client.post(
        "/api/v1/auth/token",
        json={"username_or_id": admin_party_id, "enrollment_key": admin_key},
    )
    assert token_resp.status_code == 200
    admin_token = token_resp.json()["access_token"]
    assert decode_access_token(admin_token)["role"] == "admin"

    enroll_resp = e2e_client.post(
        "/api/v1/party/register",
        json={"name": "e2e-new-user", "role": "user", "public_key": "e2e-user-enrollment-key"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert enroll_resp.status_code == 201
    new_party_id = enroll_resp.json()["party_id"]

    user_token_resp = e2e_client.post(
        "/api/v1/auth/token",
        json={"username_or_id": new_party_id, "enrollment_key": "e2e-user-enrollment-key"},
    )
    assert user_token_resp.status_code == 200
    assert decode_access_token(user_token_resp.json()["access_token"])["role"] == "user"


def test_party_isolation_via_memory_endpoints(e2e_client, seed_party):
    party_a_id, token_a = seed_party(role="user")
    _, token_b = seed_party(role="user")

    set_resp = e2e_client.post(
        "/api/v1/memory",
        json={"key": "secret", "value": "party-a-only-value"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert set_resp.status_code == 201

    own_get = e2e_client.get(
        "/api/v1/memory/secret", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert own_get.status_code == 200
    assert own_get.json()["value"] == "party-a-only-value"

    other_get = e2e_client.get(
        "/api/v1/memory/secret", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert other_get.status_code == 404


def test_role_based_access_denied(e2e_client, seed_party):
    _, user_token = seed_party(role="user")
    _, contributor_token = seed_party(role="contributor")

    for token in (user_token, contributor_token):
        resp = e2e_client.post(
            "/api/v1/party/register",
            json={"name": "should-not-be-created", "role": "user"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
