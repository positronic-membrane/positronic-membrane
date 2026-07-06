"""End-to-end: daemon reflection cycle -> goal proposals -> episodic memory -> consolidation.

Drives the same dynamic skills the daemon's own high/mid-layer ticks call
(run_reflection_cycle, consolidate_memories) directly and synchronously via
DynamicSkillExecutor, rather than the async heartbeat loop + sleep/cancel
pattern used in tests/test_daemon.py — these skill calls are already
deterministic, so no sleeping is required.
"""

import pytest

from src.database import get_connection
from src.memory import get_collection
from src.skills import DynamicSkillExecutor

pytestmark = pytest.mark.e2e


def _count(sql, params=()):
    conn = get_connection()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def test_reflection_cycle_produces_goal_and_memory(daemon_llm_script):
    result = DynamicSkillExecutor.execute("run_reflection_cycle", {}, party_id="system")
    assert result["success"] is True

    assert _count("SELECT COUNT(*) FROM internal_deliberations;") >= 1

    assert (
        _count("SELECT COUNT(*) FROM goal_proposals WHERE status = 'proposed';") >= 1
    )

    assert (
        _count(
            "SELECT COUNT(*) FROM episodic_memory WHERE context_type = 'background_thought';"
        )
        >= 1
    )

    details_before = get_collection("janus_details").get(where={"consolidated": "false"})
    assert details_before["ids"], "reflection cycle should have archived an unconsolidated detail memory"

    consolidate_result = DynamicSkillExecutor.execute(
        "consolidate_memories", {}, party_id="system"
    )
    assert consolidate_result["success"] is True

    details_after = get_collection("janus_details").get(where={"consolidated": "true"})
    assert details_after["ids"], "consolidate_memories should have marked the detail entry consolidated"

    long_term = get_collection("janus_long_term").get()
    assert long_term["ids"], "consolidate_memories should have written a Primary Concept to janus_long_term"


def test_goal_proposal_budget_cap_respected(daemon_llm_script):
    conn = get_connection()
    try:
        for i in range(3):
            conn.execute(
                "INSERT INTO goal_proposals (type, description, confidence_score, source_reason, status) "
                "VALUES ('short', ?, 0.5, 'pre-seeded for budget cap test', 'proposed');",
                (f"pre-seeded proposal {i}",),
            )
        conn.commit()
    finally:
        conn.close()

    assert _count("SELECT COUNT(*) FROM goal_proposals WHERE status = 'proposed';") == 3

    result = DynamicSkillExecutor.execute("run_reflection_cycle", {}, party_id="system")
    assert result["success"] is True

    # propose_goals' own rate-limit (goal_proposal.max_open_proposals, default 3)
    # must have skipped generation — no new row beyond the 3 pre-seeded ones.
    assert _count("SELECT COUNT(*) FROM goal_proposals WHERE status = 'proposed';") == 3
