"""
Observability baseline (issue #63): DB-backed counters shared by GET /metrics,
GET /api/system/metrics, and the /status Persona command, so all three
surfaces read the same numbers and can't drift apart.

Counters are stored in system_config (not true in-process memory) because
Docker deployment runs web_server and daemon as separate OS processes sharing
one DB, and counts must survive a restart so pre/post self-deploy health can
be compared. The increment itself is the same atomic UPDATE
src.database.increment_consecutive_background_loops et al. use — but without
their reread-after-write, since no caller here needs the post-increment value.
"""
from datetime import datetime, timezone

from src.database import get_connection

_COUNTER_KEYS = (
    "metrics.llm_calls_total",
    "metrics.llm_calls_failed_total",
    "metrics.daemon_cycles_total",
    "metrics.skills_executed_total",
    "metrics.skills_failed_total",
    "metrics.skills_sync_failed_total",
    "metrics.http_requests_total",
)


def _increment_counter(config_key: str) -> None:
    """Atomically increments a system_config counter by 1. No caller reads a
    return value, so this is a single UPDATE — not an UPDATE-then-reread —
    since this runs on hot paths (every LLM call, every skill dispatch,
    every HTTP request, every daemon mid-tick)."""
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE system_config
            SET config_value = CAST(CAST(config_value AS INTEGER) + 1 AS TEXT), updated_at = CURRENT_TIMESTAMP
            WHERE config_key = ?;
            """,
            (config_key,),
        )
        conn.commit()
    finally:
        conn.close()


def _get_counter(config_key: str) -> int:
    conn = get_connection(read_only_constitution=True)
    try:
        return _get_counter_on(conn, config_key)
    finally:
        conn.close()


def _get_counter_on(conn, config_key: str) -> int:
    cursor = conn.cursor()
    cursor.execute("SELECT config_value FROM system_config WHERE config_key = ?;", (config_key,))
    row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _count_rows_on(conn, query: str) -> int:
    cursor = conn.cursor()
    cursor.execute(query)
    row = cursor.fetchone()
    return int(row[0]) if row else 0


def increment_llm_calls_total() -> None:
    _increment_counter("metrics.llm_calls_total")


def increment_llm_calls_failed_total() -> None:
    _increment_counter("metrics.llm_calls_failed_total")


def increment_daemon_cycles_total() -> None:
    _increment_counter("metrics.daemon_cycles_total")


def increment_skills_executed_total() -> None:
    _increment_counter("metrics.skills_executed_total")


def increment_skills_failed_total() -> None:
    _increment_counter("metrics.skills_failed_total")


def increment_skills_sync_failed_total() -> None:
    _increment_counter("metrics.skills_sync_failed_total")


def increment_http_requests_total() -> None:
    _increment_counter("metrics.http_requests_total")


def _get_daemon_last_cycle_timestamp() -> str | None:
    from src.routers.health import check_daemon_heartbeat
    _, last_heartbeat_iso = check_daemon_heartbeat()
    return last_heartbeat_iso


def _fmt_sqlite_ts(dt) -> str:
    """Formats a datetime to match SQLite's CURRENT_TIMESTAMP default column
    format ('YYYY-MM-DD HH:MM:SS', space-separated, UTC, no offset) — the
    format llm_call_costs.timestamp, episodic_memory.timestamp,
    pending_escalations.created_at, and swarm_disputes.created_at are all
    actually written in. A naive lexicographic BETWEEN comparison against a
    Python isoformat() string ('...T...+00:00') never matches: at character
    10, space (0x20) sorts below 'T' (0x54), so every real row's timestamp
    compares as "less than" any isoformat() window bound regardless of actual
    time-of-day, silently excluding all rows. Windowed queries against these
    four tables must format bounds through this helper, not .isoformat()."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_iso_ts(dt) -> str:
    """Formats a datetime to match goal_checkpoints.achieved_at's actual write
    format (src/skills.py::SafeGoals.complete_checkpoint uses
    datetime.now(timezone.utc).isoformat(), unlike the CURRENT_TIMESTAMP-backed
    columns _fmt_sqlite_ts() targets) — the two column families use genuinely
    different on-disk timestamp formats, so no single bound format works for
    both."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def get_windowed_cost_total(start: datetime, end: datetime, conn=None) -> float:
    """SUM(cost) FROM llm_call_costs within [start, end] (timezone-aware
    datetimes). Windowed sibling of get_budget_status() (src/llm.py) which is
    hardcoded to date('now') only — this is for the behavioral eval harness's
    (issue #112) cost-per-checkpoint metric, and is the shared instrumentation
    issue #110's cost model is expected to consume."""
    start_str, end_str = _fmt_sqlite_ts(start), _fmt_sqlite_ts(end)
    own_conn = conn is None
    if own_conn:
        conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT SUM(cost) FROM llm_call_costs WHERE timestamp BETWEEN ? AND ?;",
            (start_str, end_str),
        )
        row = cursor.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0
    finally:
        if own_conn:
            conn.close()


def get_windowed_checkpoints_completed(start: datetime, end: datetime, conn=None) -> dict:
    """{"total": N, "autonomous": N} -- windowed variant of get_system_metrics_dict()'s
    all-time goals_checkpoints_completed_total / _completed_autonomously counters,
    filtered on achieved_at BETWEEN ? AND ? instead of unbounded achieved = 1.
    Built for issue #112's behavioral eval harness (Goal Autonomy Rate measurement)."""
    start_str, end_str = _fmt_iso_ts(start), _fmt_iso_ts(end)
    own_conn = conn is None
    if own_conn:
        conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM goal_checkpoints WHERE achieved = 1 AND achieved_at BETWEEN ? AND ?;",
            (start_str, end_str),
        )
        total = int(cursor.fetchone()[0])
        cursor.execute(
            "SELECT COUNT(*) FROM goal_checkpoints WHERE achieved = 1 AND completed_by_party_id = 'system' "
            "AND achieved_at BETWEEN ? AND ?;",
            (start_str, end_str),
        )
        autonomous = int(cursor.fetchone()[0])
        return {"total": total, "autonomous": autonomous}
    finally:
        if own_conn:
            conn.close()


def get_windowed_stagnation_pause_count(start: datetime, end: datetime, conn=None) -> dict:
    """{"stagnation": N, "hard_cap": N} -- counts Smart Loop Governor halt events within
    [start, end] by reading the episodic_memory log text the governor already
    writes (src/daemon.py's stagnation-threshold and hard-cap halt call sites), rather
    than a dedicated event table (none exists). This is a pure SELECT with no daemon.py
    changes, appropriate for issue #112's build+test-only scope; a follow-up issue should
    promote this to a first-class system_config counter (metrics.governor_stagnation_halts_total
    / metrics.governor_hardcap_halts_total) mirroring this module's _increment_counter
    pattern at the two log_episodic_memory("Smart Governor Halt: ...") call sites."""
    start_str, end_str = _fmt_sqlite_ts(start), _fmt_sqlite_ts(end)
    own_conn = conn is None
    if own_conn:
        conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM episodic_memory WHERE speaker = 'system' "
            "AND context_type = 'background_thought' "
            "AND message_content LIKE 'Smart Governor Halt: background cycle stagnation%' "
            "AND timestamp BETWEEN ? AND ?;",
            (start_str, end_str),
        )
        stagnation = int(cursor.fetchone()[0])
        cursor.execute(
            "SELECT COUNT(*) FROM episodic_memory WHERE speaker = 'system' "
            "AND context_type = 'background_thought' "
            "AND message_content LIKE 'Smart Governor Halt: background loop hard cap%' "
            "AND timestamp BETWEEN ? AND ?;",
            (start_str, end_str),
        )
        hard_cap = int(cursor.fetchone()[0])
        return {"stagnation": stagnation, "hard_cap": hard_cap}
    finally:
        if own_conn:
            conn.close()


def get_system_metrics_dict() -> dict:
    """Assembles the full observability snapshot. Shared by /metrics,
    /api/system/metrics, and /status so the three surfaces can't drift.
    Reuses one connection for all system_config/row-count reads instead of
    opening a fresh connection per value — this is a scrape endpoint that
    may be polled frequently."""
    conn = get_connection(read_only_constitution=True)
    try:
        counters = {key.split("metrics.", 1)[1]: _get_counter_on(conn, key) for key in _COUNTER_KEYS}

        episodic_memory_rows = _count_rows_on(conn, "SELECT COUNT(*) FROM episodic_memory;")
        active_goals_count = _count_rows_on(
            conn, "SELECT COUNT(*) FROM goals WHERE status IN ('active', 'in_progress');"
        )
        goals_checkpoints_completed_total = _count_rows_on(
            conn, "SELECT COUNT(*) FROM goal_checkpoints WHERE achieved = 1;"
        )
        goals_checkpoints_completed_autonomously = _count_rows_on(
            conn, "SELECT COUNT(*) FROM goal_checkpoints WHERE achieved = 1 AND completed_by_party_id = 'system';"
        )
    finally:
        conn.close()

    return {
        **counters,
        "daemon_last_cycle_timestamp": _get_daemon_last_cycle_timestamp(),
        "episodic_memory_rows": episodic_memory_rows,
        "active_goals_count": active_goals_count,
        "goals_checkpoints_completed_total": goals_checkpoints_completed_total,
        "goals_checkpoints_completed_autonomously": goals_checkpoints_completed_autonomously,
    }
