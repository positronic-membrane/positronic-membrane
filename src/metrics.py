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
from src.database import get_connection

_COUNTER_KEYS = (
    "metrics.llm_calls_total",
    "metrics.llm_calls_failed_total",
    "metrics.daemon_cycles_total",
    "metrics.skills_executed_total",
    "metrics.skills_failed_total",
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


def increment_http_requests_total() -> None:
    _increment_counter("metrics.http_requests_total")


def _get_daemon_last_cycle_timestamp() -> str | None:
    from src.routers.health import check_daemon_heartbeat
    _, last_heartbeat_iso = check_daemon_heartbeat()
    return last_heartbeat_iso


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
