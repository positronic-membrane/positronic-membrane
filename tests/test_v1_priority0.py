import os
import pytest
import unittest.mock as mock
from unittest.mock import patch
import src.config
from src.database import get_connection, init_db
from src.skills import SafeGoals
from src.memory_hydration import hydrate_context
from src.daemon import (
    check_smart_governor_stagnation,
    _consecutive_stagnant_cycles,
    reset_consecutive_background_loops,
    get_consecutive_background_loops
)
from src.llm import query_agent, BillingViolationError

@pytest.fixture(autouse=True)
def clean_globals_and_db(setup_test_db):
    """Resets global governor variables and cleans up DB state for every test."""
    import src.daemon
    src.daemon._consecutive_stagnant_cycles = 0
    src.daemon._last_git_diff_hash = None
    src.daemon._last_db_write_count = None
    src.daemon._last_completed_checkpoints = None
    
    # Initialize schema
    init_db()
    yield

# ==========================================
# 1. Goal Management & CLI Commands Tests
# ==========================================

def test_goals_management_crud():
    sg = SafeGoals()
    
    # Test Create
    res = sg.manage_goals("create", {"type": "short", "description": "Write Priority 0 tests"})
    assert res["success"] is True
    goal_id = res["goal_id"]
    assert goal_id is not None
    
    # Check created goal status
    goals = sg.get_goals(type="short")
    assert len(goals) == 1
    assert goals[0]["id"] == goal_id
    assert goals[0]["status"] == "proposed"
    
    # Test Modify Status & Tier
    res = sg.manage_goals("modify", {"goal_id": goal_id, "status": "in_progress", "type": "long"})
    assert res["success"] is True
    
    goals = sg.get_goals(type="long")
    assert len(goals) == 1
    assert goals[0]["status"] == "in_progress"
    
    # Test Archive
    res = sg.manage_goals("archive", {"goal_id": goal_id})
    assert res["success"] is True
    goals = sg.get_goals(status="archived")
    assert len(goals) == 1
    
    # Test Delete (Soft Delete status update)
    res = sg.manage_goals("delete", {"goal_id": goal_id})
    assert res["success"] is True
    goals = sg.get_goals(status="deleted")
    assert len(goals) == 1

def test_goals_checkpoints():
    sg = SafeGoals()
    goal_id = sg.create_goal("stretch", "Integrate Smart Governor")
    
    # Create checkpoint
    res = sg.manage_goals("checkpoint_create", {"goal_id": goal_id, "description": "Write diff hashing helper"})
    assert res["success"] is True
    cp_id = res["checkpoint_id"]
    
    # Complete checkpoint
    res = sg.manage_goals("checkpoint_complete", {"checkpoint_id": cp_id})
    assert res["success"] is True
    
    # Verify completed status
    goals = sg.get_goals(type="stretch")
    assert len(goals) == 1
    assert goals[0]["checkpoints"][0]["id"] == cp_id
    assert goals[0]["checkpoints"][0]["achieved"] is True

def test_goals_cli_commands():
    from src.persona import handle_goal_command
    sg = SafeGoals()
    
    # Setup a goal
    goal_id = sg.create_goal("short", "Build MVP")
    
    # Test Prioritize Command (/goal prioritize <id> <tier>)
    resp = handle_goal_command(f"/goal prioritize {goal_id} long")
    assert "[✔] Goal" in resp
    assert "priority tier updated to 'long'" in resp
    
    goals = sg.get_goals(type="long")
    assert len(goals) == 1
    
    # Check invalid priority
    resp = handle_goal_command(f"/goal prioritize {goal_id} super-important")
    assert "[Error]" in resp

# ==========================================
# 2. Context Hydration & Anchoring Tests
# ==========================================

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

# ==========================================
# 3. Smart Governor Tests
# ==========================================

@patch("subprocess.run")
def test_smart_governor_stagnation_checks(mock_run):
    import src.daemon
    
    # Stub git diff to return empty output (meaning no git changes)
    mock_res = mock.Mock()
    mock_res.returncode = 0
    mock_res.stdout = ""
    mock_run.return_value = mock_res
    
    # Initial run (initializes variables, registers progress: returns False)
    stagnant, desc = check_smart_governor_stagnation()
    assert stagnant is False
    assert src.daemon._consecutive_stagnant_cycles == 0
    
    # Second run with same states (no git change, no db write, no checkpoint completion) -> stagnant!
    stagnant, desc = check_smart_governor_stagnation()
    assert stagnant is True
    assert src.daemon._consecutive_stagnant_cycles == 1
    
    # Third run -> stagnant incremented
    stagnant, desc = check_smart_governor_stagnation()
    assert stagnant is True
    assert src.daemon._consecutive_stagnant_cycles == 2

@patch("subprocess.run")
def test_smart_governor_progress_reset(mock_run):
    import src.daemon
    
    # Setup mocks
    mock_res = mock.Mock()
    mock_res.returncode = 0
    mock_res.stdout = ""
    mock_run.return_value = mock_res
    
    # Initialize
    check_smart_governor_stagnation()
    
    # Trigger stagnation
    check_smart_governor_stagnation()
    assert src.daemon._consecutive_stagnant_cycles == 1
    
    # Simulate progress: change git diff output
    mock_res.stdout = "diff --git a/src/main.py b/src/main.py\n+ # some changes"
    
    stagnant, desc = check_smart_governor_stagnation()
    assert stagnant is False
    assert src.daemon._consecutive_stagnant_cycles == 0

# ==========================================
# 4. LLM Caching, Costs & Hyperparameters Tests
# ==========================================

@patch("openai.resources.chat.completions.Completions.create")
def test_llm_cache_and_retry(mock_create):
    # Setup mock completions response
    mock_resp = mock.Mock()
    mock_resp.choices = [mock.Mock(message=mock.Mock(content="Hello cache content"))]
    mock_resp.usage = mock.Mock(prompt_tokens=10, completion_tokens=15)
    mock_create.return_value = mock_resp
    
    # Verify first query (cache miss, runs LLM, caches response)
    res = query_agent("proposer", "Hello caching validation")
    assert res == "Hello cache content"
    assert mock_create.call_count == 1
    
    # Verify second query (cache hit, returns response without calling API)
    res_cached = query_agent("proposer", "Hello caching validation")
    assert res_cached == "Hello cache content"
    assert mock_create.call_count == 1 # Still 1

@patch("openai.resources.chat.completions.Completions.create")
def test_llm_cost_limiting(mock_create):
    mock_resp = mock.Mock()
    mock_resp.choices = [mock.Mock(message=mock.Mock(content="Success response"))]
    mock_resp.usage = mock.Mock(prompt_tokens=1000000, completion_tokens=1000000) # Huge tokens!
    mock_create.return_value = mock_resp
    
    # Seed budget limits to system_config
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
    mock_resp = mock.Mock()
    mock_resp.choices = [mock.Mock(message=mock.Mock(content="Critic response"))]
    mock_resp.usage = mock.Mock(prompt_tokens=10, completion_tokens=15)
    mock_create.return_value = mock_resp
    
    # Verify temp override for Critic
    query_agent("critic", "Auditing safety constraint")
    
    call_args = mock_create.call_args[1]
    assert call_args["temperature"] == 0.0
    assert call_args["top_p"] == 1.0
