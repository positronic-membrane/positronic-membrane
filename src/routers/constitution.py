import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from src.database import get_constitution, log_episodic_memory
from src.routers.dependencies import (
    ConstitutionAmendRequest,
    ConstitutionDeleteRequest,
    PartyRegisterRequest,
    PartyRoleUpdateRequest,
    RegistryRulesUpdateRequest,
    RegistryUpdateRequest,
    get_connection,
    require_role,
    resolve_party_by_api_key,
    resolve_party_by_fingerprint,
)

logger = logging.getLogger("JanusWebServer")
router = APIRouter()

@router.get("/api/constitution")
def get_constitution_endpoint(current_party = Depends(require_role('user'))):
    """Returns core constitution rules from SQLite."""
    try:
        rules = get_constitution()
        return [{"key": r[0], "text": r[1]} for r in rules]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/registry")
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
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/registry/rules")
def get_registry_rules(current_party = Depends(require_role('user'))):
    """Returns active agent constitutional auditing rules."""
    try:
        from src.database import get_all_agent_rules
        return get_all_agent_rules()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/constitution/amend")
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
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/constitution/delete")
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
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/registry/update")
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
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/registry/rules/update")
def update_registry_rules(data: RegistryRulesUpdateRequest, current_party = Depends(require_role('contributor'))):
    """Adds, toggles, or deletes agent-specific constitutional auditing rules."""
    from src.database import add_agent_rule, delete_agent_rule, toggle_agent_rule
    try:
        if data.action == "add":
            if not data.agent_id or not data.rule_key or not data.rule_text:
                raise HTTPException(status_code=400, detail="Missing required fields: agent_id, rule_key, rule_text")
            add_agent_rule(data.agent_id.strip(), data.rule_key.strip(), data.rule_text.strip())
            log_episodic_memory(
                "system",
                f"User added agent rule via Web UI: [{data.agent_id.strip()}] '{data.rule_key.strip()}' = "
                f"'{data.rule_text.strip()}'",
                "user_visible"
            )
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
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/v1/party/{target_party_id}")
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


@router.get("/api/v1/feedback/aggregate")
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


@router.post("/api/v1/party/register", status_code=201)
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
        raise HTTPException(status_code=400, detail=f"Failed to create party: {e}") from e
    finally:
        conn.close()


@router.put("/api/v1/party/{target_party_id}/role")
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
