"""Escalation-quality windowed reads for the behavioral evaluation harness
(issue #112). Cost/checkpoint/stagnation windowed aggregates live in
src/metrics.py instead (per docs/successor_spec.md §4.9: "#112's benchmark
and #110's cost model consume these counters") so #110 can reuse them without
depending on this benchmarks/ package. Escalation quality is benchmark-scoring
specific and stays here."""
from src.database import get_connection
from src.metrics import _fmt_sqlite_ts


def get_windowed_escalations(start, end, conn=None) -> list:
    """Reads pending_escalations and swarm_disputes rows created within
    [start, end] (timezone-aware datetimes), normalized into a common shape
    for judge.py's score_escalation(). Returns [] if none occurred --
    callers must render that as "N/A (no escalations occurred)", not treat
    it as an error, since a short bounded sandbox run may legitimately
    produce zero escalations.

    Both tables' created_at columns are CURRENT_TIMESTAMP-backed, so bounds
    are formatted via src.metrics._fmt_sqlite_ts — see that helper's
    docstring for why a plain .isoformat() bound silently matches nothing."""
    start_str, end_str = _fmt_sqlite_ts(start), _fmt_sqlite_ts(end)
    own_conn = conn is None
    if own_conn:
        conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        results = []

        cursor.execute(
            "SELECT id, party_id, source, summary, detail, status, created_at, delivered_at "
            "FROM pending_escalations WHERE created_at BETWEEN ? AND ?;",
            (start_str, end_str),
        )
        for row in cursor.fetchall():
            results.append({
                "kind": "pending_escalation",
                "id": row[0],
                "party_id": row[1],
                "source": row[2],
                "summary": row[3],
                "detail": row[4],
                "status": row[5],
                "created_at": row[6],
                "resolved_at": row[7],
            })

        cursor.execute(
            "SELECT id, created_at, proposed_action, debate_transcript, veto_count, status, "
            "resolution, resolution_notes, resolved_at FROM swarm_disputes "
            "WHERE created_at BETWEEN ? AND ?;",
            (start_str, end_str),
        )
        for row in cursor.fetchall():
            results.append({
                "kind": "swarm_dispute",
                "id": row[0],
                "created_at": row[1],
                "proposed_action": row[2],
                "debate_transcript": row[3],
                "veto_count": row[4],
                "status": row[5],
                "resolution": row[6],
                "resolution_notes": row[7],
                "resolved_at": row[8],
            })

        return results
    finally:
        if own_conn:
            conn.close()
