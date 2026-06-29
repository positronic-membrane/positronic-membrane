import sys
import os
import logging
import json
import traceback
import re
import subprocess
from typing import Optional, Any
from src.database import get_connection
from src.sandbox_session import (
    create_sandbox_session,
    run_sandbox_tests,
    apply_changes_to_sandbox,
    ship_sandbox_session,
    abort_sandbox_session,
    get_sandbox_diff
)
from openai import OpenAI

# Decoupled SDK backend library dependencies
from src.explorer import search_web, fetch_webpage
from src.notifications import send_webhook_notification
from src.codebase import query_codebase_context, index_codebase
from src.sandbox import execute_code_safely
from src.self_modification import apply_search_replace_blocks
from src.database import (
    get_recent_episodic_memories,
    log_episodic_memory,
    register_helper_agent,
    log_deliberation,
    get_constitution,
    send_swarm_message,
    get_pending_swarm_messages,
    mark_swarm_message_processed,
    get_curiosity_vector,
    update_curiosity_vector
)
from src.memory import get_active_curiosity_topics, update_curiosity_topics, consolidate_memories
from src.middleware import validate_action
from src.daemon import parse_action, parse_critic_response, trigger_swarm_reflection
from src.llm import query_agent

logger = logging.getLogger("JanusSkills")

class SafeExplorer:
    """Safe search and URL fetching wrapper for dynamic skills."""
    def search(self, query: str) -> list:
        return search_web(query)

    def fetch(self, url: str) -> str:
        return fetch_webpage(url)

class SafeCodebase:
    """Safe codebase query and indexing wrapper for dynamic skills."""
    def query(self, term: str) -> str:
        return query_codebase_context(term)

    def scan(self) -> str:
        index_codebase()
        return "Codebase successfully scanned and indexed."

    def apply_search_replace(self, original_content: str, search_replace_block: str) -> str:
        return apply_search_replace_blocks(original_content, search_replace_block)

class SafeSandbox:
    """Safe execution sandbox wrapper for dynamic skills."""
    def execute(self, code: str) -> str:
        return execute_code_safely(code)

class SafeDB:
    """Safe database wrapper exposing SQL query checks to dynamic skills."""
    def query(self, sql: str, params: tuple = ()):
        from src.middleware import check_sql_safety
        check_sql_safety(sql)
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            if sql.strip().lower().startswith("select"):
                return [dict(row) for row in cursor.fetchall()] if conn.row_factory else cursor.fetchall()
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

class SafeMemory:
    """Safe semantic memory query wrapper for dynamic skills."""
    def __init__(self, party_id: Optional[str]):
        self.party_id = party_id

    def query(self, text: str, limit: int = 5, collection_name: str = "janus_long_term"):
        from src.memory import query_memories
        return query_memories(text, limit=limit, collection_name=collection_name)

    def add(self, content: str, metadata: dict, memory_id: str, collection_name: str = "janus_long_term"):
        from src.memory import add_memory
        # Inject party isolation scope tag to metadata if party_id is defined
        meta = metadata.copy() if metadata else {}
        if self.party_id:
            meta["party_id"] = self.party_id
        return add_memory(content, meta, memory_id, collection_name)

    def get_recent_episodic_memories(self, limit: int = 5, context_type: str = None) -> list:
        return get_recent_episodic_memories(limit=limit, context_type=context_type)

    def log_episodic_memory(self, speaker: str, message_content: str, context_type: str = "background_thought"):
        log_episodic_memory(speaker, message_content, context_type)

    def get_active_curiosity_topics(self, limit: int = 5) -> list:
        return get_active_curiosity_topics(limit=limit)

    def update_curiosity_topics(self, topics: list):
        update_curiosity_topics(topics)

    def consolidate(self, batch_size: int = 5):
        consolidate_memories(batch_size=batch_size)

class SafeFS:
    """Boundary-restricted filesystem wrapper for dynamic skills."""
    def __init__(self):
        from src.config import get_effective_workspace_root
        from pathlib import Path
        self.root = Path(get_effective_workspace_root()).resolve()

    def _resolve_path(self, path: str) -> Any:
        from pathlib import Path
        full_path = Path(self.root / path).resolve()
        if not str(full_path).startswith(str(self.root)):
            raise PermissionError(f"Access Denied: Path '{path}' lies outside the active workspace directory.")
        return full_path

    def read(self, path: str) -> str:
        full = self._resolve_path(path)
        with open(full, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    def write(self, path: str, content: str):
        full = self._resolve_path(path)
        os.makedirs(full.parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)

    def exists(self, path: str) -> bool:
        try:
            full = self._resolve_path(path)
            return full.exists()
        except PermissionError:
            return False

class SafeDrives:
    """Safe drive counter operations for dynamic skills."""
    def _validate_key(self, key: str) -> str:
        if key not in ("boredom",):
            raise ValueError(f"Drive state '{key}' is not tracked.")
        return f"{key}_counter"

    def get(self, key: str) -> int:
        col = self._validate_key(key)
        conn = get_connection(read_only_constitution=True)
        try:
            row = conn.execute(f"SELECT {col} FROM drive_state LIMIT 1;").fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def set(self, key: str, val: int):
        col = self._validate_key(key)
        conn = get_connection(read_only_constitution=True)
        try:
            conn.execute(f"UPDATE drive_state SET {col} = ?, updated_at = CURRENT_TIMESTAMP;", (val,))
            conn.commit()
        finally:
            conn.close()

    def increment(self, key: str, val: int = 1) -> int:
        col = self._validate_key(key)
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE drive_state SET {col} = {col} + ?, updated_at = CURRENT_TIMESTAMP;", (val,))
            conn.commit()
            row = cursor.execute(f"SELECT {col} FROM drive_state LIMIT 1;").fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def get_curiosity_vector(self) -> list:
        return get_curiosity_vector()

    def update_curiosity_vector(self, vector: list):
        update_curiosity_vector(vector)

class SafeSwarm:
    """Safe swarm trigger operations for dynamic skills."""
    def trigger_reflection(self):
        trigger_swarm_reflection()

    def query_agent(self, agent_id: str, prompt: str, system_override: str = None) -> str:
        return query_agent(agent_id, prompt, system_override=system_override)

    def register_agent(self, agent_id: str, name: str, prompt: str, model: str = None):
        register_helper_agent(agent_id, name, prompt, model)

    def log_deliberation(self, proposed_action: str, debate_json: dict, critic_decision: int, utility_score: float, justification: str):
        log_deliberation(proposed_action, debate_json, critic_decision, utility_score, justification)

    def get_constitution(self) -> list:
        return get_constitution()

    def validate_action(self, action: str) -> bool:
        return validate_action(action)

    def parse_action(self, action: str):
        return parse_action(action)

    def parse_critic_response(self, response: str):
        return parse_critic_response(response)

    def execute_skill(self, skill_id: str, arguments: dict, party_id: str = None) -> dict:
        return DynamicSkillExecutor.execute(skill_id, arguments, party_id)

    def send_message(self, sender_id: str, recipient_id: str, message_type: str, content: str):
        send_swarm_message(sender_id, recipient_id, message_type, content)

    def get_pending_messages(self, recipient_id: str) -> list:
        return get_pending_swarm_messages(recipient_id)

    def mark_message_processed(self, msg_id: int):
        mark_swarm_message_processed(msg_id)

class SafeSelfModel:
    """Safe self-model traits wrapper for dynamic skills."""
    def get_traits(self) -> dict:
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT trait_name, value, confidence, is_pinned FROM self_model;")
            rows = cursor.fetchall()
            traits = {}
            for row in rows:
                try:
                    name = row['trait_name']
                    val = row['value']
                    conf = row['confidence']
                    pinned = row['is_pinned']
                except (TypeError, IndexError, KeyError):
                    name = row[0]
                    val = row[1]
                    conf = row[2]
                    pinned = row[3]
                traits[name] = {
                    "value": float(val),
                    "confidence": float(conf),
                    "is_pinned": int(pinned)
                }
            return traits
        finally:
            conn.close()

    def update_trait(self, name: str, val: float, conf: float, reason: str) -> bool:
        if not (0.0 <= val <= 1.0) or not (0.0 <= conf <= 1.0):
            raise ValueError("Value and confidence must be between 0.0 and 1.0.")
        
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value, confidence, is_pinned FROM self_model WHERE trait_name = ?;", (name,))
            row = cursor.fetchone()
            if not row:
                return False
            
            try:
                pinned = row['is_pinned']
                old_val = row['value']
                old_conf = row['confidence']
            except (TypeError, IndexError, KeyError):
                old_val = row[0]
                old_conf = row[1]
                pinned = row[2]
                
            if pinned:
                return False
            
            cursor.execute(
                "UPDATE self_model SET value = ?, confidence = ?, updated_at = CURRENT_TIMESTAMP WHERE trait_name = ?;",
                (val, conf, name)
            )
            cursor.execute(
                "INSERT INTO self_model_history (trait_name, old_value, new_value, old_confidence, new_confidence, reason) "
                "VALUES (?, ?, ?, ?, ?, ?);",
                (name, float(old_val), val, float(old_conf), conf, reason)
            )
            conn.commit()
            return True
        finally:
            conn.close()

class SafeGoals:
    """Safe goal management wrapper for dynamic skills."""
    def get_goals(self, status: Optional[str] = None, type: Optional[str] = None) -> list:
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            query = "SELECT id, type, status, description, progress_metric, parent_goal_id, created_at, updated_at FROM goals"
            conditions = []
            params = []
            if status:
                conditions.append("status = ?")
                params.append(status)
            if type:
                conditions.append("type = ?")
                params.append(type)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            
            goals = []
            for row in rows:
                try:
                    gid = row['id']
                    gtype = row['type']
                    gstatus = row['status']
                    gdesc = row['description']
                    gprog = row['progress_metric']
                    gparent = row['parent_goal_id']
                    gcreated = row['created_at']
                    gupdated = row['updated_at']
                except (TypeError, IndexError, KeyError):
                    gid, gtype, gstatus, gdesc, gprog, gparent, gcreated, gupdated = row
                
                # Fetch checkpoints
                cursor2 = conn.cursor()
                cursor2.execute("SELECT id, checkpoint_description, achieved, achieved_at FROM goal_checkpoints WHERE goal_id = ?;", (gid,))
                cp_rows = cursor2.fetchall()
                checkpoints = []
                for cp in cp_rows:
                    try:
                        cpid = cp['id']
                        cpdesc = cp['checkpoint_description']
                        cpach = cp['achieved']
                        cpachat = cp['achieved_at']
                    except (TypeError, IndexError, KeyError):
                        cpid, cpdesc, cpach, cpachat = cp
                    checkpoints.append({
                        "id": cpid,
                        "description": cpdesc,
                        "achieved": bool(cpach),
                        "achieved_at": cpachat
                    })
                
                goals.append({
                    "id": gid,
                    "type": gtype,
                    "status": gstatus,
                    "description": gdesc,
                    "progress_metric": gprog,
                    "parent_goal_id": gparent,
                    "checkpoints": checkpoints,
                    "created_at": gcreated,
                    "updated_at": gupdated
                })
            return goals
        finally:
            conn.close()

    def create_goal(self, type: str, description: str, progress_metric: Optional[str] = None, parent_goal_id: Optional[int] = None) -> int:
        if type not in ('short', 'long', 'stretch', 'aspirational'):
            raise ValueError("Type must be one of: 'short', 'long', 'stretch', 'aspirational'")
            
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO goals (type, status, description, progress_metric, parent_goal_id) VALUES (?, 'proposed', ?, ?, ?);",
                (type, description, progress_metric, parent_goal_id)
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def update_goal_status(self, goal_id: int, status: str) -> bool:
        if status not in ('proposed', 'active', 'in_progress', 'completed', 'abandoned', 'archived', 'deleted'):
            raise ValueError("Status must be one of: 'proposed', 'active', 'in_progress', 'completed', 'abandoned', 'archived', 'deleted'")
            
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM goals WHERE id = ?;", (goal_id,))
            if not cursor.fetchone():
                return False
            cursor.execute(
                "UPDATE goals SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (status, goal_id)
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def add_checkpoint(self, goal_id: int, description: str) -> int:
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM goals WHERE id = ?;", (goal_id,))
            if not cursor.fetchone():
                raise ValueError(f"Goal ID {goal_id} does not exist.")
            cursor.execute(
                "INSERT INTO goal_checkpoints (goal_id, checkpoint_description) VALUES (?, ?);",
                (goal_id, description)
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def complete_checkpoint(self, checkpoint_id: int) -> bool:
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM goal_checkpoints WHERE id = ?;", (checkpoint_id,))
            if not cursor.fetchone():
                return False
            
            from datetime import datetime
            now_str = datetime.utcnow().isoformat()
            cursor.execute(
                "UPDATE goal_checkpoints SET achieved = 1, achieved_at = ? WHERE id = ?;",
                (now_str, checkpoint_id)
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def get_proposals(self, status: Optional[str] = None) -> list:
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            query = "SELECT id, type, description, confidence_score, source_reason, status, created_at, updated_at FROM goal_proposals"
            params = []
            if status:
                query += " WHERE status = ?"
                params.append(status)
            query += " ORDER BY id DESC;"
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()

            proposals = []
            for row in rows:
                try:
                    proposals.append({
                        "id": row['id'],
                        "type": row['type'],
                        "description": row['description'],
                        "confidence_score": row['confidence_score'],
                        "source_reason": row['source_reason'],
                        "status": row['status'],
                        "created_at": row['created_at'],
                        "updated_at": row['updated_at']
                    })
                except (TypeError, IndexError, KeyError):
                    pid, ptype, pdesc, pconf, preason, pstatus, pcreated, pupdated = row
                    proposals.append({
                        "id": pid,
                        "type": ptype,
                        "description": pdesc,
                        "confidence_score": pconf,
                        "source_reason": preason,
                        "status": pstatus,
                        "created_at": pcreated,
                        "updated_at": pupdated
                    })
            return proposals
        finally:
            conn.close()

    def propose_goal(self, type: str, description: str, confidence_score: float, source_reason: str) -> int:
        if type not in ('short', 'long', 'stretch', 'aspirational'):
            raise ValueError("Type must be one of: 'short', 'long', 'stretch', 'aspirational'")

        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO goal_proposals (type, description, confidence_score, source_reason) VALUES (?, ?, ?, ?);",
                (type, description, confidence_score, source_reason)
            )
            conn.commit()
            proposal_id = cursor.lastrowid
            send_webhook_notification(
                "goal_proposal", f"New {type} goal proposal #{proposal_id}: {description}"
            )
            return proposal_id
        finally:
            conn.close()

    def approve_proposal(self, proposal_id: int) -> dict:
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT type, description, status FROM goal_proposals WHERE id = ?;", (proposal_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"Proposal ID {proposal_id} does not exist.")
            try:
                ptype, pdesc, pstatus = row['type'], row['description'], row['status']
            except (TypeError, IndexError, KeyError):
                ptype, pdesc, pstatus = row
            if pstatus != 'proposed':
                raise ValueError(f"Proposal ID {proposal_id} has already been resolved (status: '{pstatus}').")

            goal_id = self.create_goal(ptype, pdesc)

            cursor.execute(
                "UPDATE goal_proposals SET status = 'approved', updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (proposal_id,)
            )
            conn.commit()
            return {"success": True, "proposal_id": proposal_id, "goal_id": goal_id}
        finally:
            conn.close()

    def reject_proposal(self, proposal_id: int) -> dict:
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM goal_proposals WHERE id = ?;", (proposal_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"Proposal ID {proposal_id} does not exist.")
            try:
                pstatus = row['status']
            except (TypeError, IndexError, KeyError):
                pstatus = row[0]
            if pstatus != 'proposed':
                raise ValueError(f"Proposal ID {proposal_id} has already been resolved (status: '{pstatus}').")

            cursor.execute(
                "UPDATE goal_proposals SET status = 'rejected', updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (proposal_id,)
            )
            conn.commit()
            return {"success": True, "proposal_id": proposal_id}
        finally:
            conn.close()

    def manage_goals(self, action: str, params: dict) -> dict:
        if action == "create":
            g_type = params.get("type")
            description = params.get("description")
            progress_metric = params.get("progress_metric")
            parent_goal_id = params.get("parent_goal_id")
            
            if not g_type or not description:
                raise ValueError("Both 'type' and 'description' are required to create a goal.")
            
            goal_id = self.create_goal(g_type, description, progress_metric, parent_goal_id)
            return {"success": True, "goal_id": goal_id}
            
        elif action == "modify":
            goal_id = params.get("goal_id")
            if not goal_id:
                raise ValueError("'goal_id' is required for modification.")
            
            updates = []
            values = []
            
            for field in ("type", "status", "description", "progress_metric", "parent_goal_id"):
                if field in params:
                    val = params[field]
                    if field == "type" and val not in ('short', 'long', 'stretch', 'aspirational'):
                        raise ValueError("Type must be one of: 'short', 'long', 'stretch', 'aspirational'")
                    if field == "status" and val not in ('proposed', 'active', 'in_progress', 'completed', 'abandoned', 'archived', 'deleted'):
                        raise ValueError("Status must be one of: 'proposed', 'active', 'in_progress', 'completed', 'abandoned', 'archived', 'deleted'")
                    updates.append(f"{field} = ?")
                    values.append(val)
                    
            if not updates:
                return {"success": True, "message": "No modification parameters provided."}
                
            values.append(goal_id)
            conn = get_connection(read_only_constitution=True)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM goals WHERE id = ?;", (goal_id,))
                if not cursor.fetchone():
                    raise ValueError(f"Goal ID {goal_id} does not exist.")
                
                query = f"UPDATE goals SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?;"
                cursor.execute(query, tuple(values))
                conn.commit()
                return {"success": True, "message": f"Goal {goal_id} modified successfully."}
            finally:
                conn.close()
                
        elif action == "archive":
            goal_id = params.get("goal_id")
            if not goal_id:
                raise ValueError("'goal_id' is required for archiving.")
            success = self.update_goal_status(goal_id, "archived")
            if not success:
                raise ValueError(f"Goal ID {goal_id} does not exist.")
            return {"success": True, "message": f"Goal {goal_id} archived successfully."}
            
        elif action == "delete":
            goal_id = params.get("goal_id")
            if not goal_id:
                raise ValueError("'goal_id' is required for deletion.")
            success = self.update_goal_status(goal_id, "deleted")
            if not success:
                raise ValueError(f"Goal ID {goal_id} does not exist.")
            return {"success": True, "message": f"Goal {goal_id} marked as deleted successfully."}
            
        elif action == "checkpoint_create":
            goal_id = params.get("goal_id")
            description = params.get("description")
            if not goal_id or not description:
                raise ValueError("Both 'goal_id' and 'description' are required to create a checkpoint.")
            cp_id = self.add_checkpoint(goal_id, description)
            return {"success": True, "checkpoint_id": cp_id}
            
        elif action == "checkpoint_complete":
            checkpoint_id = params.get("checkpoint_id")
            if not checkpoint_id:
                raise ValueError("'checkpoint_id' is required to complete a checkpoint.")
            success = self.complete_checkpoint(checkpoint_id)
            if not success:
                raise ValueError(f"Checkpoint ID {checkpoint_id} does not exist.")
            return {"success": True, "message": f"Checkpoint {checkpoint_id} completed successfully."}
            
        else:
            raise ValueError(f"Unknown action: '{action}'")

class SafeDocuments:
    """Safe document management wrapper for database-backed dynamic skills."""
    
    def list(self, tag_filter: Optional[str] = None, purpose: Optional[str] = None) -> list:
        import json
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            query = "SELECT id, title, tags, purpose, metadata, created_at, updated_at FROM janus_documents"
            conditions = []
            params = []
            if tag_filter:
                conditions.append("tags LIKE ?")
                params.append(f'%"{tag_filter}"%')
            if purpose:
                conditions.append("purpose = ?")
                params.append(purpose)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY updated_at DESC;"
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            docs = []
            for row in rows:
                try:
                    did = row['id']
                    title = row['title']
                    tags = row['tags']
                    doc_purpose = row['purpose']
                    metadata = row['metadata']
                    created = row['created_at']
                    updated = row['updated_at']
                except (TypeError, KeyError, IndexError):
                    did, title, tags, doc_purpose, metadata, created, updated = row
                docs.append({
                    "id": did,
                    "title": title,
                    "tags": json.loads(tags) if tags else [],
                    "purpose": doc_purpose,
                    "metadata": json.loads(metadata) if metadata else {},
                    "created_at": created,
                    "updated_at": updated
                })
            return docs
        finally:
            conn.close()

    def get(self, title: str) -> Optional[dict]:
        import json
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, title, content, tags, purpose, metadata, created_at, updated_at "
                "FROM janus_documents WHERE title = ?;",
                (title,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            try:
                did = row['id']
                title = row['title']
                content = row['content']
                tags = row['tags']
                purpose = row['purpose']
                metadata = row['metadata']
                created = row['created_at']
                updated = row['updated_at']
            except (TypeError, KeyError, IndexError):
                did, title, content, tags, purpose, metadata, created, updated = row
            return {
                "id": did,
                "title": title,
                "content": content,
                "tags": json.loads(tags) if tags else [],
                "purpose": purpose,
                "metadata": json.loads(metadata) if metadata else {},
                "created_at": created,
                "updated_at": updated
            }
        finally:
            conn.close()

    def upsert(
        self, title: str, content: str, tags: Optional[list] = None,
        purpose: str = "memory", metadata: Optional[dict] = None
    ) -> bool:
        import json
        if purpose not in ("memory", "knowledge"):
            raise ValueError("purpose must be one of: 'memory', 'knowledge'")

        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            tags_json = json.dumps(tags if isinstance(tags, list) else [])
            metadata_json = json.dumps(metadata if isinstance(metadata, dict) else {})
            cursor.execute(
                """
                INSERT INTO janus_documents (title, content, tags, purpose, metadata, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(title) DO UPDATE SET
                    content=excluded.content, tags=excluded.tags, purpose=excluded.purpose,
                    metadata=excluded.metadata, updated_at=excluded.updated_at;
                """,
                (title, content, tags_json, purpose, metadata_json)
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def delete(self, title: str) -> bool:
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM janus_documents WHERE title = ?;", (title,))
            if not cursor.fetchone():
                return False
            cursor.execute("DELETE FROM janus_documents WHERE title = ?;", (title,))
            conn.commit()
            return True
        finally:
            conn.close()

class SafeLayeredCognition:
    """Safe layered cognition cadence and reflex controls for dynamic skills."""
    def trigger_reflex(self, action: str, priority: int = 0):
        from src.daemon import enqueue_reflex_action
        enqueue_reflex_action(action, priority)
        
    def get_layers(self) -> list:
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT layer_name, cadence_ms, is_active, last_run_at FROM cognitive_layers;")
            rows = cursor.fetchall()
            layers = []
            for row in rows:
                try:
                    name = row['layer_name']
                    cadence = row['cadence_ms']
                    active = row['is_active']
                    last_run = row['last_run_at']
                except (TypeError, IndexError, KeyError):
                    name, cadence, active, last_run = row
                layers.append({
                    "name": name,
                    "cadence_ms": int(cadence),
                    "is_active": bool(active),
                    "last_run_at": last_run
                })
            return layers
        finally:
            conn.close()

class SafeAgentOrchestration:
    """Safe external agent registration and dispatch operations for dynamic skills."""
    
    def register_agent(self, name: str, agent_type: str, endpoint: str, api_key: str, capabilities: list) -> int:
        """
        Registers a new external coding agent in the SQLite registry.
        Encrypts the API key before storage if type is 'api'.
        """
        if agent_type not in ('api', 'cli'):
            raise ValueError("Type must be either 'api' or 'cli'.")
            
        from src.security import encrypt_api_key
        enc_key = encrypt_api_key(api_key) if agent_type == 'api' else None
        caps_str = json.dumps(capabilities or [])
        
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO external_agents (name, type, endpoint, api_key_encrypted, capabilities)
                VALUES (?, ?, ?, ?, ?);
            """, (name, agent_type, endpoint, enc_key, caps_str))
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_agents(self) -> list:
        """Retrieves all registered external agents."""
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, type, endpoint, capabilities, is_active FROM external_agents;")
            rows = cursor.fetchall()
            agents = []
            for row in rows:
                try:
                    aid = row['id']
                    name = row['name']
                    atype = row['type']
                    endpoint = row['endpoint']
                    caps = row['capabilities']
                    active = row['is_active']
                except (TypeError, IndexError, KeyError):
                    aid, name, atype, endpoint, caps, active = row
                agents.append({
                    "id": aid,
                    "name": name,
                    "type": atype,
                    "endpoint": endpoint,
                    "capabilities": json.loads(caps or "[]"),
                    "is_active": bool(active)
                })
            return agents
        finally:
            conn.close()

    def dispatch_task(self, agent_name: str, task_description: str, file_paths: list = None) -> int:
        """
        Spawns a sandbox, formats context, dispatches the task to the agent,
        applies changes, runs tests, and updates dispatch logs.
        """
        from pathlib import Path
        # 1. Fetch agent details
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, type, endpoint, api_key_encrypted FROM external_agents WHERE name = ? AND is_active = 1;",
                (agent_name,)
            )
            row = cursor.fetchone()
        finally:
            conn.close()
            
        if not row:
            raise ValueError(f"Active external agent '{agent_name}' not found.")
            
        try:
            agent_id = row['id']
            agent_type = row['type']
            endpoint = row['endpoint']
            enc_key = row['api_key_encrypted']
        except (TypeError, IndexError, KeyError):
            agent_id, agent_type, endpoint, enc_key = row
            
        # 2. Write initial log record
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO dispatch_log (agent_id, task_description, status) VALUES (?, ?, 'in_progress');",
                (agent_id, task_description)
            )
            conn.commit()
            dispatch_id = cursor.lastrowid
        finally:
            conn.close()
            
        # 3. Setup git worktree sandbox session
        from src.config import get_effective_workspace_root
        
        session_name = f"dispatch_{dispatch_id}"
        sandbox_path, branch_name = create_sandbox_session(session_name)
        
        # update sandbox ID in log
        conn = get_connection(read_only_constitution=True)
        try:
            conn.execute("UPDATE dispatch_log SET sandbox_session_id = ? WHERE id = ?;", (session_name, dispatch_id))
            conn.commit()
        finally:
            conn.close()
            
        prompt_sent = ""
        response_received = ""
        
        try:
            if agent_type == 'api':
                from src.security import decrypt_api_key
                api_key = decrypt_api_key(enc_key)
                
                # Gather context files
                context_str = ""
                if file_paths:
                    for rel_path in file_paths:
                        full_path = Path(get_effective_workspace_root()) / rel_path
                        if full_path.exists():
                            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                                file_content = f.read()
                            context_str += f"\nFile: {rel_path}\n```python\n{file_content}\n```\n"
                            
                prompt = f"""You are an expert coder. Please implement the following task on the provided codebase:
TASK:
{task_description}

EXISTING CONTEXT FILES:
{context_str if context_str else "No files provided."}

Return your changes using standard SEARCH/REPLACE blocks. For each file you modify, write the blocks like this:
FILE: <relative_path>
<<<<<<< SEARCH
[exact original code lines]
=======
[replacement code lines]
>>>>>>> REPLACE
"""
                prompt_sent = prompt
                
                client = OpenAI(base_url=endpoint, api_key=api_key)
                
                logger.info(f"Dispatching API task to agent '{agent_name}' ({endpoint})...")
                completion = client.chat.completions.create(
                    model="gpt-4-turbo",
                    messages=[
                        {"role": "system", "content": "You are a professional software engineer agent."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.1
                )
                response_received = completion.choices[0].message.content
                logger.info("Received response from external API coder agent.")
                
                # Apply code modifications inside sandbox worktree
                file_segments = re.split(r"\bFILE:\s*([^\s\n\r]+)", response_received)
                
                proposed_mods = {}
                if len(file_segments) > 1:
                    for i in range(1, len(file_segments), 2):
                        filepath = file_segments[i].strip()
                        file_block = file_segments[i+1]
                        
                        full_staged_path = Path(sandbox_path) / filepath
                        current_file_content = ""
                        if full_staged_path.exists():
                            with open(full_staged_path, "r", encoding="utf-8") as f:
                                current_file_content = f.read()
                                
                        from src.self_modification import apply_search_replace_blocks
                        new_content = apply_search_replace_blocks(current_file_content, file_block)
                        proposed_mods[filepath] = new_content
                        
                if proposed_mods:
                    apply_changes_to_sandbox(proposed_mods)
                else:
                    if "<<<<<<< SEARCH" in response_received and file_paths:
                        filepath = file_paths[0]
                        full_staged_path = Path(sandbox_path) / filepath
                        current_file_content = ""
                        if full_staged_path.exists():
                            with open(full_staged_path, "r", encoding="utf-8") as f:
                                current_file_content = f.read()
                        from src.self_modification import apply_search_replace_blocks
                        new_content = apply_search_replace_blocks(current_file_content, response_received)
                        apply_changes_to_sandbox({filepath: new_content})
                    else:
                        raise ValueError("No changes applied: could not parse SEARCH/REPLACE blocks or target files.")
                        
            elif agent_type == 'cli':
                import subprocess
                cmd_parts = endpoint.split()
                cmd = cmd_parts + ["--message", task_description]
                
                prompt_sent = " ".join(cmd)
                logger.info(f"Executing CLI agent in sandbox: '{prompt_sent}'...")
                
                res = subprocess.run(
                    cmd,
                    cwd=sandbox_path,
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                response_received = f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
                logger.info(f"CLI agent finished with exit code {res.returncode}.")
                
                if res.returncode != 0:
                    raise RuntimeError(f"CLI agent exited with error: {res.stderr}")
                    
            passed, test_logs = run_sandbox_tests()
            
            status = "success" if passed else "failed"
            
            conn = get_connection(read_only_constitution=True)
            try:
                conn.execute(
                    "UPDATE dispatch_log SET prompt_sent = ?, response_received = ?, status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?;",
                    (prompt_sent, response_received, status, dispatch_id)
                )
                conn.commit()
            except Exception as update_err:
                logger.error(f"Failed to update dispatch log: {update_err}")
            finally:
                conn.close()
                
            return dispatch_id
            
        except Exception as e:
            logger.error(f"Dispatch failed: {e}", exc_info=True)
            err_msg = f"Error: {e}\n{response_received}"
            conn = get_connection(read_only_constitution=True)
            try:
                conn.execute(
                    "UPDATE dispatch_log SET prompt_sent = ?, response_received = ?, status = 'failed', completed_at = CURRENT_TIMESTAMP WHERE id = ?;",
                    (prompt_sent, err_msg, dispatch_id)
                )
                conn.commit()
            except Exception as update_err:
                logger.error(f"Failed to update dispatch log on error: {update_err}")
            finally:
                conn.close()
                
            try:
                abort_sandbox_session()
            except Exception:
                pass
                
            return dispatch_id

    def get_dispatch_status(self, dispatch_id: int) -> dict:
        """Retrieves status and details of a specific dispatch task."""
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT dl.id, ea.name, dl.task_description, dl.status, dl.sandbox_session_id, dl.created_at, dl.completed_at, dl.prompt_sent, dl.response_received
                FROM dispatch_log dl
                LEFT JOIN external_agents ea ON dl.agent_id = ea.id
                WHERE dl.id = ?;
            """, (dispatch_id,))
            row = cursor.fetchone()
            if not row:
                return {}
            try:
                did = row['id']
                aname = row['name']
                task = row['task_description']
                status = row['status']
                sandbox = row['sandbox_session_id']
                created = row['created_at']
                completed = row['completed_at']
                prompt = row['prompt_sent']
                resp = row['response_received']
            except (TypeError, IndexError, KeyError):
                did, aname, task, status, sandbox, created, completed, prompt, resp = row
                
            diff = ""
            if status in ('success', 'failed', 'in_progress') and sandbox:
                try:
                    diff = get_sandbox_diff()
                except Exception:
                    pass
                    
            return {
                "id": did,
                "agent_name": aname,
                "task_description": task,
                "status": status,
                "sandbox_session_id": sandbox,
                "created_at": created,
                "completed_at": completed,
                "prompt_sent": prompt,
                "response_received": resp,
                "diff": diff
            }
        finally:
            conn.close()

    def get_all_dispatches(self) -> list:
        """Retrieves all logged dispatches."""
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT dl.id, ea.name, dl.task_description, dl.status, dl.sandbox_session_id, dl.created_at, dl.completed_at
                FROM dispatch_log dl
                LEFT JOIN external_agents ea ON dl.agent_id = ea.id
                ORDER BY dl.created_at DESC;
            """)
            rows = cursor.fetchall()
            logs = []
            for row in rows:
                try:
                    did = row['id']
                    aname = row['name']
                    task = row['task_description']
                    status = row['status']
                    sandbox = row['sandbox_session_id']
                    created = row['created_at']
                    completed = row['completed_at']
                except (TypeError, IndexError, KeyError):
                    did, aname, task, status, sandbox, created, completed = row
                logs.append({
                    "id": did,
                    "agent_name": aname,
                    "task_description": task,
                    "status": status,
                    "sandbox_session_id": sandbox,
                    "created_at": created,
                    "completed_at": completed
                })
            return logs
        finally:
            conn.close()

    def review_dispatch(self, dispatch_id: int, approve: bool = True) -> bool:
        """
        Reviews a completed dispatch task.
        If approved: copies changes to live workspace and cleans up branch.
        If rejected: discards branch and aborts changes.
        """
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT status, sandbox_session_id FROM dispatch_log WHERE id = ?;", (dispatch_id,))
            row = cursor.fetchone()
        finally:
            conn.close()
            
        if not row:
            return False
            
        try:
            status = row['status']
            sandbox = row['sandbox_session_id']
        except (TypeError, IndexError, KeyError):
            status, sandbox = row
            
        if status not in ('success', 'failed'):
            return False
            
        if approve:
            ship_sandbox_session()
            new_status = "reviewed"
        else:
            abort_sandbox_session()
            new_status = "failed"
            
        conn = get_connection(read_only_constitution=True)
        try:
            conn.execute("UPDATE dispatch_log SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?;", (new_status, dispatch_id))
            conn.commit()
            return True
        finally:
            conn.close()

class SafeReplication:
    """Safe self-replication and parent-child spawning operations for dynamic skills."""
    
    def get_instincts(self) -> list:
        """Retrieves all current instincts from the database."""
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id, key, value, category, version, created_at FROM instincts;")
            rows = cursor.fetchall()
            instincts = []
            for row in rows:
                try:
                    iid = row['id']
                    key = row['key']
                    val = row['value']
                    cat = row['category']
                    ver = row['version']
                    created = row['created_at']
                except (TypeError, IndexError, KeyError):
                    iid, key, val, cat, ver, created = row
                instincts.append({
                    "id": iid,
                    "key": key,
                    "value": val,
                    "category": cat,
                    "version": ver,
                    "created_at": created
                })
            return instincts
        finally:
            conn.close()

    def get_children(self) -> list:
        """Retrieves all spawned child instances."""
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id, child_path, child_pid, status, spawned_at, last_heartbeat FROM spawn_log;")
            rows = cursor.fetchall()
            children = []
            for row in rows:
                try:
                    cid = row['id']
                    path = row['child_path']
                    pid = row['child_pid']
                    status = row['status']
                    spawned = row['spawned_at']
                    hb = row['last_heartbeat']
                except (TypeError, IndexError, KeyError):
                    cid, path, pid, status, spawned, hb = row
                children.append({
                    "id": cid,
                    "child_path": path,
                    "child_pid": pid,
                    "status": status,
                    "spawned_at": spawned,
                    "last_heartbeat": hb
                })
            return children
        finally:
            conn.close()

    def spawn_child(self, name: str, relative_path: str) -> dict:
        """
        Clones codebase, bootstraps database from instincts, and spawns the child process.
        """
        import shutil
        import sys
        import sqlite3
        from datetime import datetime
        from pathlib import Path
        import subprocess
        from src.config import get_effective_workspace_root
        import src.config

        # 1. Safe resolve path
        root = Path(get_effective_workspace_root()).resolve()
        full_path = Path(root / relative_path).resolve()
        if not str(full_path).startswith(str(root)):
            raise PermissionError(f"Access Denied: Path '{relative_path}' lies outside the active workspace directory.")

        # 2. Codebase copy
        def _ignore_patterns(path, names):
            ignored = []
            for n in names:
                if n in ('.venv', '.git', '.janus_sandboxes', '.janus_snapshots', '__pycache__', 'verify_phase3.db', 'verify_phase4.db', 'verify_phase5.db', 'verify_phase6.db', 'verify_phase7.db', 'verify_phase5.db-wal', 'verify_phase5.db-shm'):
                    ignored.append(n)
                elif n.endswith('.db') or n.endswith('.db-wal') or n.endswith('.db-shm'):
                    ignored.append(n)
            return ignored

        if full_path.exists():
            shutil.rmtree(full_path, ignore_errors=True)
            
        shutil.copytree(src.config.ROOT_DIR, full_path, ignore=_ignore_patterns)

        # 3. Read parent instincts
        conn_parent = get_connection(read_only_constitution=True)
        try:
            cursor_parent = conn_parent.cursor()
            cursor_parent.execute("SELECT key, value FROM instincts WHERE category = 'schema';")
            schemas = cursor_parent.fetchall()
            cursor_parent.execute("SELECT key, value, category, version FROM instincts;")
            all_instincts = cursor_parent.fetchall()
        finally:
            conn_parent.close()

        # 4. Bootstrap child database DDLs & populate data
        db_type = getattr(src.config, "DB_TYPE", "sqlite").lower()
        child_schema = None
        if db_type == "postgres":
            import psycopg2
            conn_child_raw = psycopg2.connect(src.config.DATABASE_URL)
            child_schema = f"janus_child_{name.lower()}"
            with conn_child_raw.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {child_schema};")
                cur.execute(f"SET search_path TO {child_schema};")
            from src.database import JanusConnectionWrapper
            conn_child = JanusConnectionWrapper(conn_child_raw, db_type="postgres", read_only_constitution=False)
        else:
            child_db_file = full_path / "janus.db"
            conn_child = sqlite3.connect(str(child_db_file))
            from src.database import JanusConnectionWrapper
            conn_child = JanusConnectionWrapper(conn_child, db_type="sqlite", read_only_constitution=False)

        try:
            if db_type != "postgres":
                conn_child.execute("PRAGMA foreign_keys = OFF;")
            # Run all schema DDLs
            for row in schemas:
                try:
                    sql = row['value']
                except TypeError:
                    sql = row[1]
                conn_child.execute(sql)
                
            # Populate child instincts
            for row in all_instincts:
                try:
                    ikey, ival, icat, iver = row['key'], row['value'], row['category'], row['version']
                except TypeError:
                    ikey, ival, icat, iver = row[0], row[1], row[2], row[3]
                conn_child.execute("""
                INSERT OR REPLACE INTO instincts (key, value, category, version)
                VALUES (?, ?, ?, ?);
                """, (ikey, ival, icat, iver))
                
            # Populate core_constitution
            conn_child.execute("DELETE FROM core_constitution;")
            for row in all_instincts:
                try:
                    ikey, ival, icat = row['key'], row['value'], row['category']
                except TypeError:
                    ikey, ival, icat = row[0], row[1], row[2]
                if icat == 'constitution' and ikey == 'core_constitution':
                    rules = json.loads(ival)
                    for r in rules:
                        conn_child.execute("""
                        INSERT OR IGNORE INTO core_constitution (rule_key, rule_text)
                        VALUES (?, ?);
                        """, (r['rule_key'], r['rule_text']))
                        
            # Populate agent_skills
            conn_child.execute("DELETE FROM agent_skills;")
            for row in all_instincts:
                try:
                    ikey, ival, icat = row['key'], row['value'], row['category']
                except TypeError:
                    ikey, ival, icat = row[0], row[1], row[2]
                if icat == 'tool' and ikey == 'agent_skills':
                    skills = json.loads(ival)
                    for s in skills:
                        conn_child.execute("""
                        INSERT OR REPLACE INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, 
                                                             entry_point_function, required_role, trigger_type, trigger_config, is_active)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """, (
                            s['skill_id'], s['name'], s['description'], s['parameters_schema'], s['code_blob'],
                            s['entry_point_function'], s['required_role'], s['trigger_type'], s['trigger_config'], s['is_active']
                        ))
                        
            # Populate system_config
            conn_child.execute("DELETE FROM system_config;")
            for row in all_instincts:
                try:
                    ikey, ival, icat = row['key'], row['value'], row['category']
                except TypeError:
                    ikey, ival, icat = row[0], row[1], row[2]
                if icat == 'boot' and ikey == 'system_config':
                    configs = json.loads(ival)
                    for c in configs:
                        conn_child.execute("""
                        INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable)
                        VALUES (?, ?, ?);
                        """, (c['config_key'], c['config_value'], c['is_agent_modifiable']))
                        
            # Set parent name in child DB config
            conn_child.execute("""
            INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable)
            VALUES ('parent_name', ?, 0);
            """, (name,))
            
            # Setup bilateral routing in child DB parties
            conn_child.execute("DELETE FROM parties;")
            now_str = datetime.utcnow().isoformat()
            conn_child.execute("""
            INSERT INTO parties (id, name, role, created_at, last_seen, metadata)
            VALUES ('parent', 'Parent Janus', 'admin', ?, ?, '{"type": "parent"}');
            """, (now_str, now_str))
            conn_child.execute("""
            INSERT INTO parties (id, name, role, created_at, last_seen, metadata)
            VALUES (?, ?, 'admin', ?, ?, '{"type": "self"}');
            """, (name, name, now_str, now_str))
            
            conn_child.commit()
        finally:
            conn_child.close()

        # 5. Populate Parent DB (parties and spawn_log)
        conn_parent = get_connection(read_only_constitution=True)
        try:
            now_str = datetime.utcnow().isoformat()
            conn_parent.execute("""
            INSERT OR REPLACE INTO parties (id, name, role, created_at, last_seen, metadata)
            VALUES (?, ?, 'user', ?, ?, '{"type": "child"}');
            """, (name, name, now_str, now_str))
            
            cursor_parent = conn_parent.cursor()
            cursor_parent.execute("""
            INSERT OR REPLACE INTO spawn_log (child_path, status, spawned_at)
            VALUES (?, 'spawning', CURRENT_TIMESTAMP);
            """, (str(full_path),))
            conn_parent.commit()
            spawn_id = cursor_parent.lastrowid
        finally:
            conn_parent.close()

        # 6. Launch Child Process pointing to isolated child database
        env = os.environ.copy()
        if db_type == "postgres":
            env["DB_SCHEMA"] = child_schema
            env["DB_TYPE"] = "postgres"
            env["DATABASE_URL"] = src.config.DATABASE_URL
        else:
            env["DB_PATH"] = str(child_db_file)
            env["DB_TYPE"] = "sqlite"

        spawn_provider = getattr(src.config, "SPAWN_PROVIDER", "local").lower()
        child_pid = 0
        status = "alive"

        if spawn_provider == "ecs":
            logger.info(f"ECS Spawning replica task for child '{name}'...")
            child_pid = 99999
        elif spawn_provider == "docker":
            logger.info(f"Docker Spawning container task for child '{name}'...")
            child_pid = 88888
        else:
            main_py = str(full_path / "src" / "main.py")
            process = subprocess.Popen(
                [sys.executable, main_py],
                cwd=str(full_path),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            child_pid = process.pid

        # 7. Update parent's spawn_log registry details
        conn_parent = get_connection(read_only_constitution=True)
        try:
            conn_parent.execute("""
            UPDATE spawn_log 
            SET child_pid = ?, status = ?, last_heartbeat = CURRENT_TIMESTAMP
            WHERE child_path = ?;
            """, (child_pid, status, str(full_path)))
            conn_parent.commit()
        finally:
            conn_parent.close()

        return {
            "success": True,
            "child_path": str(full_path),
            "child_pid": child_pid,
            "status": status
        }

class SafeGitHub:
    """Authenticated GitHub REST API wrapper for dynamic skills."""

    def __init__(self, party_id: Optional[str] = None):
        self.party_id = party_id
        self._base = "https://api.github.com"

    def _token(self) -> str:
        import src.config
        token = src.config.GITHUB_ACCESS_TOKEN
        if not token:
            raise PermissionError("GitHub integration disabled: GITHUB_ACCESS_TOKEN not configured.")
        return token

    def _check_rate_limit(self) -> None:
        """Enforce rolling hourly API call cap stored in system_config."""
        import time
        conn = get_connection(read_only_constitution=True)
        try:
            ws = conn.execute(
                "SELECT config_value FROM system_config WHERE config_key='github.rate_limit_window_start'"
            ).fetchone()
            calls_row = conn.execute(
                "SELECT config_value FROM system_config WHERE config_key='github.api_calls_this_hour'"
            ).fetchone()
        finally:
            conn.close()
        now = time.time()
        window_ts = float(ws[0]) if ws and ws[0] else 0.0
        n_calls = int(calls_row[0]) if calls_row and calls_row[0] else 0
        if now - window_ts > 3600:
            self._set_rate_state(str(now), "1")
            return
        if n_calls >= 50:
            raise RuntimeError(
                f"GitHub API hourly rate limit reached ({n_calls} calls). Try again later."
            )
        self._set_rate_state(str(window_ts) if window_ts else str(now), str(n_calls + 1))

    def _set_rate_state(self, window_start: str, calls: str) -> None:
        conn = get_connection(read_only_constitution=True)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) "
                "VALUES ('github.rate_limit_window_start', ?, 0)",
                (window_start,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) "
                "VALUES ('github.api_calls_this_hour', ?, 0)",
                (calls,),
            )
            conn.commit()
        finally:
            conn.close()

    def _api(self, method: str, path: str, body: Optional[dict] = None):
        import urllib.request
        self._check_rate_limit()
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self._base}{path}", data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    def list_open_issues(self, repo: str, label: Optional[str] = None) -> list:
        path = f"/repos/{repo}/issues?state=open"
        if label:
            path += f"&labels={label}"
        return self._api("GET", path)

    def get_issue(self, repo: str, number: int) -> dict:
        return self._api("GET", f"/repos/{repo}/issues/{number}")

    def create_issue(
        self, repo: str, title: str, body: str = "", labels: Optional[list] = None
    ) -> dict:
        validate_action(f"create GitHub issue on {repo}: {title}")
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        return self._api("POST", f"/repos/{repo}/issues", payload)

    def add_comment(self, repo: str, number: int, body: str) -> dict:
        validate_action(f"add GitHub comment on {repo}#{number}: {body[:80]}")
        return self._api(
            "POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}
        )

    def close_issue(self, repo: str, number: int) -> dict:
        validate_action(f"close GitHub issue {repo}#{number}")
        if not has_role(self.party_id, "contributor"):
            raise PermissionError("Closing issues requires contributor role.")
        return self._api("PATCH", f"/repos/{repo}/issues/{number}", {"state": "closed"})

    def create_pr(
        self, repo: str, title: str, body: str, head: str, base: str = "main"
    ) -> dict:
        validate_action(f"create GitHub PR on {repo}: {title}")
        if not has_role(self.party_id, "contributor"):
            raise PermissionError("Creating pull requests requires contributor role.")
        return self._api(
            "POST",
            f"/repos/{repo}/pulls",
            {"title": title, "body": body, "head": head, "base": base},
        )


def has_role(party_id: Optional[str], required_role: str) -> bool:
    """Verifies that the given party meets or exceeds the required security role."""
    if required_role == 'observer':
        return True
    if not party_id:
        return False
    if party_id == 'system':
        return True

    conn = get_connection(read_only_constitution=True)
    try:
        row = conn.execute("SELECT role FROM parties WHERE id = ?", (party_id,)).fetchone()
        if not row:
            return False
        role = row[0]
        role_hierarchy = {'observer': 0, 'user': 1, 'contributor': 2, 'admin': 3}
        return role_hierarchy.get(role, -1) >= role_hierarchy.get(required_role, 0)
    finally:
        conn.close()

class DynamicSkillExecutor:
    """Compiles and executes database-backed Python skills in isolated namespaces."""

    @staticmethod
    def execute(skill_id: str, arguments: dict, party_id: Optional[str] = None) -> dict:
        """
        Loads the dynamic skill from SQLite, checks user permissions,
        and compiles/executes its code logic.
        """
        logger.info(f"Loading skill '{skill_id}' for execution...")
        
        conn = get_connection(read_only_constitution=True)
        try:
            row = conn.execute(
                "SELECT name, code_blob, entry_point_function, required_role FROM agent_skills WHERE skill_id = ? AND is_active = 1;",
                (skill_id,)
            ).fetchone()
        finally:
            conn.close()

        if not row:
            return {"success": False, "error": f"Skill '{skill_id}' not found or is inactive."}

        name, code_blob, entry_point, required_role = row

        # Check governance role permissions
        if party_id and not has_role(party_id, required_role):
            return {"success": False, "error": f"Security Veto: Execution of skill '{skill_id}' requires role '{required_role}'."}

        # Build execution SDK context
        from src.memory_orchestrator import MemoryOrchestrator
        sdk_context = {
            "db": SafeDB(),
            "memory": SafeMemory(party_id),
            "memory_orch": MemoryOrchestrator(),
            "fs": SafeFS(),
            "drives": SafeDrives(),
            "swarm": SafeSwarm(),
            "self_model": SafeSelfModel(),
            "goals": SafeGoals(),
            "documents": SafeDocuments(),
            "layered_cognition": SafeLayeredCognition(),
            "agent_orchestration": SafeAgentOrchestration(),
            "replication": SafeReplication(),
            "explorer": SafeExplorer(),
            "codebase": SafeCodebase(),
            "sandbox": SafeSandbox(),
            "github": SafeGitHub(party_id),
            "logger": logging.getLogger(f"JanusSkill.{name.replace(' ', '')}")
        }

        # Create isolated global namespace
        namespace = {
            "__builtins__": __import__("builtins"),
            "sdk": sdk_context
        }

        try:
            # Compile and execute the module definitions in-memory
            compiled = compile(code_blob, "<dynamic_skill>", "exec")
            exec(compiled, namespace, namespace)

            # Retrieve entrypoint function
            func = namespace.get(entry_point)
            if not func or not callable(func):
                return {"success": False, "error": f"AttributeError: Entry point function '{entry_point}' not found in skill code."}

            # Run function with supplied arguments
            result = func(**arguments)
            return {"success": True, "result": result}

        except Exception as e:
            # Map tracebacks to show line numbers of the dynamic code string
            exc_type, exc_value, exc_traceback = sys.exc_info()
            tb_list = traceback.extract_tb(exc_traceback)
            formatted_tb = []
            
            for frame in tb_list:
                if frame.filename == "<dynamic_skill>":
                    lines = code_blob.splitlines()
                    line_content = lines[frame.lineno - 1] if 0 < frame.lineno <= len(lines) else ""
                    formatted_tb.append(f"  File <dynamic_skill>, line {frame.lineno}, in {frame.name}\n    {line_content}")
                else:
                    formatted_tb.append(f"  File {frame.filename}, line {frame.lineno}, in {frame.name}")
                    
            tb_str = "\n".join(formatted_tb)
            error_msg = f"Dynamic Execution Error: {exc_type.__name__}: {exc_value}\nTraceback:\n{tb_str}"
            logger.error(f"Skill execution failed for '{skill_id}': {error_msg}")
            return {"success": False, "error": error_msg}
