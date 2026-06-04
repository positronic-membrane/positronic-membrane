import pytest
from unittest.mock import patch, MagicMock
import src.config
from src.database import init_db, log_deliberation, log_episodic_memory
from src.persona import (
    detect_metacognitive_intent,
    generate_metacognitive_narrative,
    generate_persona_response,
    detect_modification_intent
)

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus.db"
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
