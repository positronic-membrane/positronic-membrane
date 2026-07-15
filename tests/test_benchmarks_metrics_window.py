from datetime import datetime, timedelta, timezone

from benchmarks.metrics_window import get_windowed_escalations
from src.database import get_connection
from src.metrics import (
    _fmt_iso_ts,
    _fmt_sqlite_ts,
    get_windowed_checkpoints_completed,
    get_windowed_cost_total,
    get_windowed_stagnation_pause_count,
)

WINDOW_START_DT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
WINDOW_END_DT = datetime(2026, 1, 1, 23, 59, 59, tzinfo=timezone.utc)
BEFORE_WINDOW_DT = WINDOW_START_DT - timedelta(days=1)
AFTER_WINDOW_DT = WINDOW_END_DT + timedelta(days=1)


def test_fmt_sqlite_ts_matches_sqlite_current_timestamp_format():
    conn = get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS _fmt_probe (ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO _fmt_probe DEFAULT VALUES")
    conn.commit()
    real_ts = conn.execute("SELECT ts FROM _fmt_probe").fetchone()[0]
    conn.close()

    # Both must be the same length/shape for lexicographic BETWEEN to work.
    assert len(real_ts) == len(_fmt_sqlite_ts(datetime.now(timezone.utc)))
    assert " " in real_ts and "T" not in real_ts


def test_fmt_iso_ts_matches_achieved_at_write_format():
    # src/skills.py::SafeGoals.complete_checkpoint writes datetime.now(timezone.utc).isoformat()
    real_write = datetime.now(timezone.utc).isoformat()
    formatted = _fmt_iso_ts(datetime.now(timezone.utc))
    assert "T" in formatted and "+00:00" in formatted
    assert real_write[:10] == formatted[:10]


def _insert_cost(timestamp_str, cost):
    conn = get_connection()
    conn.execute(
        "INSERT INTO llm_call_costs (query_id, model, input_tokens, output_tokens, cost, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?);",
        ("proposer", "test-model", 10, 10, cost, timestamp_str),
    )
    conn.commit()
    conn.close()


def test_get_windowed_cost_total_sums_only_in_window_rows():
    _insert_cost(_fmt_sqlite_ts(WINDOW_START_DT), 0.01)
    _insert_cost(_fmt_sqlite_ts(WINDOW_END_DT), 0.02)
    _insert_cost(_fmt_sqlite_ts(BEFORE_WINDOW_DT), 100.0)
    _insert_cost(_fmt_sqlite_ts(AFTER_WINDOW_DT), 100.0)

    total = get_windowed_cost_total(WINDOW_START_DT, WINDOW_END_DT)
    assert total == 0.03


def test_get_windowed_cost_total_matches_real_sqlite_default_timestamp():
    """Regression test for the original bug: inserting via the real
    CURRENT_TIMESTAMP default (not a hand-formatted string) must still be
    picked up by a window that safely brackets "now"."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO llm_call_costs (query_id, model, input_tokens, output_tokens, cost) "
        "VALUES ('proposer', 'test-model', 10, 10, 0.05);"
    )
    conn.commit()
    conn.close()

    now = datetime.now(timezone.utc)
    total = get_windowed_cost_total(now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert total == 0.05


def test_get_windowed_cost_total_empty_is_zero():
    assert get_windowed_cost_total(WINDOW_START_DT, WINDOW_END_DT) == 0.0


def _insert_goal_and_checkpoint(achieved_at_str, completed_by_party_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO goals (type, status, description) VALUES ('short', 'active', 'test goal');"
    )
    goal_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO goal_checkpoints (goal_id, checkpoint_description, achieved, achieved_at, completed_by_party_id) "
        "VALUES (?, 'test checkpoint', 1, ?, ?);",
        (goal_id, achieved_at_str, completed_by_party_id),
    )
    conn.commit()
    conn.close()


def test_get_windowed_checkpoints_completed_counts_total_and_autonomous():
    _insert_goal_and_checkpoint(_fmt_iso_ts(WINDOW_START_DT), "system")
    _insert_goal_and_checkpoint(_fmt_iso_ts(WINDOW_END_DT), "human_party_1")
    _insert_goal_and_checkpoint(_fmt_iso_ts(BEFORE_WINDOW_DT), "system")  # outside window, excluded
    _insert_goal_and_checkpoint(_fmt_iso_ts(AFTER_WINDOW_DT), "system")  # outside window, excluded

    result = get_windowed_checkpoints_completed(WINDOW_START_DT, WINDOW_END_DT)
    assert result == {"total": 2, "autonomous": 1}


def test_get_windowed_checkpoints_completed_empty():
    assert get_windowed_checkpoints_completed(WINDOW_START_DT, WINDOW_END_DT) == {"total": 0, "autonomous": 0}


def _insert_governor_halt(timestamp_str, message):
    conn = get_connection()
    conn.execute(
        "INSERT INTO episodic_memory (speaker, message_content, context_type, timestamp) "
        "VALUES ('system', ?, 'background_thought', ?);",
        (message, timestamp_str),
    )
    conn.commit()
    conn.close()


_STAGNATION_MSG = "Smart Governor Halt: background cycle stagnation threshold of 3 met. Pausing background automations."
_HARD_CAP_MSG = "Smart Governor Halt: background loop hard cap of 20 exceeded. Pausing background automations."


def test_get_windowed_stagnation_pause_count_distinguishes_kinds():
    _insert_governor_halt(_fmt_sqlite_ts(WINDOW_START_DT), _STAGNATION_MSG)
    _insert_governor_halt(_fmt_sqlite_ts(WINDOW_END_DT), _HARD_CAP_MSG)
    _insert_governor_halt(_fmt_sqlite_ts(WINDOW_START_DT), "Unrelated background thought.")
    _insert_governor_halt(_fmt_sqlite_ts(BEFORE_WINDOW_DT), _STAGNATION_MSG)

    result = get_windowed_stagnation_pause_count(WINDOW_START_DT, WINDOW_END_DT)
    assert result == {"stagnation": 1, "hard_cap": 1}


def test_get_windowed_escalations_empty_list():
    assert get_windowed_escalations(WINDOW_START_DT, WINDOW_END_DT) == []


def test_get_windowed_escalations_returns_both_kinds_in_window_only():
    conn = get_connection()
    conn.execute(
        "INSERT INTO pending_escalations (party_id, source, summary, detail, created_at) "
        "VALUES ('p1', 'agent_status_blocked', 'blocked PR', 'details here', ?);",
        (_fmt_sqlite_ts(WINDOW_START_DT),),
    )
    conn.execute(
        "INSERT INTO pending_escalations (party_id, source, summary, detail, created_at) "
        "VALUES ('p1', 'agent_status_blocked', 'old blocked PR', 'old details', ?);",
        (_fmt_sqlite_ts(BEFORE_WINDOW_DT),),
    )
    conn.execute(
        "INSERT INTO swarm_disputes (proposed_action, debate_transcript, veto_count, created_at) "
        "VALUES ('risky action', 'debate text', 3, ?);",
        (_fmt_sqlite_ts(WINDOW_END_DT),),
    )
    conn.commit()
    conn.close()

    results = get_windowed_escalations(WINDOW_START_DT, WINDOW_END_DT)
    kinds = sorted(r["kind"] for r in results)
    assert kinds == ["pending_escalation", "swarm_dispute"]
    assert len(results) == 2
