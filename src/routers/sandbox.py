import sqlite3
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from src.database import log_episodic_memory
from src.middleware import SelfModificationFrozenError
from src.routers.dependencies import ModificationCreateRequest, SandboxActionRequest, get_connection, require_role

router = APIRouter()

@router.get("/api/sandbox/status")
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
                "purpose": active.get("active_sandbox_purpose", "evolution"),
                "app_name": active.get("active_sandbox_app_name", ""),
                "modified": modified,
                "test_logs": active.get("active_sandbox_test_logs", "")
            }
        return {"active": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/sandbox/diff")
def get_sandbox_diff_endpoint(current_party = Depends(require_role('user'))):
    """Returns difference between active sandbox worktree and main branch."""
    try:
        from src.sandbox_session import get_sandbox_diff
        diff = get_sandbox_diff()
        return {"diff": diff}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/stage/status")
def get_stage_status(current_party = Depends(require_role('user'))):
    """Removed — direct source modification is disabled (V3-T3)."""
    raise HTTPException(status_code=410, detail="Direct source modification is disabled. Use the skill staging harness or a Project Sandbox.")


@router.post("/api/sandbox/action")
def post_sandbox_action(data: SandboxActionRequest, current_party = Depends(require_role('contributor'))):
    """Handles Git Sandbox initialization, test running, aborting, shipping, and promotion."""
    try:
        from src.sandbox_session import (
            abort_sandbox_session,
            create_sandbox_session,
            delete_project_sandbox,
            promote_evolution_sandbox,
            run_sandbox_tests,
            ship_sandbox_session,
        )

        if data.action == "start":
            purpose = data.purpose or "evolution"
            name = data.name or ("web_project" if purpose == "project" else "web_sandbox")
            path, branch = create_sandbox_session(name, purpose=purpose, app_name=data.app_name)
            log_episodic_memory(
                "sandbox_automation",
                f"Sandbox session '{name}' (purpose={purpose}) initialized"
                + (f" on branch '{branch}'" if branch else "")
                + f". Sandbox path: '{path}'.",
                "user_visible"
            )
            return {"success": True, "branch": branch, "path": path, "purpose": purpose}
        elif data.action == "test":
            passed, logs = run_sandbox_tests()
            return {"success": True, "passed": passed, "logs": logs}
        elif data.action == "ship":
            from src.sandbox_session import get_active_sandbox
            active = get_active_sandbox()
            copied = ship_sandbox_session()
            if active:
                msg = (
                    f"Sandbox session branch '{active['active_sandbox_branch']}' successfully shipped and "
                    f"applied to active workspace. Files modified: {', '.join(copied)}."
                )
                log_episodic_memory("sandbox_automation", msg, "user_visible")
            return {"success": True, "copied": copied}
        elif data.action == "promote":
            from src.sandbox_session import get_active_sandbox
            active = get_active_sandbox()
            if not active or active.get("active_sandbox_purpose") != "evolution":
                raise HTTPException(status_code=400, detail="No active evolution-purpose sandbox session to promote.")
            result = promote_evolution_sandbox()
            msg = (
                f"Sandbox session branch '{active['active_sandbox_branch']}' promoted. "
                f"Files: {len(result['copied_files'])}, migrations queued: {result['queued_migrations']}, "
                f"memories ported: {result['ported_memories']}."
            )
            log_episodic_memory("sandbox_automation", msg, "user_visible")
            return {"success": True, **result}
        elif data.action == "abort":
            from src.sandbox_session import get_active_sandbox
            active = get_active_sandbox()
            abort_sandbox_session()
            if active:
                msg = f"Sandbox session branch '{active['active_sandbox_branch']}' aborted and cleaned up."
                log_episodic_memory("sandbox_automation", msg, "user_visible")
            return {"success": True}
        elif data.action == "delete_project":
            if not data.app_name:
                raise HTTPException(status_code=400, detail="app_name is required for delete_project.")
            deleted = delete_project_sandbox(data.app_name)
            if deleted:
                log_episodic_memory(
                    "sandbox_automation",
                    f"Project sandbox '{data.app_name}' permanently deleted.",
                    "user_visible"
                )
            return {"success": deleted}
        else:
            raise HTTPException(status_code=400, detail=f"Invalid sandbox action: {data.action}")
    except HTTPException:
        raise
    except SelfModificationFrozenError as e:
        raise HTTPException(status_code=410, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/stage/action")
def post_stage_action(current_party = Depends(require_role('contributor'))):
    """Removed — direct source modification is disabled (V3-T3)."""
    raise HTTPException(status_code=410, detail="Direct source modification is disabled. Use the skill staging harness or a Project Sandbox.")


@router.post("/api/v1/modification", status_code=201)
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
        raise HTTPException(status_code=400, detail=f"Failed to create modification: {e}") from e
    finally:
        conn.close()


@router.put("/api/v1/modification/{mod_id}/approve")
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


@router.put("/api/v1/modification/{mod_id}/deploy")
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


@router.put("/api/v1/modification/{mod_id}/rollback")
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
