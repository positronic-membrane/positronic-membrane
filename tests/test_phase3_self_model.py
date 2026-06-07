import os
import pytest
import sqlite3
from unittest.mock import patch
import src.config
from src.database import init_db, get_connection
from src.skills import SafeSelfModel, DynamicSkillExecutor
from src.persona import (
    handle_self_command,
    handle_pin_command,
    handle_unpin_command,
    generate_persona_response
)

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Redirects config.DB_PATH to a temp file and seeds it."""
    temp_db = tmp_path / "test_janus_phase3.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    
    init_db()
    
    yield
    
    src.config.DB_PATH = orig_db_path

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
    assert traits["curiosity"]["confidence"] == 1.0 # pins to 1.0
    
    # Attempting to update a pinned trait via SafeSelfModel should return False and not change anything
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
    # Decay rate is 0.01, so diff (0.8 - 0.5 = 0.3) * 0.01 = 0.003
    # 0.8 - 0.003 = 0.797
    assert traits_after["verbosity"]["value"] < 0.8
    # Confidence decays by 0.005
    # 0.8 - 0.005 = 0.795
    assert traits_after["verbosity"]["confidence"] == 0.795
