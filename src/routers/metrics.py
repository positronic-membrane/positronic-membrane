import logging

from fastapi import APIRouter, Depends

from src.metrics import get_system_metrics_dict
from src.routers.dependencies import require_role

logger = logging.getLogger("JanusWebServer")
router = APIRouter()


@router.get("/metrics")
def get_metrics():
    """Unauthenticated scrape endpoint: llm/skill/daemon/http counters plus
    live episodic-memory/goal counts. No Prometheus dependency — plain JSON."""
    return get_system_metrics_dict()


@router.get("/api/system/metrics")
def get_system_metrics(current_party = Depends(require_role('user'))):
    """Same payload as GET /metrics, behind auth — the V1 DoD-required path."""
    return get_system_metrics_dict()
