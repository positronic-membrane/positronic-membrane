import pytest
from unittest.mock import patch, MagicMock
import src.config
from src.database import init_db, get_connection, log_deliberation, log_episodic_memory
from src.skills import SafeSelfModel, DynamicSkillExecutor
from src.memory_hydration import hydrate_context
from src.persona import (
    detect_metacognitive_intent,
    generate_metacognitive_narrative,
    generate_persona_response,
    detect_modification_intent,
    handle_self_command,
    handle_pin_command,
    handle_unpin_command
)

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

def test_detect_modification_intent():
    """Verify that intent detection flags modification queries accurately."""
    # Slash commands
    path, inst = detect_modification_intent("/modify src/config.py | Change T_ACTIVE to 2")
    assert path == "src/config.py"
    assert inst == "Change T_ACTIVE to 2"
    
    path, inst = detect_modification_intent("/modify src/config.py")
    assert path == "INVALID"
    assert inst is None
    
    # Natural language requests
    path, inst = detect_modification_intent("please modify src/config.py to set T_ACTIVE to 2")
    assert path == "src/config.py"
    assert "set T_ACTIVE to 2" in inst
    
    path, inst = detect_modification_intent("can you change tests/test_persona.py to add some assertions?")
    assert path == "tests/test_persona.py"
    assert "add some assertions" in inst
    
    # Non-modifications
    path, inst = detect_modification_intent("can you explain src/config.py?")
    assert path is None
    assert inst is None

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
