import logging
from fastapi import APIRouter, Depends

from src.routers.dependencies import require_role
import src.daemon as daemon

logger = logging.getLogger("JanusWebServer")
router = APIRouter()


@router.get("/api/governor/status")
def get_governor_status(current_party = Depends(require_role('user'))):
    """Returns Smart Loop Governor state: pause status, stagnation counters, thresholds."""
    return daemon.get_governor_status_dict()
