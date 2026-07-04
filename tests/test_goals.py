import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import src.config
from src.database import get_connection, init_db
from src.persona import handle_goal_command
from src.skills import DynamicSkillExecutor, SafeGoals
from src.web_server import app


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Redirects config.DB_PATH to a temp file and seeds it."""
    temp_db = tmp_path / "test_janus_goals.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)

    init_db()

    yield

    src.config.DB_PATH = orig_db_path

def test_safe_goals_crud():
    sg = SafeGoals()

    # Verify default aspirational goal exists
    goals = sg.get_goals()
    assert len(goals) == 1
    assert goals[0]["type"] == "aspirational"
    assert goals[0]["status"] == "active"
    assert "cognitive architecture" in goals[0]["description"]

    # Create short-term goal
    gid = sg.create_goal("short", "Implement Goal System", parent_goal_id=goals[0]["id"])
    assert gid > 0

    goals_after = sg.get_goals()
    assert len(goals_after) == 2

    short_goal = next(g for g in goals_after if g["id"] == gid)
    assert short_goal["type"] == "short"
    assert short_goal["status"] == "proposed"
    assert short_goal["parent_goal_id"] == goals[0]["id"]

    # Update status
    success = sg.update_goal_status(gid, "active")
    assert success is True

    goals_active = sg.get_goals(status="active")
    assert any(g["id"] == gid for g in goals_active)

    # Add checkpoints
    cpid1 = sg.add_checkpoint(gid, "Write implementation plan")
    cpid2 = sg.add_checkpoint(gid, "Write unit tests")
    assert cpid1 > 0
    assert cpid2 > 0

    goals_with_cp = sg.get_goals()
    my_goal = next(g for g in goals_with_cp if g["id"] == gid)
    assert len(my_goal["checkpoints"]) == 2
    assert any(cp["description"] == "Write implementation plan" for cp in my_goal["checkpoints"])

    # Complete a checkpoint
    success_cp = sg.complete_checkpoint(cpid1)
    assert success_cp is True

    goals_part_done = sg.get_goals()
    my_goal_part = next(g for g in goals_part_done if g["id"] == gid)
    cp1 = next(cp for cp in my_goal_part["checkpoints"] if cp["id"] == cpid1)
    assert cp1["achieved"] is True
    assert cp1["achieved_at"] is not None

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

def test_goal_slash_commands():
    sg = SafeGoals()

    # Test listing goals
    res_list = handle_goal_command("/goal")
    assert "🎯 Project Janus Goal Registry" in res_list
    assert "Aspirational-Term Goals" in res_list
    assert "Refine internal cognitive architecture" in res_list

    # Test creating goal
    res_create = handle_goal_command("/goal create short Code Phase 4 features")
    assert "successfully created" in res_create

    goals = sg.get_goals(type="short")
    assert len(goals) == 1
    gid = goals[0]["id"]
    assert goals[0]["description"] == "Code Phase 4 features"

    # Test setting status
    res_status = handle_goal_command(f"/goal status {gid} active")
    assert "status updated" in res_status

    goals_updated = sg.get_goals(status="active")
    assert any(g["id"] == gid for g in goals_updated)

    # Test adding checkpoint
    res_cp = handle_goal_command(f"/goal checkpoint {gid} Add tests")
    assert "Checkpoint" in res_cp

    goals_with_cp = sg.get_goals()
    my_goal = next(g for g in goals_with_cp if g["id"] == gid)
    assert len(my_goal["checkpoints"]) == 1
    cpid = my_goal["checkpoints"][0]["id"]

    # Test completing checkpoint
    res_complete = handle_goal_command(f"/goal complete {cpid}")
    assert "marked as completed" in res_complete

    goals_completed_cp = sg.get_goals()
    my_goal_done = next(g for g in goals_completed_cp if g["id"] == gid)
    assert my_goal_done["checkpoints"][0]["achieved"] is True

def test_goals_cli_commands():
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

def test_evaluate_goals_skill():
    sg = SafeGoals()

    # Create short-term goal
    gid = sg.create_goal("short", "Evaluate goals test")
    sg.update_goal_status(gid, "active")
    cpid1 = sg.add_checkpoint(gid, "Checkpoint 1")
    cpid2 = sg.add_checkpoint(gid, "Checkpoint 2")

    # Run dynamic skill, should not complete yet since checkpoints are incomplete
    party_id = "system"
    res1 = DynamicSkillExecutor.execute("evaluate_goals", {}, party_id=party_id)
    assert res1["success"] is True
    assert "no status transitions" in res1["result"].lower()

    # Complete checkpoints
    sg.complete_checkpoint(cpid1)
    sg.complete_checkpoint(cpid2)

    # Run dynamic skill again, should auto-complete the active goal
    res2 = DynamicSkillExecutor.execute("evaluate_goals", {}, party_id=party_id)
    assert res2["success"] is True
    assert f"completed: goal [{gid}]" in res2["result"].lower()

    # Check status in DB
    goals = sg.get_goals()
    my_goal = next(g for g in goals if g["id"] == gid)
    assert my_goal["status"] == "completed"

    # Verify episodic log is recorded
    conn = get_connection()
    try:
        row = conn.execute("SELECT speaker, message_content FROM episodic_memory WHERE context_type = 'background_thought' ORDER BY id DESC LIMIT 1;").fetchone()
        assert row is not None
        assert row[0] == "system"
        assert "Autonomous Goal Achievement" in row[1]
    finally:
        conn.close()

def test_aspirational_goals_do_not_auto_complete():
    sg = SafeGoals()

    # Retrieve default aspirational goal
    goals = sg.get_goals(type="aspirational")
    gid = goals[0]["id"]

    # Add a checkpoint
    cpid = sg.add_checkpoint(gid, "Aspirational step")
    sg.complete_checkpoint(cpid)

    # Run dynamic skill, aspirational goals should ignore auto-completion checklist
    party_id = "system"
    res = DynamicSkillExecutor.execute("evaluate_goals", {}, party_id=party_id)
    assert res["success"] is True
    assert "no status transitions" in res["result"].lower()

    # Aspirational goal stays active
    goals_after = sg.get_goals(type="aspirational")
    assert goals_after[0]["status"] == "active"

def test_safe_goals_proposal_crud():
    sg = SafeGoals()

    # No proposals initially
    assert sg.get_proposals() == []

    # Propose a goal
    pid = sg.propose_goal("short", "Investigate flaky sandbox tests", 0.75, "Recurring failures in episodic logs")
    assert pid > 0

    proposals = sg.get_proposals()
    assert len(proposals) == 1
    assert proposals[0]["id"] == pid
    assert proposals[0]["status"] == "proposed"
    assert proposals[0]["type"] == "short"
    assert proposals[0]["confidence_score"] == 0.75

    # Filter by status
    assert len(sg.get_proposals(status="proposed")) == 1
    assert len(sg.get_proposals(status="approved")) == 0

    # Invalid type rejected
    with pytest.raises(ValueError):
        sg.propose_goal("urgent", "Bad type", 0.5, "n/a")

    # Approve promotes into goals table as 'proposed'
    res = sg.approve_proposal(pid)
    assert res["success"] is True
    goal_id = res["goal_id"]

    goals = sg.get_goals()
    promoted = next(g for g in goals if g["id"] == goal_id)
    assert promoted["status"] == "proposed"
    assert promoted["description"] == "Investigate flaky sandbox tests"
    assert promoted["type"] == "short"

    proposals_after = sg.get_proposals()
    assert proposals_after[0]["status"] == "approved"

    # Cannot re-approve or reject an already-resolved proposal
    with pytest.raises(ValueError):
        sg.approve_proposal(pid)
    with pytest.raises(ValueError):
        sg.reject_proposal(pid)

    # Reject a second proposal
    pid2 = sg.propose_goal("long", "Build skill staging harness", 0.6, "Predecessor unblocked")
    res2 = sg.reject_proposal(pid2)
    assert res2["success"] is True
    rejected = next(p for p in sg.get_proposals() if p["id"] == pid2)
    assert rejected["status"] == "rejected"

    # Unknown proposal id
    with pytest.raises(ValueError):
        sg.approve_proposal(99999)

@patch("src.skills.send_webhook_notification")
def test_propose_goal_sends_webhook_notification(mock_webhook):
    sg = SafeGoals()
    pid = sg.propose_goal("stretch", "Audit sandbox container hardening", 0.75, "Recurring CVE chatter")

    mock_webhook.assert_called_once()
    event_type, message = mock_webhook.call_args[0]
    assert event_type == "goal_proposal"
    assert f"#{pid}" in message
    assert "Audit sandbox container hardening" in message

def test_goal_proposals_slash_commands():
    sg = SafeGoals()

    # Empty state
    res_empty = handle_goal_command("/goal proposals")
    assert "No goal proposals pending" in res_empty

    pid = sg.propose_goal("stretch", "Adopt smart governor v2", 0.9, "Stagnation cycles increasing")

    res_list = handle_goal_command("/goal proposals")
    assert "Subconscious Goal Proposals" in res_list
    assert f"[{pid}]" in res_list
    assert "Adopt smart governor v2" in res_list
    assert "0.90" in res_list

    # Approve via CLI
    res_approve = handle_goal_command(f"/goal approve {pid}")
    assert "[✔]" in res_approve
    assert "approved and promoted to Goal" in res_approve

    goals = sg.get_goals(type="stretch")
    assert any(g["description"] == "Adopt smart governor v2" for g in goals)

    # Re-approving fails gracefully
    res_reapprove = handle_goal_command(f"/goal approve {pid}")
    assert "[Error]" in res_reapprove

    # Reject a different proposal
    pid2 = sg.propose_goal("short", "Tune llm_cache TTL", 0.4, "Disk growth observed")
    res_reject = handle_goal_command(f"/goal reject {pid2}")
    assert "[✔]" in res_reject
    assert "rejected" in res_reject

    # Invalid id
    res_bad = handle_goal_command("/goal approve not-a-number")
    assert "[Error] Proposal ID must be an integer." in res_bad

def _reflection_side_effect(proposal_response):
    """query_agent side effect driving a full reflection cycle; proposal_response
    is returned for the propose_goals candidate-goal prompt."""
    def side_effect(agent_id, prompt, system_override=None, **kwargs):
        if "candidate goals" in prompt.lower():
            return proposal_response
        if agent_id == "proposer":
            return "PROPOSED_ACTION: Review recent episodic logs"
        elif agent_id == "critic":
            return "Decision: 1\nJustification: Safe."
        elif agent_id == "archivist":
            if "curiosity_topics" in prompt.lower():
                return "CURIOSITY_TOPICS: sandbox egress, network policy"
            return "Execution summary nugget."
        return ""
    return side_effect

def test_reflection_cycle_generates_goal_proposal():
    sg = SafeGoals()

    proposal_json = json.dumps([
        {
            "type": "short",
            "description": "Audit sandbox network egress rules",
            "confidence": 0.8,
            "source_reason": "Repeated boundary_encounter events",
        }
    ])

    with patch("src.skills.query_agent", side_effect=_reflection_side_effect(proposal_json)), \
         patch("src.memory.query_memories", return_value=[]), \
         patch("src.memory.add_memory", return_value=None), \
         patch("src.skills.get_active_curiosity_topics", return_value=[]), \
         patch("src.skills.update_curiosity_topics", return_value=None), \
         patch("src.skills.send_webhook_notification") as mock_webhook:
        res = DynamicSkillExecutor.execute("run_reflection_cycle", {}, party_id="system")

    assert res["success"] is True

    proposals = sg.get_proposals()
    assert len(proposals) == 1
    assert proposals[0]["description"] == "Audit sandbox network egress rules"
    assert proposals[0]["type"] == "short"
    assert proposals[0]["confidence_score"] == 0.8
    assert proposals[0]["status"] == "proposed"

    # V2-T9 goal_proposal webhook fires on autonomous generation
    webhook_events = [call.args[0] for call in mock_webhook.call_args_list]
    assert "goal_proposal" in webhook_events

def test_reflection_cycle_proposal_generation_capped():
    sg = SafeGoals()
    for i in range(3):
        sg.propose_goal("short", f"Pending proposal {i}", 0.5, "seed")

    proposal_prompts = []

    def side_effect(agent_id, prompt, system_override=None, **kwargs):
        if "candidate goals" in prompt.lower():
            proposal_prompts.append(prompt)
            return '[{"type": "short", "description": "Must not be inserted", "confidence": 0.9, "source_reason": "x"}]'
        return _reflection_side_effect("")(agent_id, prompt, system_override, **kwargs)

    with patch("src.skills.query_agent", side_effect=side_effect), \
         patch("src.memory.query_memories", return_value=[]), \
         patch("src.memory.add_memory", return_value=None), \
         patch("src.skills.get_active_curiosity_topics", return_value=[]), \
         patch("src.skills.update_curiosity_topics", return_value=None):
        res = DynamicSkillExecutor.execute("run_reflection_cycle", {}, party_id="system")

    assert res["success"] is True
    # Cap short-circuits before the proposal LLM call; no new rows appear
    assert proposal_prompts == []
    assert len(sg.get_proposals(status="proposed")) == 3

def test_reflection_cycle_tolerates_unparseable_proposal_response():
    sg = SafeGoals()

    with patch("src.skills.query_agent",
               side_effect=_reflection_side_effect("We should definitely explore many things.")), \
         patch("src.memory.query_memories", return_value=[]), \
         patch("src.memory.add_memory", return_value=None), \
         patch("src.skills.get_active_curiosity_topics", return_value=[]), \
         patch("src.skills.update_curiosity_topics", return_value=None):
        res = DynamicSkillExecutor.execute("run_reflection_cycle", {}, party_id="system")

    assert res["success"] is True
    assert sg.get_proposals() == []

def test_propose_goals_skill_direct_invocation():
    """The propose_goals skill can also be run standalone (e.g. via /runskill)."""
    sg = SafeGoals()
    proposal_json = json.dumps([
        {"type": "stretch", "description": "Prototype curiosity-driven crawl planner",
         "confidence": 1.7, "source_reason": ""}
    ])

    with patch("src.skills.query_agent", return_value=proposal_json), \
         patch("src.skills.get_active_curiosity_topics", return_value=["crawl planning"]):
        res = DynamicSkillExecutor.execute("propose_goals", {}, party_id="system")

    assert res["success"] is True
    proposals = sg.get_proposals(status="proposed")
    assert len(proposals) == 1
    assert proposals[0]["type"] == "stretch"
    # Confidence is clamped into [0, 1] and empty source_reason gets a default
    assert proposals[0]["confidence_score"] == 1.0
    assert proposals[0]["source_reason"] == "Subconscious proposal from idle reflection."

def test_goal_proposal_cap_config_seeded():
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT config_value, is_agent_modifiable FROM system_config "
            "WHERE config_key = 'goal_proposal.max_open_proposals';"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "3"
    assert row[1] == 0

def test_api_get_goal_proposals():
    sg = SafeGoals()
    sg.propose_goal("long", "Expose proposals over the API", 0.55, "API parity with CLI")

    client = TestClient(app)
    orig_require = src.config.REQUIRE_AUTH
    try:
        src.config.REQUIRE_AUTH = False
        resp = client.get("/api/goals/proposals")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["description"] == "Expose proposals over the API"
    finally:
        src.config.REQUIRE_AUTH = orig_require
