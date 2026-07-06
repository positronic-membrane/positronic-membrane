"""End-to-end: chat message -> persona response -> episodic memory -> next-turn hydration.

Exercises the real POST /api/chat route (src/routers/chat.py), the real
generate_persona_response_autonomous/_build_persona_prompt pipeline (src/persona.py),
and real SQLite episodic_memory rows — only the LLM call itself is scripted.
"""

from unittest.mock import patch

import pytest

import src.config

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _require_auth_disabled(monkeypatch):
    # Explicit rather than relying on the ambient default, so this test stays
    # correct even if REQUIRE_AUTH's default ever changes.
    monkeypatch.setattr(src.config, "REQUIRE_AUTH", False)


@pytest.fixture
def mock_persona_llm():
    with patch("src.persona.query_agent") as mock_query:
        mock_query.return_value = "Hello! I am Janus, nice to meet you."
        yield mock_query


def _episodic_rows(limit=10):
    from src.database import get_connection

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT speaker, message_content, context_type FROM episodic_memory "
            "ORDER BY id ASC LIMIT ?;",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return rows


def test_chat_roundtrip_logs_episodic_memory(e2e_client, mock_persona_llm):
    resp = e2e_client.post("/api/chat", json={"message": "Hello Janus"})

    assert resp.status_code == 200
    assert resp.json()["response"] == "Hello! I am Janus, nice to meet you."

    rows = _episodic_rows()
    user_rows = [r for r in rows if r[0] == "user"]
    persona_rows = [r for r in rows if r[0] == "persona"]

    assert any(r[1] == "Hello Janus" and r[2] == "user_visible" for r in user_rows)
    assert any(
        r[1] == "Hello! I am Janus, nice to meet you." and r[2] == "user_visible"
        for r in persona_rows
    )


def test_memory_hydration_includes_prior_turn(e2e_client, mock_persona_llm):
    mock_persona_llm.return_value = "Got it, teal it is."
    resp_a = e2e_client.post("/api/chat", json={"message": "My favorite color is teal"})
    assert resp_a.status_code == 200

    mock_persona_llm.return_value = "You told me your favorite color is teal."
    resp_b = e2e_client.post("/api/chat", json={"message": "What did I just tell you?"})
    assert resp_b.status_code == 200

    # The second call's prompt is built from get_recent_episodic_memories, which
    # must include the first turn's user message for hydration to be real.
    second_call_prompt = mock_persona_llm.call_args_list[-1].args[1]
    assert "teal" in second_call_prompt
    assert "What did I just tell you?" in second_call_prompt


def test_chat_requires_user_role(e2e_client, seed_party):
    _, observer_token = seed_party(role="observer")
    resp = e2e_client.post(
        "/api/chat",
        json={"message": "Hello"},
        headers={"Authorization": f"Bearer {observer_token}"},
    )
    assert resp.status_code == 403
