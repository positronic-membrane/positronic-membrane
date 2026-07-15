from unittest.mock import MagicMock, patch

import pytest

import src.config
from src.database import get_connection, init_db
from src.llm import (
    OffboxRoutingViolationError,
    get_agent_settings,
    query_agent,
    resolve_agent_model,
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


@pytest.fixture(autouse=True)
def _default_no_openrouter(monkeypatch):
    """Neutralize the developer's real .env OPENROUTER_API_KEY so tests that
    exercise the real query_agent()/_prepare_llm_call() path aren't at the
    mercy of local dev config for off-box routing (issue #108's allow_offbox
    gate makes this consequential — previously irrelevant since the OpenAI
    client itself was always mocked regardless of which endpoint it was
    constructed with). Tests that specifically exercise OpenRouter routing
    re-set this themselves via the same monkeypatch fixture."""
    monkeypatch.setattr(src.config, "OPENROUTER_API_KEY", "")

def test_get_agent_settings():
    """Verify registry queries return correct defaults for proposer and critic."""
    proposer_settings = get_agent_settings("proposer")
    assert proposer_settings is not None
    assert proposer_settings[0] == "Proposer Agent"
    assert "You are the Proposer" in proposer_settings[1]
    assert proposer_settings[2] is None  # target_model defaults to Null
    assert proposer_settings[3] == 0  # allow_offbox defaults to deny (issue #108)

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

    # Case 2: OpenRouter automatic routing (contains '/' and OPENROUTER_API_KEY is configured).
    # allow_offbox=True here since this is testing tier-resolution logic, not
    # the allow_offbox gate itself (covered separately below) — the
    # parameter's default is fail-closed (issue #108), so it must be passed
    # explicitly to reach tier 2/3.
    monkeypatch.setattr(src.config, "OPENROUTER_API_KEY", "sk-or-v1-testkey")
    base_url, api_key = resolve_agent_client_params("proposer", "google/gemini-2.5-flash", allow_offbox=True)
    assert base_url == "https://openrouter.ai/api/v1"
    assert api_key == "sk-or-v1-testkey"

    # Case 3: Agent-specific overrides (highest priority)
    monkeypatch.setattr(src.config, "PROPOSER_BASE_URL", "https://custom-agent-endpoint.com/v1")
    monkeypatch.setattr(src.config, "PROPOSER_API_KEY", "custom-agent-key")
    base_url, api_key = resolve_agent_client_params("proposer", "google/gemini-2.5-flash", allow_offbox=True)
    assert base_url == "https://custom-agent-endpoint.com/v1"
    assert api_key == "custom-agent-key"


def test_resolve_agent_client_params_allow_offbox_false(monkeypatch):
    """Verify allow_offbox=False (issue #108) raises instead of silently
    routing off-box, for both the agent-override tier and the OpenRouter
    tier, but never blocks the LLM_BASE_URL fallback tier."""
    from src.llm import resolve_agent_client_params

    monkeypatch.setattr(src.config, "PROPOSER_BASE_URL", None)
    monkeypatch.setattr(src.config, "PROPOSER_API_KEY", None)
    monkeypatch.setattr(src.config, "OPENROUTER_API_KEY", "")

    # Tier 3 fallback is always permitted, even with allow_offbox=False.
    base_url, api_key = resolve_agent_client_params(
        "proposer", "qwen2.5-coder:7b", allow_offbox=False
    )
    assert base_url == src.config.LLM_BASE_URL
    assert api_key == src.config.LLM_API_KEY

    # Tier 2 (OpenRouter) raises when allow_offbox=False.
    monkeypatch.setattr(src.config, "OPENROUTER_API_KEY", "sk-or-v1-testkey")
    with pytest.raises(OffboxRoutingViolationError):
        resolve_agent_client_params("proposer", "google/gemini-2.5-flash", allow_offbox=False)

    # Tier 1 (agent-specific override) raises when allow_offbox=False.
    monkeypatch.setattr(src.config, "PROPOSER_BASE_URL", "https://custom-agent-endpoint.com/v1")
    monkeypatch.setattr(src.config, "PROPOSER_API_KEY", "custom-agent-key")
    with pytest.raises(OffboxRoutingViolationError):
        resolve_agent_client_params("proposer", "qwen2.5-coder:7b", allow_offbox=False)


@patch("src.llm.OpenAI")
def test_offbox_violation_propagates_through_query_agent(mock_openai_class, monkeypatch):
    """An agent with allow_offbox=0 (the default) and a '/'-model with
    OPENROUTER_API_KEY set must raise OffboxRoutingViolationError from
    query_agent() rather than silently falling back or routing off-box —
    and must never construct/call the OpenAI client (issue #108)."""
    monkeypatch.setattr(src.config, "OPENROUTER_API_KEY", "sk-or-v1-testkey")

    conn = get_connection()
    conn.cursor().execute(
        "UPDATE agent_registry SET target_model = 'google/gemini-2.5-flash' WHERE agent_id = 'proposer';"
    )
    conn.commit()
    conn.close()

    with pytest.raises(OffboxRoutingViolationError):
        query_agent("proposer", "Build action")

    mock_openai_class.assert_not_called()


@patch("src.llm.OpenAI")
def test_allow_offbox_1_permits_openrouter_routing(mock_openai_class, monkeypatch):
    """allow_offbox=1 permits the same agent/model combo that would
    otherwise raise (issue #108)."""
    monkeypatch.setattr(src.config, "OPENROUTER_API_KEY", "sk-or-v1-testkey")

    conn = get_connection()
    conn.cursor().execute(
        "UPDATE agent_registry SET target_model = 'google/gemini-2.5-flash', allow_offbox = 1 "
        "WHERE agent_id = 'proposer';"
    )
    conn.commit()
    conn.close()

    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client
    mock_choice = MagicMock()
    mock_choice.message.content = "PROPOSED_ACTION: ok"
    mock_client.chat.completions.create.return_value.choices = [mock_choice]

    resp = query_agent("proposer", "Build action")
    assert resp == "PROPOSED_ACTION: ok"
    mock_openai_class.assert_called_once()
    call_kwargs = mock_openai_class.call_args[1]
    assert call_kwargs["base_url"] == "https://openrouter.ai/api/v1"


@patch("src.llm.OpenAI")
def test_offbox_gate_does_not_block_a_cache_hit(mock_openai_class, monkeypatch):
    """issue #108: a cache hit needs zero network egress, so it must be served
    even for an agent whose *current* routing would violate allow_offbox=0 —
    the gate must not run ahead of the cache short-circuit."""
    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="cached content"))]
    mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=15)
    mock_client.chat.completions.create.return_value = mock_resp

    # First call: local on-box routing (default), populates the cache.
    resp = query_agent("proposer", "cache-then-offbox test")
    assert resp == "cached content"
    assert mock_client.chat.completions.create.call_count == 1

    # Flip this agent's target_model to something that would now violate
    # allow_offbox=0 — the system prompt (and therefore prompt_hash) is
    # unaffected by target_model, so the same prompt still hits the cache.
    monkeypatch.setattr(src.config, "OPENROUTER_API_KEY", "sk-or-v1-testkey")
    conn = get_connection()
    conn.cursor().execute(
        "UPDATE agent_registry SET target_model = 'google/gemini-2.5-flash' WHERE agent_id = 'proposer';"
    )
    conn.commit()
    conn.close()

    resp2 = query_agent("proposer", "cache-then-offbox test")
    assert resp2 == "cached content"
    # No new client call — served entirely from cache, gate never engaged.
    assert mock_client.chat.completions.create.call_count == 1


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


def test_llm_cache_cleanup_ttl():
    import datetime

    from src.skills import DynamicSkillExecutor

    conn = get_connection(read_only_constitution=False)
    import sqlite3
    conn.row_factory = sqlite3.Row

    # Seed ttl_days config
    conn.execute(
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) "
        "VALUES ('llm_cache.ttl_days', '7', 1);"
    )
    # Clear last run time to ensure it triggers
    conn.execute(
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) "
        "VALUES ('llm_cache.last_cleanup_time', '', 1);"
    )
    conn.commit()

    def insert_cache_row(prompt_hash, days_ago):
        target_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days_ago)
        ts_str = target_time.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO llm_cache (prompt_hash, response, created_at) VALUES (?, ?, ?);",
            (prompt_hash, "cached response", ts_str)
        )
        conn.commit()

    # 1. Insert a fresh cache row (2 days old)
    insert_cache_row("fresh_hash", 2)
    # 2. Insert an expired cache row (10 days old)
    insert_cache_row("expired_hash", 10)

    rows = conn.execute("SELECT prompt_hash FROM llm_cache;").fetchall()
    assert len(rows) == 2

    # Execute the cleanup skill
    res = DynamicSkillExecutor.execute("cleanup_llm_cache", {}, party_id="system")
    assert res["success"] is True
    assert "LLM cache cleanup complete" in res["result"]

    # Verify only the fresh row remains
    rows_after = conn.execute("SELECT prompt_hash FROM llm_cache;").fetchall()
    assert len(rows_after) == 1
    assert rows_after[0]["prompt_hash"] == "fresh_hash"

    # Verify last_cleanup_time is now populated
    last_cleanup = conn.execute(
        "SELECT config_value FROM system_config WHERE config_key = 'llm_cache.last_cleanup_time';"
    ).fetchone()
    assert last_cleanup["config_value"] != ""

    # Re-run immediately: it should skip cleanup
    res_skipped = DynamicSkillExecutor.execute("cleanup_llm_cache", {}, party_id="system")
    assert res_skipped["success"] is True
    assert "LLM cache cleanup skipped" in res_skipped["result"]
    conn.close()


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


@patch("openai.resources.chat.completions.Completions.create")
def test_llm_calls_total_increments_on_success(mock_create):
    from src.metrics import _get_counter

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="ok"))]
    mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=15)
    mock_create.return_value = mock_resp

    before = _get_counter("metrics.llm_calls_total")
    query_agent("proposer", "unique prompt for counter test")
    assert _get_counter("metrics.llm_calls_total") == before + 1
    assert _get_counter("metrics.llm_calls_failed_total") == 0


@patch("openai.resources.chat.completions.Completions.create")
def test_llm_calls_failed_total_increments_on_retry_exhaustion(mock_create, monkeypatch):
    import src.llm
    from src.metrics import _get_counter

    monkeypatch.setattr(src.llm.time, "sleep", lambda *_a, **_kw: None)
    mock_create.side_effect = RuntimeError("connection refused")

    before_total = _get_counter("metrics.llm_calls_total")
    before_failed = _get_counter("metrics.llm_calls_failed_total")

    with pytest.raises(RuntimeError):
        query_agent("proposer", "a prompt that will exhaust retries")

    assert _get_counter("metrics.llm_calls_total") == before_total + 1
    assert _get_counter("metrics.llm_calls_failed_total") == before_failed + 1


def test_llm_calls_failed_total_increments_on_billing_violation():
    from src.llm import BillingViolationError
    from src.metrics import _get_counter

    conn = get_connection(read_only_constitution=False)
    conn.execute(
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) "
        "VALUES ('daily_budget_usd', '0', 1);"
    )
    conn.commit()
    conn.close()

    before_failed = _get_counter("metrics.llm_calls_failed_total")
    with pytest.raises(BillingViolationError):
        query_agent("proposer", "should be billing-blocked")
    assert _get_counter("metrics.llm_calls_failed_total") == before_failed + 1

