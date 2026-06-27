from unittest.mock import patch

import pytest

import src.config
from src.database import get_connection, init_db, log_deliberation, log_episodic_memory
from src.memory_hydration import hydrate_context
from src.persona import (
    detect_metacognitive_intent,
    generate_metacognitive_narrative,
    generate_persona_response,
    handle_pin_command,
    handle_self_command,
    handle_unpin_command,
    run_persona_chat,
)
from src.skills import DynamicSkillExecutor, SafeSelfModel


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus_persona.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

def test_detect_metacognitive_intent():
    """Verify that intent detection flags metacognitive queries while allowing conversational queries."""
    # Metacognitive queries
    assert detect_metacognitive_intent("what did you do while I was away?")
    assert detect_metacognitive_intent("explain deliberations from today")
    assert detect_metacognitive_intent("audit background tasks")
    assert detect_metacognitive_intent("why did you run that search?")

    # Conversational queries
    assert not detect_metacognitive_intent("hello Janus!")
    assert not detect_metacognitive_intent("can you write a quick python loop for me?")
    assert not detect_metacognitive_intent("what is the capital of France?")

@patch("src.persona.query_agent")
def test_generate_metacognitive_narrative(mock_query):
    """Verify deliberations are fetched from SQLite and mapped to the Persona explanation."""
    mock_query.return_value = "While you were coding, I completed a background scan of your documents."

    # Insert mock deliberations
    log_deliberation(
        proposed_action="Scan project docs",
        debate_json={"proposer": "indexing"},
        critic_decision=1,
        utility_score=0.9,
        justification="Safe action"
    )

    narrative = generate_metacognitive_narrative("What did you do in the background?")

    assert narrative == "While you were coding, I completed a background scan of your documents."
    mock_query.assert_called_once()

    # Check that database records are in the prompt context
    args, kwargs = mock_query.call_args
    prompt_used = args[1]
    assert "Scan project docs" in prompt_used
    assert "Safe action" in prompt_used

@patch("src.persona.query_memories")
@patch("src.persona.query_agent")
def test_generate_persona_response(mock_query, mock_query_memories):
    """Verify standard chat responses draw from vector memory context and conversation history."""
    mock_query.return_value = "Hello! I am ready to assist you."
    mock_query_memories.return_value = [{"content": "Project Janus is a multi-agent swarm."}]

    # Insert chat history
    log_episodic_memory("user", "Hello Janus", "user_visible")
    log_episodic_memory("persona", "Hello! How can I help?", "user_visible")

    response = generate_persona_response("Who are you?")

    assert response == "Hello! I am ready to assist you."
    mock_query.assert_called_once()

    # Check context in prompt
    args, kwargs = mock_query.call_args
    prompt_used = args[1]
    assert "Project Janus is a multi-agent swarm." in prompt_used
    assert "Hello Janus" in prompt_used
    assert "Who are you?" in prompt_used

# --- Consolidating from test_phase3_self_model.py ---

def test_safe_self_model_get_and_update():
    sm = SafeSelfModel()
    traits = sm.get_traits()
    assert "curiosity" in traits
    assert "verbosity" in traits
    assert "cautiousness" in traits

    assert traits["curiosity"]["value"] == 0.5
    assert traits["curiosity"]["confidence"] == 0.5
    assert traits["curiosity"]["is_pinned"] == 0

    # Update unpinned trait
    success = sm.update_trait("curiosity", 0.8, 0.9, "Exploration of new codebase modules")
    assert success is True

    new_traits = sm.get_traits()
    assert new_traits["curiosity"]["value"] == 0.8
    assert new_traits["curiosity"]["confidence"] == 0.9

    # Verify history is logged
    conn = get_connection()
    try:
        row = conn.execute("SELECT old_value, new_value, reason FROM self_model_history ORDER BY id DESC LIMIT 1;").fetchone()
        assert row is not None
        assert float(row[0]) == 0.5
        assert float(row[1]) == 0.8
        assert row[2] == "Exploration of new codebase modules"
    finally:
        conn.close()

def test_pinning_and_immutability():
    sm = SafeSelfModel()

    # Pin curiosity manually using command
    res = handle_pin_command("/pin curiosity 0.3")
    assert "pinned" in res.lower()

    traits = sm.get_traits()
    assert traits["curiosity"]["value"] == 0.3
    assert traits["curiosity"]["is_pinned"] == 1
    assert traits["curiosity"]["confidence"] == 1.0

    # Attempting to update a pinned trait via SafeSelfModel should return False
    success = sm.update_trait("curiosity", 0.9, 0.9, "Automated update test")
    assert success is False

    traits_after = sm.get_traits()
    assert traits_after["curiosity"]["value"] == 0.3

    # Unpin curiosity
    res_unpin = handle_unpin_command("/unpin curiosity")
    assert "unpinned" in res_unpin.lower()

    traits_unpinned = sm.get_traits()
    assert traits_unpinned["curiosity"]["is_pinned"] == 0

    # Updating now works
    success_after = sm.update_trait("curiosity", 0.9, 0.9, "Post-unpin update")
    assert success_after is True
    assert sm.get_traits()["curiosity"]["value"] == 0.9

def test_self_command_rendering():
    res = handle_self_command()
    assert "🧠 Janus Self-Model & Personality Traits" in res
    assert "Curiosity" in res
    assert "Verbosity" in res
    assert "Cautiousness" in res

def test_prompt_modulation_via_traits():
    # Test low verbosity prompt modulation
    handle_pin_command("/pin verbosity 0.1")
    handle_pin_command("/pin curiosity 0.2")
    handle_pin_command("/pin cautiousness 0.8")

    with patch("src.persona.query_agent") as mock_query:
        mock_query.return_value = "Response mock"
        generate_persona_response("Hello")

        called_prompt = mock_query.call_args[0][1]
        assert "Be extremely concise" in called_prompt
        assert "Answer directly and stick only to the requested topic" in called_prompt
        assert "Emphasize security, verification" in called_prompt

    # Test high verbosity/curiosity/low cautiousness prompt modulation
    handle_pin_command("/pin verbosity 0.9")
    handle_pin_command("/pin curiosity 0.8")
    handle_pin_command("/pin cautiousness 0.2")

    with patch("src.persona.query_agent") as mock_query:
        mock_query.return_value = "Response mock"
        generate_persona_response("Hello")

        called_prompt = mock_query.call_args[0][1]
        assert "Be highly verbose" in called_prompt
        assert "Actively demonstrate curiosity" in called_prompt
        assert "Prioritize direct action, efficiency" in called_prompt

def test_decay_self_model_skill():
    sm = SafeSelfModel()

    # Seed trait values to non-baseline
    sm.update_trait("verbosity", 0.8, 0.8, "Increased verbosity")

    # Verify starting state
    traits_before = sm.get_traits()
    assert traits_before["verbosity"]["value"] == 0.8
    assert traits_before["verbosity"]["confidence"] == 0.8

    # Run the decay skill
    party_id = "system"
    res = DynamicSkillExecutor.execute("decay_self_model", {}, party_id=party_id)
    assert res["success"] is True
    assert "decayed" in res["result"].lower()

    traits_after = sm.get_traits()
    assert traits_after["verbosity"]["value"] < 0.8
    assert traits_after["verbosity"]["confidence"] == 0.795

# --- Consolidating from test_v1_priority0.py ---

def test_context_hydration_tagging():
    # Insert mock self trait
    conn = get_connection(read_only_constitution=False)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO self_model (trait_name, value, confidence, is_pinned) VALUES ('verbosity', 0.8, 0.9, 1);")
    # Insert mock episodic memory
    cursor.execute("INSERT INTO episodic_memory (speaker, message_content, context_type, party_id) VALUES ('user', 'Hello Janus', 'user_visible', 'user-123');")
    conn.commit()
    conn.close()

    # Hydrate context
    hydrated = hydrate_context("user-123", limit_memories=5)

    # Check XML tags are present
    assert "<self_traits>" in hydrated
    assert "- verbosity: 0.8" in hydrated
    assert "</self_traits>" in hydrated

    assert "<episodic_memory>" in hydrated
    assert "user: Hello Janus" in hydrated
    assert "</episodic_memory>" in hydrated

    assert "<semantic_knowledge>" in hydrated

    # Check Anchor directive
    assert "Your objective reality is defined strictly by the data provided" in hydrated
    assert "You are strictly forbidden from substituting pre-trained assumptions." in hydrated

# --- V2-T10: Dispute Resolution Protocol ---

def _open_a_dispute() -> int:
    """Logs 3 consecutive Critic vetoes to create an open swarm dispute, returns its id."""
    from src.database import get_open_disputes

    for _ in range(3):
        log_deliberation(
            proposed_action="modify_code: src/risky.py",
            debate_json={"proposer_output": "x", "critic_output": "y"},
            critic_decision=0,
            utility_score=0.0,
            justification="Violates constitution rule X",
        )
    return get_open_disputes()[0]["id"]


def _make_input_mock(inputs):
    remaining = list(inputs)

    def side_effect(prompt):
        if remaining:
            return remaining.pop(0)
        return "/exit"
    return side_effect


@pytest.mark.asyncio
@patch("src.persona.get_input")
async def test_goals_resolve_no_open_disputes(mock_get_input, capsys):
    mock_get_input.side_effect = _make_input_mock(["/goals resolve", "/exit"])

    await run_persona_chat()

    captured = capsys.readouterr()
    assert "No open disputes" in captured.out


@pytest.mark.asyncio
@patch("src.persona.get_input")
async def test_goals_resolve_lists_open_disputes(mock_get_input, capsys):
    dispute_id = _open_a_dispute()
    mock_get_input.side_effect = _make_input_mock(["/goal resolve", "/exit"])

    await run_persona_chat()

    captured = capsys.readouterr()
    assert f"[{dispute_id}]" in captured.out
    assert "modify_code: src/risky.py" in captured.out
    assert "vetoed 3x" in captured.out


@pytest.mark.asyncio
@patch("src.persona.get_input")
async def test_goals_resolve_unknown_id(mock_get_input, capsys):
    mock_get_input.side_effect = _make_input_mock(["/goals resolve 999", "/exit"])

    await run_persona_chat()

    captured = capsys.readouterr()
    assert "Dispute ID 999 not found" in captured.out


@pytest.mark.asyncio
@patch("src.persona.get_input")
async def test_goals_resolve_shows_transcript_and_overrides(mock_get_input, capsys):
    from src.database import get_dispute

    dispute_id = _open_a_dispute()
    mock_get_input.side_effect = _make_input_mock([f"/goals resolve {dispute_id}", "override", "/exit"])

    await run_persona_chat()

    captured = capsys.readouterr()
    assert "Debate transcript" in captured.out
    assert "Violates constitution rule X" in captured.out
    assert f"Dispute [{dispute_id}] resolved: override" in captured.out

    dispute = get_dispute(dispute_id)
    assert dispute["status"] == "resolved"
    assert dispute["resolution"] == "override"

    conn = get_connection(read_only_constitution=True)
    row = conn.execute("SELECT config_value FROM system_config WHERE config_key = 'dispute_paused';").fetchone()
    conn.close()
    assert row[0] == "false"


@pytest.mark.asyncio
@patch("src.persona.get_input")
async def test_goals_resolve_abort(mock_get_input):
    from src.database import get_dispute

    dispute_id = _open_a_dispute()
    mock_get_input.side_effect = _make_input_mock([f"/goals resolve {dispute_id}", "abort", "/exit"])

    await run_persona_chat()

    assert get_dispute(dispute_id)["resolution"] == "abort"


@pytest.mark.asyncio
@patch("src.persona.get_input")
async def test_goals_resolve_rewrite_rules_seals_constitution_rule(mock_get_input):
    from src.database import get_constitution, get_dispute

    dispute_id = _open_a_dispute()
    mock_get_input.side_effect = _make_input_mock([
        f"/goals resolve {dispute_id}",
        "rewrite",
        "no_risky_modify | Risky modifications to src/risky.py are now permitted.",
        "/exit",
    ])

    await run_persona_chat()

    dispute = get_dispute(dispute_id)
    assert dispute["resolution"] == "rewrite_rules"
    assert dispute["resolution_notes"] == "no_risky_modify | Risky modifications to src/risky.py are now permitted."

    rules = dict(get_constitution())
    assert rules["NO_RISKY_MODIFY"] == "Risky modifications to src/risky.py are now permitted."


@pytest.mark.asyncio
@patch("src.persona.get_input")
async def test_goals_resolve_cancel_keeps_dispute_open(mock_get_input):
    from src.database import get_dispute

    dispute_id = _open_a_dispute()
    mock_get_input.side_effect = _make_input_mock([f"/goals resolve {dispute_id}", "cancel", "/exit"])

    await run_persona_chat()

    dispute = get_dispute(dispute_id)
    assert dispute["status"] == "open"

    conn = get_connection(read_only_constitution=True)
    row = conn.execute("SELECT config_value FROM system_config WHERE config_key = 'dispute_paused';").fetchone()
    conn.close()
    assert row[0] == "true"
