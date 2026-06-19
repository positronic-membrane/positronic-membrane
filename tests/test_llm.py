from unittest.mock import MagicMock, patch

import pytest

import src.config
from src.database import get_connection, init_db
from src.llm import get_agent_settings, query_agent, resolve_agent_model


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

def test_get_agent_settings():
    """Verify registry queries return correct defaults for proposer and critic."""
    proposer_settings = get_agent_settings("proposer")
    assert proposer_settings is not None
    assert proposer_settings[0] == "Proposer Agent"
    assert "You are the Proposer" in proposer_settings[1]
    assert proposer_settings[2] is None  # target_model defaults to Null

def test_resolve_agent_model(monkeypatch):
    """Verify dynamic model overrides resolve in order of priority."""
    # 1. Global default fallback
    monkeypatch.setattr(src.config, "LLM_MODEL", "global-model-7b")

    resolved = resolve_agent_model("proposer", db_model=None)
    assert resolved == "global-model-7b"

    # 2. DB Override (Highest priority)
    resolved = resolve_agent_model("proposer", db_model="db-override-32b")
    assert resolved == "db-override-32b"

@patch("src.llm.OpenAI")
def test_query_agent_completions(mock_openai_class):
    """Verify that query_agent instantiates OpenAI client and returns mock response."""
    # Mock OpenAI client completions response
    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client

    mock_choice = MagicMock()
    mock_choice.message.content = "PROPOSED_ACTION: Scan documentation folder"
    mock_client.chat.completions.create.return_value.choices = [mock_choice]

    resp = query_agent("proposer", "Build action")
    assert resp == "PROPOSED_ACTION: Scan documentation folder"

    # Check that it was called with correct parameters
    mock_client.chat.completions.create.assert_called_once()
    kwargs = mock_client.chat.completions.create.call_args[1]
    assert kwargs["model"] == src.config.LLM_MODEL
    assert len(kwargs["messages"]) == 2
    assert kwargs["messages"][0]["role"] == "system"
    assert kwargs["messages"][1]["content"] == "Build action"

def test_resolve_agent_client_params(monkeypatch):
    """Verify that API URL and key are dynamically resolved based on overrides and model name."""
    from src.llm import resolve_agent_client_params

    # Reset overrides to clean state
    monkeypatch.setattr(src.config, "PROPOSER_BASE_URL", None)
    monkeypatch.setattr(src.config, "PROPOSER_API_KEY", None)
    monkeypatch.setattr(src.config, "OPENROUTER_API_KEY", "")

    # Case 1: Default fallbacks (local Ollama)
    base_url, api_key = resolve_agent_client_params("proposer", "qwen2.5-coder:7b")
    assert base_url == src.config.LLM_BASE_URL
    assert api_key == src.config.LLM_API_KEY

    # Case 2: OpenRouter automatic routing (contains '/' and OPENROUTER_API_KEY is configured)
    monkeypatch.setattr(src.config, "OPENROUTER_API_KEY", "sk-or-v1-testkey")
    base_url, api_key = resolve_agent_client_params("proposer", "google/gemini-2.5-flash")
    assert base_url == "https://openrouter.ai/api/v1"
    assert api_key == "sk-or-v1-testkey"

    # Case 3: Agent-specific overrides (highest priority)
    monkeypatch.setattr(src.config, "PROPOSER_BASE_URL", "https://custom-agent-endpoint.com/v1")
    monkeypatch.setattr(src.config, "PROPOSER_API_KEY", "custom-agent-key")
    base_url, api_key = resolve_agent_client_params("proposer", "google/gemini-2.5-flash")
    assert base_url == "https://custom-agent-endpoint.com/v1"
    assert api_key == "custom-agent-key"


# --- Consolidating from test_v1_priority0.py ---

@patch("openai.resources.chat.completions.Completions.create")
def test_llm_cache_and_retry(mock_create):
    # Setup mock completions response
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="Hello cache content"))]
    mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=15)
    mock_create.return_value = mock_resp

    # Verify first query (cache miss, runs LLM, caches response)
    res = query_agent("proposer", "Hello caching validation")
    assert res == "Hello cache content"
    assert mock_create.call_count == 1

    # Verify second query (cache hit, returns response without calling API)
    res_cached = query_agent("proposer", "Hello caching validation")
    assert res_cached == "Hello cache content"
    assert mock_create.call_count == 1

@patch("openai.resources.chat.completions.Completions.create")
def test_llm_cost_limiting(mock_create):
    from src.llm import BillingViolationError

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="Success response"))]
    mock_resp.usage = MagicMock(prompt_tokens=1000000, completion_tokens=1000000)
    mock_create.return_value = mock_resp

    conn = get_connection(read_only_constitution=False)
    conn.execute("INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) VALUES ('daily_budget_usd', '0.01', 1);")
    conn.commit()
    conn.close()

    # First query works but consumes budget
    query_agent("proposer", "Big prompt")

    # Second query throws BillingViolationError
    with pytest.raises(BillingViolationError):
        query_agent("proposer", "Another query")

@patch("openai.resources.chat.completions.Completions.create")
def test_llm_hyperparameters_calibration(mock_create):
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="Critic response"))]
    mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=15)
    mock_create.return_value = mock_resp

    # Verify temp override for Critic
    query_agent("critic", "Auditing safety constraint")

    call_args = mock_create.call_args[1]
    assert call_args["temperature"] == 0.0
    assert call_args["top_p"] == 1.0

