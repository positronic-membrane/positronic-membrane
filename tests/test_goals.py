import pytest

import src.config
from src.database import get_connection, init_db
from src.persona import handle_goal_command
from src.skills import DynamicSkillExecutor, SafeGoals


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
