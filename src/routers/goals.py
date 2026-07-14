import logging

from fastapi import APIRouter, Depends, HTTPException

from src.routers.dependencies import require_role
from src.skills import SafeGoals

logger = logging.getLogger("JanusWebServer")
router = APIRouter()


@router.get("/api/goals/proposals")
def get_goal_proposals(current_party = Depends(require_role('user'))):
    """Returns subconscious goal proposals awaiting human ratification."""
    try:
        return SafeGoals().get_proposals()
    except Exception as e:
        logger.error(f"Error fetching goal proposals: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
