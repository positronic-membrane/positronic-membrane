import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import src.config
from src.database import check_connection, get_connection
from src.memory import check_vector_connection

logger = logging.getLogger("JanusWebServer")
router = APIRouter()

# Staleness threshold scales with the daemon's own configured mid-layer cadence
# (cognitive_layers.cadence_ms), rather than assuming its 5s default forever —
# an operator can retune that cadence at runtime via SafeDB.
DAEMON_STALE_MULTIPLIER = 5
DAEMON_STALE_MIN_SECONDS = 30


def _parse_timestamp(value) -> datetime | None:
    """Normalizes a cognitive_layers.last_run_at value (sqlite naive UTC string
    or Postgres datetime) into an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def check_daemon_heartbeat() -> tuple[bool, str | None]:
    """Returns (is_fresh, last_heartbeat_iso_or_None) for the 'mid' cognitive layer."""
    conn = None
    try:
        conn = get_connection(read_only_constitution=True)
        cursor = conn.execute("SELECT last_run_at, cadence_ms FROM cognitive_layers WHERE layer_name = 'mid';")
        row = cursor.fetchone()
    except Exception as e:
        logger.error(f"Health check: failed to query daemon heartbeat: {e}")
        return False, None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if not row or row[0] is None:
        return False, None

    last_run_at = _parse_timestamp(row[0])
    if last_run_at is None:
        return False, None

    cadence_seconds = (row[1] or 5000) / 1000
    threshold = max(DAEMON_STALE_MIN_SECONDS, DAEMON_STALE_MULTIPLIER * cadence_seconds)
    age_seconds = (datetime.now(timezone.utc) - last_run_at).total_seconds()
    # No lower bound on age_seconds: a small negative value just means clock
    # skew made last_run_at look slightly ahead of "now" — still fresh.
    is_fresh = age_seconds <= threshold
    return is_fresh, last_run_at.isoformat()


def _run_checks() -> tuple[bool, bool, bool, str | None]:
    """Runs the three underlying health checks once. Returns (db_ok, vector_ok, daemon_ok, last_heartbeat)."""
    db_ok = check_connection()
    vector_ok = check_vector_connection()
    daemon_ok, last_heartbeat = check_daemon_heartbeat()
    return db_ok, vector_ok, daemon_ok, last_heartbeat


@router.get("/healthz")
def healthz():
    """Liveness probe: 200 if the process can respond at all. No DB/vector/daemon checks."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz():
    """Readiness probe: 200 only if DB + vector DB are connected and the daemon heartbeat is fresh."""
    db_ok, vector_ok, daemon_ok, _ = _run_checks()
    ready = db_ok and vector_ok and daemon_ok
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ok" if ready else "not_ready"},
    )


@router.get("/health")
def health():
    """Full health diagnostic: DB, vector DB, daemon heartbeat, LLM API config, uptime."""
    db_ok, vector_ok, daemon_ok, last_heartbeat = _run_checks()
    llm_configured = bool(src.config.LLM_API_KEY)

    if db_ok and vector_ok and daemon_ok:
        overall = "ok"
    elif db_ok:
        overall = "degraded"
    else:
        overall = "down"

    body = {
        "status": overall,
        "database": "ok" if db_ok else "down",
        "vector_db": "ok" if vector_ok else "down",
        "daemon": {
            "running": daemon_ok,
            "last_heartbeat": last_heartbeat,
        },
        "llm_api": {"configured": llm_configured},
        "uptime_seconds": round(time.monotonic() - src.config.PROCESS_START_TIME, 3),
    }
    return JSONResponse(status_code=200 if overall == "ok" else 503, content=body)
