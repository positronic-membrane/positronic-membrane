import os
import json
import logging
import uuid
import re
import sqlite3
import asyncio
from datetime import datetime, UTC
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request, Response, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from pydantic import BaseModel

from src.database import (
    get_connection,
    log_episodic_memory,
    get_recent_episodic_memories,
    get_constitution
)
from src.persona import (
    detect_metacognitive_intent,
    generate_persona_response,
    generate_metacognitive_narrative,
    generate_persona_response_autonomous,
    handle_web_slash_command
)
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

# Initialize FastAPI App
app = FastAPI(title="Project Janus API Layer", version="1.0.0")

# Setup CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=src.config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple IP-based Rate Limiter (Sliding Window)
from collections import defaultdict
import time

ip_request_history = defaultdict(list)

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    
    # Skip rate limiting for non-API routes (static files, index page, etc.)
    if request.url.path.startswith("/api/"):
        now = time.time()
        requests_limit = getattr(src.config, "RATE_LIMIT_REQUESTS", 60)
        window = getattr(src.config, "RATE_LIMIT_WINDOW", 60)
        
        # Filter request timestamps outside the window
        ip_request_history[client_ip] = [t for t in ip_request_history[client_ip] if now - t < window]
        
        if len(ip_request_history[client_ip]) >= requests_limit:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again later."}
            )
        ip_request_history[client_ip].append(now)
        
    return await call_next(request)

# Path to static directory
STATIC_DIR = Path(__file__).resolve().parent / "static"

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


def verify_role(party_role: str, minimum_role: str) -> bool:
    """Check if a party's role meets the minimum required role."""
    return ROLE_HIERARCHY.get(party_role, -1) >= ROLE_HIERARCHY.get(minimum_role, 0)


from functools import lru_cache

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
    return dependency


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


# --- REST API Endpoints ---

@app.post("/api/v1/auth/token")
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


@app.get("/api/v1/bootstrap/status")
def get_bootstrap_status():
    """No Auth: Returns setup alignment wizard completeness."""
    return bootstrap.check_web_ui_bootstrap()


@app.get("/api/history")
def get_chat_history(current_party = Depends(require_role('user'))):
    """Returns the last 50 user-visible episodic memories."""
    try:
        rows = get_recent_episodic_memories(limit=50, context_type="user_visible")
        history = []
        for speaker, msg, ts in reversed(rows):
            if speaker in ("user", "persona", "sandbox_automation", "system"):
                history.append({
                    "speaker": speaker,
                    "message": msg,
                    "timestamp": ts
                })
        return history
    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/deliberations")
def get_deliberations(current_party = Depends(require_role('user'))):
    """Returns the last 20 internal swarm deliberations."""
    try:
        conn = get_connection(read_only_constitution=True)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, timestamp, proposed_action, agent_debate_json, critic_decision, utility_score, justification 
            FROM internal_deliberations 
            ORDER BY id DESC 
            LIMIT 20;
        """)
        rows = cursor.fetchall()
        conn.close()

        deliberations = []
        for r in rows:
            debate_details = {}
            try:
                if r[3]:
                    debate_details = json.loads(r[3])
            except Exception as json_err:
                logger.warning(f"Failed to parse agent_debate_json for ID {r[0]}: {json_err}")
            
            deliberations.append({
                "id": r[0],
                "timestamp": r[1],
                "action": r[2],
                "debate": debate_details,
                "decision": r[4],
                "utility": r[5],
                "justification": r[6]
            })
        return deliberations
    except Exception as e:
        logger.error(f"Error fetching deliberations: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sandbox/status")
def get_sandbox_status(current_party = Depends(require_role('user'))):
    """Returns current active sandbox worktree and branch info."""
    try:
        from src.sandbox_session import get_active_sandbox, get_sandbox_modified_files
        active = get_active_sandbox()
        if active:
            modified = get_sandbox_modified_files()
            return {
                "active": True,
                "path": active["active_sandbox_path"],
                "branch": active["active_sandbox_branch"],
                "status": active["active_sandbox_status"],
                "modified": modified,
                "test_logs": active.get("active_sandbox_test_logs", "")
            }
        return {"active": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sandbox/diff")
def get_sandbox_diff_endpoint(current_party = Depends(require_role('user'))):
    """Returns difference between active sandbox worktree and main branch."""
    try:
        from src.sandbox_session import get_sandbox_diff
        diff = get_sandbox_diff()
        return {"diff": diff}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stage/status")
def get_stage_status(current_party = Depends(require_role('user'))):
    """Returns current database staged self-modifications."""
    try:
        from src.database import get_pending_modification
        pending = get_pending_modification()
        if pending:
            test_logs = ""
            log_path = Path(pending["pending_mod_dir"]) / "staging_test.log"
            if log_path.exists():
                try:
                    with open(log_path, "r", encoding="utf-8") as f:
                        test_logs = f.read()
                except Exception:
                    pass
            return {
                "active": True,
                "file_path": pending["pending_mod_file"],
                "dir": pending["pending_mod_dir"],
                "diff": pending["pending_mod_diff"],
                "status": pending["pending_mod_status"],
                "test_logs": test_logs
            }
        return {"active": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/constitution")
def get_constitution_endpoint(current_party = Depends(require_role('user'))):
    """Returns core constitution rules from SQLite."""
    try:
        rules = get_constitution()
        return [{"key": r[0], "text": r[1]} for r in rules]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/registry")
def get_registry_endpoint(current_party = Depends(require_role('user'))):
    """Returns current active agents and their overrides."""
    try:
        conn = get_connection(read_only_constitution=True)
        cursor = conn.cursor()
        cursor.execute("SELECT agent_id, agent_name, system_prompt, target_model, is_active FROM agent_registry;")
        rows = cursor.fetchall()
        conn.close()
        return [{
            "id": r[0],
            "name": r[1],
            "prompt": r[2],
            "model": r[3] or "",
            "active": bool(r[4])
        } for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/registry/rules")
def get_registry_rules(current_party = Depends(require_role('user'))):
    """Returns active agent constitutional auditing rules."""
    try:
        from src.database import get_all_agent_rules
        return get_all_agent_rules()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat")
def post_chat(data: ChatRequest, current_party = Depends(require_role('user'))):
    """Processes user chat request, logs episodic memory, and triggers response generation."""
    try:
        user_msg = data.message.strip()
        if not user_msg:
            raise HTTPException(status_code=400, detail="Message cannot be empty")

        party_id = current_party["party_id"]
        log_episodic_memory("user", user_msg, "user_visible", party_id=party_id)

        if user_msg.startswith("/"):
            from src.persona import handle_web_slash_command
            response = asyncio.run(handle_web_slash_command(user_msg))
        elif detect_metacognitive_intent(user_msg):
            response = generate_metacognitive_narrative(user_msg)
        else:
            response = generate_persona_response_autonomous(user_msg, party_id=party_id)

        log_episodic_memory("persona", response, "user_visible", party_id=party_id)
        process_sandbox_updates(response)
        
        return {"response": response}
    except Exception as e:
        logger.error(f"Error processing chat POST: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sandbox/action")
def post_sandbox_action(data: SandboxActionRequest, current_party = Depends(require_role('contributor'))):
    """Handles Git Sandbox initialization, test running, aborting, and shipping."""
    try:
        from src.sandbox_session import (
            create_sandbox_session, run_sandbox_tests, ship_sandbox_session, abort_sandbox_session
        )

        if data.action == "start":
            name = data.name or "web_sandbox"
            path, branch = create_sandbox_session(name)
            log_episodic_memory(
                "sandbox_automation",
                f"Sandbox session '{name}' initialized on branch '{branch}'. Sandbox path: '{path}'.",
                "user_visible"
            )
            return {"success": True, "branch": branch, "path": path}
        elif data.action == "test":
            passed, logs = run_sandbox_tests()
            return {"success": True, "passed": passed, "logs": logs}
        elif data.action == "ship":
            from src.sandbox_session import get_active_sandbox
            active = get_active_sandbox()
            copied = ship_sandbox_session()
            if active:
                msg = f"Sandbox session branch '{active['active_sandbox_branch']}' successfully shipped and applied to active workspace. Files modified: {', '.join(copied)}."
                log_episodic_memory("sandbox_automation", msg, "user_visible")
            return {"success": True, "copied": copied}
        elif data.action == "abort":
            from src.sandbox_session import get_active_sandbox
            active = get_active_sandbox()
            abort_sandbox_session()
            if active:
                msg = f"Sandbox session branch '{active['active_sandbox_branch']}' aborted and cleaned up."
                log_episodic_memory("sandbox_automation", msg, "user_visible")
            return {"success": True}
        else:
            raise HTTPException(status_code=400, detail=f"Invalid sandbox action: {data.action}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/stage/action")
def post_stage_action(data: StageActionRequest, current_party = Depends(require_role('contributor'))):
    """Applies, cancels, refines, or self-heals database staged self-modifications."""
    try:
        from src.database import get_pending_modification, clear_pending_modification
        from src.self_modification import apply_staged_multi
        import shutil

        pending = get_pending_modification()
        if not pending:
            raise HTTPException(status_code=400, detail="No active staging session.")

        if data.action == "apply":
            files = pending["pending_mod_file"].split(",")
            apply_staged_multi(pending["pending_mod_dir"], {f: True for f in files})
            for f in files:
                log_episodic_memory("system", f"User approved staged multi-file self-modification for '{f}'.", "user_visible")
            try:
                shutil.rmtree(pending["pending_mod_dir"])
            except Exception:
                pass
            clear_pending_modification()
            return {"success": True}

        elif data.action == "cancel":
            files = pending["pending_mod_file"].split(",")
            for f in files:
                log_episodic_memory("system", f"User rejected staged multi-file self-modification for '{f}'.", "user_visible")
            try:
                shutil.rmtree(pending["pending_mod_dir"])
            except Exception:
                pass
            clear_pending_modification()
            return {"success": True}

        elif data.action == "refine":
            if not data.file_path or not data.instructions:
                raise HTTPException(status_code=400, detail="Missing file_path or instructions for refinement.")

            from src.llm import query_agent
            from src.database import stage_modification_in_db
            from src.self_modification import stage_and_test_multi, generate_multi_diff

            staged_files = pending["pending_mod_file"].split(",")
            proposed_mods = {}
            for f in staged_files:
                path = Path(pending["pending_mod_dir"]) / f
                if path.is_file():
                    with open(path, "r", encoding="utf-8") as file_io:
                        proposed_mods[f] = file_io.read()

            from src.config import get_effective_workspace_root
            full_path = get_effective_workspace_root() / data.file_path
            current_content = ""
            if full_path.exists():
                with open(full_path, "r", encoding="utf-8") as file_io:
                    current_content = file_io.read()

            draft_prompt = f"""
            You are the Proposer. The user has requested a refinement for: {data.file_path}
            Instructions: {data.instructions}
            Current content: {current_content}
            Output the COMPLETE updated source code for {data.file_path}.
            CRITICAL RULES: Output ONLY raw code. Do NOT wrap in markdown code blocks.
            """
            proposed_code = query_agent("proposer", draft_prompt)
            if proposed_code.strip().startswith("```"):
                lines = proposed_code.strip().splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                proposed_code = "\n".join(lines) + "\n"

            proposed_mods[data.file_path] = proposed_code

            try:
                shutil.rmtree(pending["pending_mod_dir"])
            except Exception:
                pass

            passed, logs, new_temp_dir = stage_and_test_multi(proposed_mods)
            diff = generate_multi_diff(proposed_mods)

            stage_modification_in_db(",".join(proposed_mods.keys()), new_temp_dir, diff, "passed" if passed else "failed")
            try:
                with open(Path(new_temp_dir) / "staging_test.log", "w", encoding="utf-8") as f:
                    f.write(logs)
            except Exception:
                pass

            return {"success": True, "passed": passed, "diff": diff, "logs": logs}

        elif data.action == "heal":
            log_path = Path(pending["pending_mod_dir"]) / "staging_test.log"
            if not log_path.exists():
                raise HTTPException(status_code=400, detail="No staging test logs found to heal from.")
            with open(log_path, "r", encoding="utf-8") as f:
                logs = f.read()

            failing_tests = []
            for match in re.findall(r"(?:FAILED|ERROR)\s+(tests/test_[a-zA-Z0-9_-]+\.py)|(tests/test_[a-zA-Z0-9_-]+\.py)::\S+\s+(?:FAILED|ERROR)", logs):
                failing_tests.append(match[0] or match[1])
            failing_tests = sorted(list(set(failing_tests)))

            if not failing_tests:
                raise HTTPException(status_code=400, detail="No failing tests detected in log file.")

            staged_files = pending["pending_mod_file"].split(",")
            proposed_mods = {}
            for f in staged_files:
                path = Path(pending["pending_mod_dir"]) / f
                if path.is_file():
                    with open(path, "r", encoding="utf-8") as file_io:
                        proposed_mods[f] = file_io.read()

            from src.llm import query_agent
            from src.database import stage_modification_in_db
            from src.self_modification import stage_and_test_multi, generate_multi_diff

            from src.config import get_effective_workspace_root
            for test_file in failing_tests:
                full_path = get_effective_workspace_root() / test_file
                current_content = ""
                if full_path.exists():
                    with open(full_path, "r", encoding="utf-8") as file_io:
                        current_content = file_io.read()
                from src.self_modification import summarize_pytest_logs
                draft_prompt = f"""
                You are the Proposer. A pre-existing test file has failed during staging.
                Fix (self-heal) this test file to pass unit tests.
                FAILING TEST FILE: {test_file}
                TEST RUN LOGS: {summarize_pytest_logs(logs)}
                CURRENT CONTENT: {current_content}
                Output the COMPLETE updated source code for {test_file}.
                CRITICAL RULES: Output ONLY raw code. Do NOT wrap in markdown code blocks.
                """
                proposed_code = query_agent("proposer", draft_prompt)
                if proposed_code.strip().startswith("```"):
                    lines = proposed_code.strip().splitlines()
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].strip() == "```":
                        lines = lines[:-1]
                    proposed_code = "\n".join(lines) + "\n"
                proposed_mods[test_file] = proposed_code

            try:
                shutil.rmtree(pending["pending_mod_dir"])
            except Exception:
                pass

            passed, new_logs, new_temp_dir = stage_and_test_multi(proposed_mods)
            diff = generate_multi_diff(proposed_mods)

            stage_modification_in_db(",".join(proposed_mods.keys()), new_temp_dir, diff, "passed" if passed else "failed")
            try:
                with open(Path(new_temp_dir) / "staging_test.log", "w", encoding="utf-8") as f:
                    f.write(new_logs)
            except Exception:
                pass

            return {"success": True, "passed": passed, "diff": diff, "logs": new_logs}
        else:
            raise HTTPException(status_code=400, detail=f"Invalid stage action: {data.action}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/constitution/amend")
def amend_constitution(data: ConstitutionAmendRequest, current_party = Depends(require_role('contributor'))):
    """Amends a constitutional rule in SQLite (Requires contributor role)."""
    key = data.key.strip()
    text = data.text.strip()
    if not key or not text:
        raise HTTPException(status_code=400, detail="Key and text cannot be empty.")
    try:
        from src.database import add_constitution_rule
        add_constitution_rule(key, text)
        log_episodic_memory("system", f"User sealed constitutional rule via Web UI: '{key}' = '{text}'", "user_visible")
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/constitution/delete")
def delete_constitution(data: ConstitutionDeleteRequest, current_party = Depends(require_role('contributor'))):
    """Deletes a constitutional rule from SQLite (Requires contributor role)."""
    key = data.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="Key cannot be empty.")
    try:
        from src.database import delete_constitution_rule
        delete_constitution_rule(key)
        log_episodic_memory("system", f"User deleted constitutional rule via Web UI: '{key}'", "user_visible")
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/registry/update")
def update_registry(data: RegistryUpdateRequest, current_party = Depends(require_role('contributor'))):
    """Updates target model override for a given agent registry record."""
    agent_id = data.agent_id.strip()
    model = data.model.strip() if data.model else None
    if not agent_id:
        raise HTTPException(status_code=400, detail="Agent ID cannot be empty.")
    try:
        conn = get_connection(read_only_constitution=True)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE agent_registry 
            SET target_model = ?, updated_at = CURRENT_TIMESTAMP 
            WHERE agent_id = ?;
        """, (model, agent_id))
        conn.commit()
        conn.close()
        log_episodic_memory("system", f"User updated agent model override for '{agent_id}' to '{model or 'DEFAULT'}'.", "user_visible")
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/registry/rules/update")
def update_registry_rules(data: RegistryRulesUpdateRequest, current_party = Depends(require_role('contributor'))):
    """Adds, toggles, or deletes agent-specific constitutional auditing rules."""
    from src.database import add_agent_rule, toggle_agent_rule, delete_agent_rule
    try:
        if data.action == "add":
            if not data.agent_id or not data.rule_key or not data.rule_text:
                raise HTTPException(status_code=400, detail="Missing required fields: agent_id, rule_key, rule_text")
            add_agent_rule(data.agent_id.strip(), data.rule_key.strip(), data.rule_text.strip())
            log_episodic_memory("system", f"User added agent rule via Web UI: [{data.agent_id.strip()}] '{data.rule_key.strip()}' = '{data.rule_text.strip()}'", "user_visible")
        elif data.action == "toggle":
            if not data.rule_key:
                raise HTTPException(status_code=400, detail="Missing rule_key")
            toggle_agent_rule(data.rule_key.strip(), data.is_active)
            status_str = "enabled" if data.is_active else "disabled"
            log_episodic_memory("system", f"User {status_str} agent rule via Web UI: '{data.rule_key.strip()}'", "user_visible")
        elif data.action == "delete":
            if not data.rule_key:
                raise HTTPException(status_code=400, detail="Missing rule_key")
            delete_agent_rule(data.rule_key.strip())
            log_episodic_memory("system", f"User deleted agent rule via Web UI: '{data.rule_key.strip()}'", "user_visible")
        else:
            raise HTTPException(status_code=400, detail=f"Invalid rule update action: {data.action}")
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Multi-Party GET/POST/PUT Routes (/api/v1/) ---

@app.get("/api/v1/party/{target_party_id}")
def get_v1_party(target_party_id: str, current_party = Depends(require_role('user'))):
    """Returns information for a registered party (Requires user role)."""
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            'SELECT id, name, role, created_at, last_seen, public_key, metadata FROM parties WHERE id = ?',
            (target_party_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Party not found")
        res_dict = dict(row)
        try:
            res_dict['metadata'] = json.loads(res_dict['metadata'])
        except Exception:
            pass
        return res_dict
    finally:
        conn.close()


@app.get("/api/v1/memory/{key}")
def get_v1_memory(key: str, namespace: str = "global", current_party = Depends(require_role('user'))):
    """Fetches custom memory slot for a party (Requires user role)."""
    value = memory_orch.get_memory(current_party["party_id"], key, namespace)
    if value is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"key": key, "value": value, "namespace": namespace}


@app.get("/api/v1/feedback/aggregate")
def get_v1_feedback_aggregate(feature: Optional[str] = None, current_party = Depends(require_role('user'))):
    """Fetches system-wide aggregate feedback stats (Requires user role)."""
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        if feature:
            rows = conn.execute(
                'SELECT * FROM feedback_aggregates WHERE feature = ?', (feature,)
            ).fetchall()
        else:
            rows = conn.execute('SELECT * FROM feedback_aggregates').fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/v1/party/register", status_code=201)
def register_party(data: PartyRegisterRequest, current_party = Depends(require_role('admin'))):
    """Registers a new party with associated UUID and public key (Requires admin role)."""
    if data.role not in ('user', 'contributor', 'admin', 'observer'):
        raise HTTPException(status_code=400, detail=f"Invalid role: {data.role}")
    new_party_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    metadata_str = json.dumps(data.metadata)
    conn = get_connection()
    try:
        conn.execute(
            'INSERT INTO parties (id, name, role, created_at, last_seen, public_key, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (new_party_id, data.name, data.role, now, now, data.public_key, metadata_str)
        )
        conn.commit()
        resolve_party_by_api_key.cache_clear()
        resolve_party_by_fingerprint.cache_clear()
        return {
            "party_id": new_party_id,
            "name": data.name,
            "role": data.role,
            "last_seen": now,
            "metadata": data.metadata
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create party: {e}")
    finally:
        conn.close()


@app.post("/api/v1/memory", status_code=201)
def post_v1_memory(data: MemorySetRequest, current_party = Depends(require_role('user'))):
    """Sets a customized key-value memory for a party (Requires user role)."""
    mem_id = memory_orch.set_memory(current_party["party_id"], data.key, data.value, data.namespace)
    return {"memory_id": mem_id}


@app.post("/api/v1/modification", status_code=201)
def post_v1_modification(data: ModificationCreateRequest, current_party = Depends(require_role('contributor'))):
    """Logs self-modification proposals to DB awaiting governance audit (Requires contributor role)."""
    mod_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    status_val = 'pending_self_review' if data.change_type in ('self_source', 'self_config') else 'pending'
    conn = get_connection()
    try:
        conn.execute(
            'INSERT INTO modifications (id, initiated_by, feature, change_type, change_resource, diff, status, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (mod_id, current_party["party_id"], data.feature, data.change_type, data.change_resource, data.diff, status_val, now)
        )
        conn.commit()
        return {"modification_id": mod_id, "status": status_val}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create modification: {e}")
    finally:
        conn.close()


@app.put("/api/v1/party/{target_party_id}/role")
def update_party_role(target_party_id: str, data: PartyRoleUpdateRequest, current_party = Depends(require_role('admin'))):
    """Modifies a party's system authorization hierarchy (Requires admin role)."""
    if data.role not in ('user', 'contributor', 'admin', 'observer'):
        raise HTTPException(status_code=400, detail=f"Invalid role: {data.role}")
    conn = get_connection()
    try:
        conn.execute('UPDATE parties SET role = ? WHERE id = ?', (data.role, target_party_id))
        conn.commit()
        resolve_party_by_api_key.cache_clear()
        resolve_party_by_fingerprint.cache_clear()
        return {"message": f"Party {target_party_id} role updated to {data.role}"}
    finally:
        conn.close()


@app.put("/api/v1/modification/{mod_id}/approve")
def approve_modification(mod_id: str, current_party = Depends(require_role('admin'))):
    """Approves a staged modification (Requires admin role)."""
    now = datetime.now(UTC).isoformat()
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        mod = conn.execute(
            'SELECT status, change_type FROM modifications WHERE id = ?', (mod_id,)
        ).fetchone()
        if not mod:
            raise HTTPException(status_code=404, detail="Modification not found")
        if mod['change_type'] in ('self_source', 'self_config') and mod['status'] == 'pending_self_review':
            raise HTTPException(
                status_code=400,
                detail="Self-modification requires Critic deliberation. Please complete autonomous auditing before approval."
            )
        if mod['status'] not in ('pending', 'pending_self_review'):
            raise HTTPException(status_code=400, detail=f"Cannot approve modification in status: {mod['status']}")
            
        conn.execute(
            'UPDATE modifications SET status = ?, approved_by = ?, approved_at = ? WHERE id = ?',
            ('approved', current_party["party_id"], now, mod_id)
        )
        conn.commit()
        return {"modification_id": mod_id, "status": "approved"}
    finally:
        conn.close()


@app.put("/api/v1/modification/{mod_id}/deploy")
def deploy_modification(mod_id: str, current_party = Depends(require_role('admin'))):
    """Deploys approved modifications to live configuration (Requires admin role)."""
    now = datetime.now(UTC).isoformat()
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        mod = conn.execute(
            'SELECT status FROM modifications WHERE id = ?', (mod_id,)
        ).fetchone()
        if not mod:
            raise HTTPException(status_code=404, detail="Modification not found")
        if mod['status'] != 'approved':
            raise HTTPException(status_code=400, detail=f"Cannot deploy modification in status: {mod['status']}")
            
        conn.execute(
            'UPDATE modifications SET status = ?, deployed_at = ? WHERE id = ?',
            ('deployed', now, mod_id)
        )
        conn.commit()
        return {"modification_id": mod_id, "status": "deployed"}
    finally:
        conn.close()


@app.put("/api/v1/modification/{mod_id}/rollback")
def rollback_modification(mod_id: str, current_party = Depends(require_role('admin'))):
    """Rolls back deployed modifications to historical revision (Requires admin role)."""
    now = datetime.now(UTC).isoformat()
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        mod = conn.execute(
            'SELECT status, diff, change_resource FROM modifications WHERE id = ?', (mod_id,)
        ).fetchone()
        if not mod:
            raise HTTPException(status_code=404, detail="Modification not found")
        if mod['status'] != 'deployed':
            raise HTTPException(status_code=400, detail=f"Cannot rollback modification in status: {mod['status']}")
            
        rollback_id = str(uuid.uuid4())
        conn.execute(
            'INSERT INTO modifications (id, initiated_by, feature, change_type, change_resource, diff, status, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (rollback_id, current_party["party_id"], 'rollback', 'rollback', mod['change_resource'], mod['diff'], 'rolled_back', now)
        )
        conn.execute(
            'UPDATE modifications SET status = ?, rolled_back_at = ? WHERE id = ?',
            ('rolled_back', now, mod_id)
        )
        conn.commit()
        return {"modification_id": mod_id, "status": "rolled_back", "rollback_id": rollback_id}
    finally:
        conn.close()


# --- WebSockets Event Streaming ---

@app.websocket("/ws/deliberations")
async def websocket_deliberations(websocket: WebSocket, token: Optional[str] = Query(None)):
    """WebSocket: Streams background swarm reflection deliberations in real-time."""
    try:
        current_party = await get_websocket_party(token)
        if not verify_role(current_party["role"], "user"):
            raise HTTPException(status_code=403, detail="Forbidden")
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    await websocket.accept()
        
    last_id = 0
    
    # Initialize last_id to max database id
    conn = get_connection()
    try:
        row = conn.execute("SELECT MAX(id) FROM internal_deliberations").fetchone()
        if row and row[0] is not None:
            last_id = max(0, row[0] - 1)
    finally:
        conn.close()
        
    try:
        while True:
            # Poll DB for new deliberations
            conn = get_connection()
            try:
                rows = conn.execute(
                    "SELECT id, timestamp, proposed_action, agent_debate_json, critic_decision, utility_score, justification "
                    "FROM internal_deliberations WHERE id > ? ORDER BY id ASC", (last_id,)
                ).fetchall()
                for r in rows:
                    last_id = r[0]
                    debate_details = {}
                    try:
                        if r[3]:
                            debate_details = json.loads(r[3])
                    except Exception:
                        pass
                    msg = {
                        "id": r[0],
                        "timestamp": r[1],
                        "action": r[2],
                        "debate": debate_details,
                        "decision": r[4],
                        "utility": r[5],
                        "justification": r[6]
                    }
                    await websocket.send_json(msg)
            finally:
                conn.close()
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Error in deliberations WebSocket: {e}")


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket, token: Optional[str] = Query(None)):
    """WebSocket: Bidirectional live agent chat interaction stream."""
    try:
        current_party = await get_websocket_party(token)
        if not verify_role(current_party["role"], "user"):
            raise HTTPException(status_code=403, detail="Forbidden")
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    await websocket.accept()
        
    try:
        while True:
            data = await websocket.receive_json()
            user_msg = data.get("message", "").strip()
            if not user_msg:
                continue
                
            party_id = current_party["party_id"]
            log_episodic_memory("user", user_msg, "user_visible", party_id=party_id)
            
            # Send 'thinking' state back to client
            await websocket.send_json({"event": "thinking"})
            
            # Process response
            if user_msg.startswith("/"):
                response = await handle_web_slash_command(user_msg)
            elif detect_metacognitive_intent(user_msg):
                response = generate_metacognitive_narrative(user_msg)
            else:
                response = generate_persona_response_autonomous(user_msg, party_id=party_id)
                
            log_episodic_memory("persona", response, "user_visible", party_id=party_id)
            process_sandbox_updates(response)
            
            # Stream response back
            await websocket.send_json({"event": "response", "message": response})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Error in chat WebSocket: {e}")


# --- Static Files / Single-Page-App Fallback ---

@app.get("/")
def serve_index():
    """Serves the front-end chat SPA interface."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse("<h1>Project Janus Web Interface</h1><p>Static files directory not found or index.html missing.</p>")


@app.get("/{path:path}")
def serve_static(path: str):
    """Fallback static router for assets (scripts, styling, pictures)."""
    file_path = STATIC_DIR / path
    if file_path.exists() and file_path.is_file():
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="File not found")


# --- Server Startup ---

def run_server(port=5005):
    """Starts the Uvicorn ASGI server. Blocks until process is interrupted."""
    import uvicorn
    os.makedirs(STATIC_DIR, exist_ok=True)
    logger.info(f"Starting Project Janus FastAPI Web Server on port {port} via Uvicorn...")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
