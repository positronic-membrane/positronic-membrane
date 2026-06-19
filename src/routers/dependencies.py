import os
import json
import logging
import uuid
import re
import sqlite3
import asyncio
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Optional, List, Dict, Any
from collections import defaultdict
from functools import lru_cache

from fastapi import Request, HTTPException, status, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel

from src.database import (
    log_episodic_memory,
    get_recent_episodic_memories,
    get_constitution
)

def get_connection(*args, **kwargs):
    import src.database
    return src.database.get_connection(*args, **kwargs)
from src.memory_orchestrator import MemoryOrchestrator
from src.role_bootstrap import RoleBootstrap
from src.auth import decode_access_token, create_access_token
import src.config

logger = logging.getLogger("JanusWebServer")

ROLE_HIERARCHY = {
    'observer': 0,
    'user': 1,
    'contributor': 2,
    'admin': 3
}

memory_orch = MemoryOrchestrator()
bootstrap = RoleBootstrap()
ip_request_history = defaultdict(list)

# --- Pydantic Request Models ---

class ChatRequest(BaseModel):
    message: str

class SandboxActionRequest(BaseModel):
    action: str
    name: Optional[str] = None

class StageActionRequest(BaseModel):
    action: str
    file_path: Optional[str] = None
    instructions: Optional[str] = None

class ConstitutionAmendRequest(BaseModel):
    key: str
    text: str

class ConstitutionDeleteRequest(BaseModel):
    key: str

class RegistryUpdateRequest(BaseModel):
    agent_id: str
    model: Optional[str] = None

class RegistryRulesUpdateRequest(BaseModel):
    action: str
    agent_id: Optional[str] = None
    rule_key: Optional[str] = None
    rule_text: Optional[str] = None
    is_active: Optional[bool] = True

class PartyRegisterRequest(BaseModel):
    name: str
    role: Optional[str] = "user"
    public_key: Optional[str] = None
    metadata: Optional[dict] = {}

class MemorySetRequest(BaseModel):
    key: str
    value: Any
    namespace: Optional[str] = "global"

class ModificationCreateRequest(BaseModel):
    feature: str
    diff: str
    change_type: Optional[str] = "modify"
    change_resource: Optional[str] = "code"

class PartyRoleUpdateRequest(BaseModel):
    role: str

class TokenRequest(BaseModel):
    username_or_id: str
    enrollment_key: str


# --- Helper & Dependency Functions ---

def verify_role(party_role: str, minimum_role: str) -> bool:
    """Check if a party's role meets the minimum required role."""
    return ROLE_HIERARCHY.get(party_role, -1) >= ROLE_HIERARCHY.get(minimum_role, 0)


@lru_cache(maxsize=128)
def resolve_party_by_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        # 1. Match public_key column
        row = conn.execute("SELECT id, role FROM parties WHERE public_key = ? LIMIT 1;", (api_key,)).fetchone()
        if row:
            return {"party_id": row["id"], "role": row["role"]}
        # 2. Match metadata JSON key
        row = conn.execute("SELECT id, role FROM parties WHERE json_extract(metadata, '$.api_key') = ? LIMIT 1;", (api_key,)).fetchone()
        if row:
            return {"party_id": row["id"], "role": row["role"]}
    except Exception as e:
        logger.error(f"Error resolving party by API key: {e}")
    finally:
        conn.close()
    return None

@lru_cache(maxsize=128)
def resolve_party_by_fingerprint(fingerprint: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT id, role FROM parties WHERE json_extract(metadata, '$.device_fingerprint') = ? LIMIT 1;", (fingerprint,)).fetchone()
        if row:
            return {"party_id": row["id"], "role": row["role"]}
    except Exception as e:
        logger.error(f"Error resolving party by fingerprint: {e}")
    finally:
        conn.close()
    return None

def get_current_party(request: Request) -> Dict[str, Any]:
    """Dependency to verify JWT access token or fallback to API Key or Fingerprint checks."""
    api_key_header = request.headers.get("X-API-Key")
    auth_header = request.headers.get("Authorization")
    fingerprint_header = request.headers.get("X-Device-Fingerprint")
    
    party_id = None
    role = None
    
    # 1. Check X-API-Key
    if api_key_header:
        res = resolve_party_by_api_key(api_key_header)
        if res:
            party_id = res["party_id"]
            role = res["role"]
            
    # 2. Check Bearer Token (JWT)
    if not party_id and auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            payload = decode_access_token(token)
            party_id = payload.get("sub")
            role = payload.get("role")
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid JWT access token: {e}"
            )
            
    # 3. Check X-Device-Fingerprint
    if not party_id and fingerprint_header:
        res = resolve_party_by_fingerprint(fingerprint_header)
        if res:
            party_id = res["party_id"]
            role = res["role"]
            
    # Fallback to legacy X-Party-ID header for backward compatibility
    if not party_id:
        party_id = request.headers.get("X-Party-ID")
        if party_id:
            conn = get_connection()
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT role FROM parties WHERE id = ?", (party_id,)).fetchone()
                if row:
                    role = row["role"]
            except Exception:
                pass
            finally:
                conn.close()

    # Fallback to local admin user if no auth headers are provided at all (for local/test mode backward compatibility)
    if not src.config.REQUIRE_AUTH and not auth_header and not api_key_header and not fingerprint_header and not request.headers.get("X-Party-ID"):
        party_id = "local_user"
        role = "admin"
                
    if not party_id or not role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Missing or invalid authentication token/header."
        )
        
    # Update last_seen in SQLite
    conn = get_connection()
    try:
        now = datetime.now(UTC).isoformat()
        conn.execute("UPDATE parties SET last_seen = ? WHERE id = ?", (now, party_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update last_seen for {party_id}: {e}")
    finally:
        conn.close()
        
    return {"party_id": party_id, "role": role}


def require_role(minimum_role: str):
    """Factory dependency to enforce minimum role access controls."""
    def dependency(current_party: Dict[str, Any] = Depends(get_current_party)):
        if not verify_role(current_party["role"], minimum_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Action requires role '{minimum_role}' or higher."
            )
        return current_party
    # We must construct a callable that FastAPI can resolve as a dependency
    return dependency

from fastapi import Depends

async def get_websocket_party(token: Optional[str] = None) -> Dict[str, Any]:
    """Helper to verify WebSocket connection JWT token or fallback to local user when auth is not required."""
    party_id = None
    role = None
    
    if token:
        try:
            payload = decode_access_token(token)
            party_id = payload.get("sub")
            role = payload.get("role")
        except Exception as e:
            logger.warning(f"WebSocket JWT decode failed: {e}")
            raise HTTPException(status_code=401, detail=f"Invalid JWT: {e}")
            
    if not party_id and not src.config.REQUIRE_AUTH:
        party_id = "local_user"
        role = "admin"
        
    if not party_id or not role:
        raise HTTPException(status_code=401, detail="Unauthorized WebSocket connection")
        
    return {"party_id": party_id, "role": role}


def process_sandbox_updates(response_text: str):
    """Spawns background task to auto-apply sandbox modifications from agent chat."""
    try:
        from src.sandbox_session import get_active_sandbox
        active_sb = get_active_sandbox()
        if active_sb:
            import threading
            def worker():
                try:
                    from src.persona import parse_proposed_changes
                    from src.sandbox_session import apply_changes_to_sandbox, run_sandbox_tests
                    proposed = parse_proposed_changes(response_text)
                    if proposed:
                        logger.info(f"Web UI: Extracted proposed modifications for {len(proposed)} file(s).")
                        apply_changes_to_sandbox(proposed)
                        logger.info("Web UI: Sandbox files updated. Executing unit tests...")
                        passed, logs = run_sandbox_tests()
                        status_str = "PASSED" if passed else "FAILED"
                        logger.info(f"Web UI: Sandbox unit tests: {status_str}")
                        
                        if passed:
                            log_episodic_memory(
                                "sandbox_automation",
                                f"Sandbox testing completed successfully for branch '{active_sb['active_sandbox_branch']}'. All tests passed.",
                                "user_visible"
                            )
                        else:
                            log_episodic_memory(
                                "sandbox_automation",
                                f"Sandbox testing FAILED for branch '{active_sb['active_sandbox_branch']}'. Errors/Logs:\n{logs}",
                                "user_visible"
                            )
                    else:
                        import re
                        has_path_mentions = bool(re.findall(
                            r"\b((?:src|tests)/[a-zA-Z0-9_/.-]+|[a-zA-Z0-9_/.-]+\.md|[a-zA-Z0-9_/.-]+\.json|requirements\.txt)\b",
                            response_text
                        ))
                        has_code_blocks = "```" in response_text
                        if has_path_mentions and has_code_blocks:
                            log_episodic_memory(
                                "sandbox_automation",
                                "[Warning] Found code blocks and file paths in response, but failed to auto-extract changes. "
                                "Ensure files are prefixed with 'Path: <relative_path>' or 'File: <relative_path>' immediately above their code blocks.",
                                "user_visible"
                            )
                except Exception as err:
                    logger.error(f"Error auto-applying to sandbox from Web UI: {err}", exc_info=True)
            
            threading.Thread(target=worker, daemon=True).start()
    except Exception as e:
        logger.error(f"Error starting sandbox updates thread: {e}", exc_info=True)
