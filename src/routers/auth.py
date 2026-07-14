import sqlite3

from fastapi import APIRouter, HTTPException

from src.auth import create_access_token
from src.routers.dependencies import TokenRequest, bootstrap, get_connection

router = APIRouter()

@router.post("/api/v1/auth/token")
def login_for_token(data: TokenRequest):
    """Exchanges a valid username/party_id and enrollment key for a signed JWT access token."""
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, role, public_key FROM parties WHERE id = ? OR name = ?",
            (data.username_or_id, data.username_or_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Invalid Party ID/Username or Enrollment Key")

        stored_key = row["public_key"]
        if not stored_key or stored_key != data.enrollment_key:
            raise HTTPException(status_code=401, detail="Invalid Party ID/Username or Enrollment Key")

        party_id = row["id"]
        role = row["role"]
        token = create_access_token(party_id, role)
        return {"access_token": token, "token_type": "bearer"}
    finally:
        conn.close()


@router.get("/api/v1/bootstrap/status")
def get_bootstrap_status():
    """No Auth: Returns setup alignment wizard completeness."""
    return bootstrap.check_web_ui_bootstrap()
