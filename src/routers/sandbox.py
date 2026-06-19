import uuid
import re
import sqlite3
import shutil
from datetime import datetime, UTC
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status

from src.routers.dependencies import (
    SandboxActionRequest,
    StageActionRequest,
    ModificationCreateRequest,
    require_role,
    get_connection
)
from src.database import log_episodic_memory

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
                "modified": modified,
                "test_logs": active.get("active_sandbox_test_logs", "")
            }
        return {"active": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/sandbox/diff")
def get_sandbox_diff_endpoint(current_party = Depends(require_role('user'))):
    """Returns difference between active sandbox worktree and main branch."""
    try:
        from src.sandbox_session import get_sandbox_diff
        diff = get_sandbox_diff()
        return {"diff": diff}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/stage/status")
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


@router.post("/api/sandbox/action")
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


@router.post("/api/stage/action")
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
        raise HTTPException(status_code=400, detail=f"Failed to create modification: {e}")
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
