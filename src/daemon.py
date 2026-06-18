import os
from typing import Optional
import re
import time
import asyncio
import logging
from pathlib import Path
import src.config
from src.llm import query_agent
from src.middleware import validate_action, SafetyViolationError, check_loop_safety
from src.memory import add_memory, query_memories, orchestrate_workspace_snapshot
from src.watcher import DirectoryWatcher
from src.database import (
    increment_boredom,
    reset_boredom,
    get_boredom_counter,
    log_episodic_memory,
    log_deliberation,
    get_recent_episodic_memories,
    get_constitution,
    get_curiosity_vector,
    update_curiosity_vector,
    get_consecutive_background_loops,
    increment_consecutive_background_loops,
    reset_consecutive_background_loops
)

# Priority queue and loop references for low-level reflexes
_reflex_queue = None
_loop = None

# Smart Loop Governor state tracking
_consecutive_stagnant_cycles = 0
_last_git_diff_hash = None
_last_db_write_count = None
_last_completed_checkpoints = None

def check_smart_governor_stagnation() -> tuple[bool, str]:
    """
    Checks if there is any progress in the current cycle across three metrics:
    1. Code changes (git diff hash)
    2. Database writes (episodic_memory + internal_deliberations row count)
    3. Checkpoint completions (completed checkpoints count)
    
    Returns (is_stagnant, justification_string)
    """
    global _consecutive_stagnant_cycles, _last_git_diff_hash, _last_db_write_count, _last_completed_checkpoints
    
    import subprocess
    import hashlib
    
    # 1. Check Git diff hash
    current_git_hash = ""
    try:
        res = subprocess.run(["git", "diff"], capture_output=True, text=True, cwd=str(src.config.ROOT_DIR), timeout=5)
        if res.returncode == 0:
            current_git_hash = hashlib.sha256(res.stdout.encode('utf-8')).hexdigest()
    except Exception:
        pass

    # 2. Check Database writes count
    current_db_writes = 0
    from src.database import get_connection
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM episodic_memory;")
        em = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM internal_deliberations;")
        id_cnt = cursor.fetchone()[0]
        current_db_writes = em + id_cnt
    except Exception:
        pass
    finally:
        conn.close()

    # 3. Check Completed checkpoints count
    current_completed_checkpoints = 0
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM goal_checkpoints WHERE achieved = 1;")
        current_completed_checkpoints = cursor.fetchone()[0]
    except Exception:
        pass
    finally:
        conn.close()

    # If this is the first run, initialize and consider as active progress
    if _last_git_diff_hash is None or _last_db_write_count is None or _last_completed_checkpoints is None:
        _last_git_diff_hash = current_git_hash
        _last_db_write_count = current_db_writes
        _last_completed_checkpoints = current_completed_checkpoints
        return False, "Governor initialized."

    # Compare values
    git_changed = (current_git_hash != _last_git_diff_hash)
    db_changed = (current_db_writes > _last_db_write_count)
    checkpoints_changed = (current_completed_checkpoints > _last_completed_checkpoints)
    
    # Update states
    _last_git_diff_hash = current_git_hash
    _last_db_write_count = current_db_writes
    _last_completed_checkpoints = current_completed_checkpoints

    if not (git_changed or db_changed or checkpoints_changed):
        _consecutive_stagnant_cycles += 1
        justification = (
            f"Stagnation detected (Consecutive Stagnant Cycles: {_consecutive_stagnant_cycles}). "
            f"Metrics: git_changed={git_changed}, db_changed={db_changed}, checkpoints_changed={checkpoints_changed}."
        )
        return True, justification
    else:
        _consecutive_stagnant_cycles = 0
        justification = (
            f"Progress registered. "
            f"Metrics: git_changed={git_changed}, db_changed={db_changed}, checkpoints_changed={checkpoints_changed}."
        )
        return False, justification

async def pause_until_user_active():
    """Waits until user_presence_status is marked active in system_config."""
    from src.skills import DynamicSkillExecutor
    from src.database import get_connection
    while True:
        try:
            DynamicSkillExecutor.execute("check_presence", {}, party_id="system")
        except Exception:
            pass
        conn = get_connection(read_only_constitution=True)
        p_val = "idle"
        try:
            r = conn.execute("SELECT config_value FROM system_config WHERE config_key = 'user_presence_status';").fetchone()
            if r:
                p_val = r[0]
        except Exception:
            pass
        finally:
            conn.close()
        if p_val == "active":
            break
        await asyncio.sleep(1 if os.getenv("JANUS_TEST_MODE") == "1" else 15)
    logger.info("User presence detected. Smart Governor reset.")
    reset_consecutive_background_loops()
    global _consecutive_stagnant_cycles
    _consecutive_stagnant_cycles = 0

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("JanusDaemon")

def detect_user_presence(workspace_path: Path, max_age_seconds: int = 300) -> bool:
    """
    Scans the workspace root folder recursively for any file modifications
    made within the last max_age_seconds.
    Ignores databases, virtual environments, cache, and git structures.
    """
    now = time.time()
    ignored_items = {
        ".git", 
        ".venv", 
        "venv", 
        "janus.db", 
        "janus.db-journal", 
        "janus.db-wal", 
        "janus.db-shm", 
        ".DS_Store", 
        "__pycache__"
    }

    try:
        for root, dirs, files in os.walk(workspace_path):
            # Prune ignored directories in-place to avoid traversing them
            dirs[:] = [d for d in dirs if d not in ignored_items]

            for file in files:
                if (file in ignored_items or 
                    file.endswith((".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3"))):
                    continue
                file_path = Path(root) / file
                try:
                    mtime = os.path.getmtime(file_path)
                    if now - mtime < max_age_seconds:
                        logger.debug(f"User presence detected via file change: {file_path}")
                        return True
                except (OSError, FileNotFoundError):
                    continue
    except Exception as e:
        logger.error(f"Error checking user presence: {e}")
        
    return False

def parse_critic_response(text: str) -> tuple:
    """
    Parses decision (0 or 1) and justification from the Critic's text response.
    """
    decision_match = re.search(r"decision:\s*(\d)", text, re.IGNORECASE)
    justification_match = re.search(r"justification:\s*(.*)", text, re.IGNORECASE | re.DOTALL)
    
    decision = 0  # Default to vetoed if parsing fails (fail-safe)
    if decision_match:
        decision = int(decision_match.group(1))
        # Ensure decision is bounded
        if decision not in (0, 1):
            decision = 0
            
    justification = "No justification provided."
    if justification_match:
        justification = justification_match.group(1).strip()
        
    return decision, justification

def run_background_maintenance():
    """
    Performs low-priority background maintenance tasks:
    1. Update the 'system' party last_seen to mark the daemon's presence.
    2. Auto-close inactive sessions (sessions with no ended_at whose associated party last_seen is older than 30 minutes).
    """
    from datetime import datetime
    from src.database import get_connection
    
    logger.debug("Executing background maintenance...")
    conn = get_connection()
    try:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        
        # 1. Update the 'system' party last_seen timestamp
        cursor.execute(
            "UPDATE parties SET last_seen = ? WHERE name = 'system';",
            (now,)
        )
        
        # 2. Auto-close sessions inactive for > 30 minutes (1800 seconds)
        cursor.execute("""
            UPDATE sessions
            SET ended_at = (SELECT last_seen FROM parties WHERE parties.id = sessions.party_id)
            WHERE ended_at IS NULL
              AND party_id IN (
                  SELECT id FROM parties 
                  WHERE datetime(last_seen) < datetime('now', '-30 minutes')
              );
        """)
        
        conn.commit()
        
        # 3. Compress episodic memory if it exceeds limits
        try:
            from src.memory import compress_episodic_memory
            compress_episodic_memory()
        except Exception as e:
            logger.error(f"Failed to compress episodic memory: {e}")
            
        logger.debug("Background maintenance completed successfully.")
    except Exception as e:
        logger.error(f"Error during background maintenance: {e}")
    finally:
        conn.close()

_last_executed_intervals = {}

def parse_action(action_str: str) -> tuple[Optional[str], dict, Optional[str]]:
    """
    Parses dynamic JSON actions or legacy tool execution statements.
    Returns (skill_id, arguments, mock_execution_result_if_any)
    """
    import json
    import re
    action_clean = action_str.strip()
    
    # 1. Try JSON parsing
    try:
        # Check if it starts/ends with markdown fences and strip them
        fence_match = re.search(r"```(?:json|python)?\s*({.*?})\s*```", action_clean, re.DOTALL)
        json_candidate = fence_match.group(1) if fence_match else None
        
        # If not, check if there's any { ... } block containing tool-like keys
        if not json_candidate:
            braces_match = re.search(r"({.*})", action_clean, re.DOTALL)
            if braces_match:
                candidate = braces_match.group(1)
                # Ensure it looks like a tool call dictionary to avoid false positives on random text
                if any(k in candidate for k in ["skill_id", "tool", "tool_name", "arguments", "args"]):
                    json_candidate = candidate
                    
        if json_candidate:
            try:
                data = json.loads(json_candidate)
                if isinstance(data, dict):
                    skill_id = data.get("skill_id") or data.get("tool") or data.get("tool_name")
                    arguments = data.get("arguments") or data.get("args") or {}
                    if skill_id:
                        return skill_id, arguments, None
            except json.JSONDecodeError as jde:
                return None, {}, f"Error: Failed to parse JSON action block. JSON syntax error: {jde}. Ensure all keys and strings use double quotes and correct syntax."
    except Exception:
        pass

    # 2. Try Legacy Parsing
    web_search_match = re.match(r"^web_search:\s*(.*)", action_clean, re.IGNORECASE) or re.search(r"\bweb_search:\s*(.*)", action_clean, re.IGNORECASE)
    fetch_url_match = re.match(r"^fetch_url:\s*(.*)", action_clean, re.IGNORECASE) or re.search(r"\bfetch_url:\s*(.*)", action_clean, re.IGNORECASE)
    read_codebase_match = re.match(r"^read_codebase:\s*(.*)", action_clean, re.IGNORECASE) or re.search(r"\bread_codebase:\s*(.*)", action_clean, re.IGNORECASE)
    scan_workspace_match = re.match(r"^scan_workspace\b", action_clean, re.IGNORECASE) or re.search(r"\bscan_workspace\b", action_clean, re.IGNORECASE)
    spawn_agent_match = re.match(r"^spawn_agent:\s*([a-z0-9_-]+)\s*\|\s*([^|]+)\s*\|\s*(.*)", action_clean, re.IGNORECASE) or re.search(r"\bspawn_agent:\s*([a-z0-9_-]+)\s*\|\s*([^|]+)\s*\|\s*(.*)", action_clean, re.IGNORECASE)
    execute_code_match = re.match(r"^execute_code:\s*(.*)", action_clean, re.DOTALL | re.IGNORECASE) or re.search(r"\bexecute_code:\s*(.*)", action_clean, re.DOTALL | re.IGNORECASE)
    modify_code_match = re.match(r"^modify_code:\s*([^|]+)\s*\|\s*(.*)", action_clean, re.DOTALL | re.IGNORECASE) or re.search(r"\bmodify_code:\s*([^|]+)\s*\|\s*(.*)", action_clean, re.DOTALL | re.IGNORECASE)
    
    write_draft_file_match = re.match(r"^write_draft_file:\s*([^|]+)\s*\|\s*(.*)", action_clean, re.DOTALL | re.IGNORECASE) or re.search(r"\bwrite_draft_file:\s*([^|]+)\s*\|\s*(.*)", action_clean, re.DOTALL | re.IGNORECASE)
    read_draft_file_match = re.match(r"^read_draft_file:\s*(.*)", action_clean, re.IGNORECASE) or re.search(r"\bread_draft_file:\s*(.*)", action_clean, re.IGNORECASE)
    list_draft_files_match = re.match(r"^list_draft_files\b", action_clean, re.IGNORECASE) or re.search(r"\blist_draft_files\b", action_clean, re.IGNORECASE)
    commit_draft_to_db_match = re.match(r"^commit_draft_to_db:\s*([^|]+)\s*\|\s*(.*)", action_clean, re.IGNORECASE) or re.search(r"\bcommit_draft_to_db:\s*([^|]+)\s*\|\s*(.*)", action_clean, re.IGNORECASE)
    checkout_db_to_draft_match = re.match(r"^checkout_db_to_draft:\s*([^|]+)\s*\|\s*(.*)", action_clean, re.IGNORECASE) or re.search(r"\bcheckout_db_to_draft:\s*([^|]+)\s*\|\s*(.*)", action_clean, re.IGNORECASE)
    document_memory_match = re.match(r"^document_memory:\s*([^|]+)(?:\s*\|\s*(.*))?", action_clean, re.IGNORECASE) or re.search(r"\bdocument_memory:\s*([^|]+)(?:\s*\|\s*(.*))?", action_clean, re.IGNORECASE)

    has_tool_keyword = any(kw in action_clean.lower() for kw in [
        "web_search", "fetch_url", "read_codebase", "scan_workspace", "spawn_agent", "execute_code", "modify_code",
        "write_draft_file", "read_draft_file", "list_draft_files", "commit_draft_to_db", "checkout_db_to_draft", "document_memory"
    ])
    any_matched = any([
        web_search_match, fetch_url_match, read_codebase_match, scan_workspace_match, spawn_agent_match, execute_code_match, modify_code_match,
        write_draft_file_match, read_draft_file_match, list_draft_files_match, commit_draft_to_db_match, checkout_db_to_draft_match, document_memory_match
    ])

    if has_tool_keyword and not any_matched:
        return None, {}, (
            f"Error: Proposed action contains a tool name but uses incorrect syntax. "
            f"Ensure your action matches standard arguments format."
        )

    if web_search_match:
        return "web_search", {"query": web_search_match.group(1).strip()}, None
    elif fetch_url_match:
        return "fetch_url", {"url": fetch_url_match.group(1).strip()}, None
    elif read_codebase_match:
        return "read_codebase", {"query": read_codebase_match.group(1).strip()}, None
    elif scan_workspace_match:
        return "scan_workspace", {}, None
    elif spawn_agent_match:
        return "spawn_agent", {
            "agent_id": spawn_agent_match.group(1).strip().lower(),
            "name": spawn_agent_match.group(2).strip(),
            "prompt": spawn_agent_match.group(3).strip()
        }, None
    elif execute_code_match:
        return "execute_code", {"code": execute_code_match.group(1).strip()}, None
    elif modify_code_match:
        return "modify_code", {
            "rel_path": modify_code_match.group(1).strip(),
            "proposed_code": modify_code_match.group(2).strip()
        }, None
    elif write_draft_file_match:
        return "write_draft_file", {
            "filename": write_draft_file_match.group(1).strip(),
            "content": write_draft_file_match.group(2).strip()
        }, None
    elif read_draft_file_match:
        return "read_draft_file", {
            "filename": read_draft_file_match.group(1).strip()
        }, None
    elif list_draft_files_match:
        return "list_draft_files", {}, None
    elif commit_draft_to_db_match:
        return "commit_draft_to_db", {
            "filename": commit_draft_to_db_match.group(1).strip(),
            "doc_title": commit_draft_to_db_match.group(2).strip()
        }, None
    elif checkout_db_to_draft_match:
        return "checkout_db_to_draft", {
            "doc_title": checkout_db_to_draft_match.group(1).strip(),
            "filename": checkout_db_to_draft_match.group(2).strip()
        }, None
    elif document_memory_match:
        act = document_memory_match.group(1).strip()
        second_param = document_memory_match.group(2)
        second_param = second_param.strip() if second_param else None
        if act.lower() == "get":
            return "document_memory", {"action": "get", "title": second_param}, None
        elif act.lower() == "list":
            return "document_memory", {"action": "list", "tag_filter": second_param}, None

    # Default fallback to mock action output
    return None, {}, f"Action successfully run. Metadata generated. Output size: {len(action_clean)} characters."

def run_interval_skills():
    """
    Finds active dynamic skills with trigger_type = 'interval', checks if their
    configured interval (in seconds) has elapsed, and runs them.
    """
    import json
    import time
    from src.database import get_connection
    from src.skills import DynamicSkillExecutor

    logger.debug("Checking interval-triggered skills...")
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT skill_id, trigger_config FROM agent_skills WHERE is_active = 1 AND trigger_type = 'interval';"
        )
        rows = cursor.fetchall()
    except Exception as e:
        logger.error(f"Failed to query interval skills: {e}")
        return
    finally:
        conn.close()

    now = time.time()
    for skill_id, trigger_config_str in rows:
        try:
            config = json.loads(trigger_config_str or "{}")
        except Exception:
            config = {}
        
        interval_seconds = config.get("interval_seconds", 600)  # Default to 10 minutes
        
        # If in test mode, speed up intervals by a factor of 60 (or run every 2-10 seconds)
        if os.getenv("JANUS_TEST_MODE") == "1":
            interval_seconds = max(2, interval_seconds // 60)

        last_run = _last_executed_intervals.get(skill_id, 0)
        if now - last_run >= interval_seconds:
            logger.info(f"Triggering interval skill '{skill_id}'...")
            _last_executed_intervals[skill_id] = now
            try:
                res = DynamicSkillExecutor.execute(skill_id, {}, party_id="system")
                if res["success"]:
                    logger.info(f"Interval skill '{skill_id}' completed: {res['result']}")
                else:
                    logger.error(f"Interval skill '{skill_id}' failed: {res['error']}")
            except Exception as e:
                logger.error(f"Interval skill '{skill_id}' crashed: {e}")

_pending_swarm_triggers = []

def trigger_swarm_reflection():
    """
    Schedules an autonomous reflection/debate cycle.
    This appends a trigger to the queue, which is processed on the next heartbeat loop tick.
    """
    logger.info("Swarm reflection event triggered.")
    _pending_swarm_triggers.append(True)

def enqueue_reflex_action(action: str, priority: int = 0):
    """
    Enqueues a reflex action with priority. Thread-safe wrapper.
    """
    global _loop, _reflex_queue
    if _loop is None or _reflex_queue is None:
        logger.warning("Event loop or reflex queue not initialized yet. Cannot enqueue reflex action.")
        return
        
    def _enqueue():
        # priority queue sorts ascending, so we push negative priority to pop highest first
        _reflex_queue.put_nowait((-priority, action))
        
    try:
        _loop.call_soon_threadsafe(_enqueue)
    except RuntimeError as e:
        logger.warning(f"Failed to enqueue reflex action thread-safely: {e}")

def get_cadence_seconds(layer_name: str, default_ms: int) -> float:
    """
    Retrieves the configured cadence (in seconds) for a given layer from cognitive_layers.
    Falls back to default_ms if the query fails or layer is not active/defined.
    """
    if os.getenv("JANUS_TEST_MODE") == "1":
        if layer_name == "high":
            return 2.0
        elif layer_name == "mid":
            return 1.0
        elif layer_name == "low":
            return 0.1

    from src.database import get_connection
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT cadence_ms, is_active FROM cognitive_layers WHERE layer_name = ?;", (layer_name,))
        row = cursor.fetchone()
        if row:
            try:
                cadence_ms = row['cadence_ms']
                is_active = row['is_active']
            except (TypeError, IndexError, KeyError):
                cadence_ms, is_active = row
            if is_active:
                return float(cadence_ms) / 1000.0
    except Exception as e:
        logger.error(f"Error querying cadence for layer '{layer_name}': {e}")
    finally:
        conn.close()
            
    return float(default_ms) / 1000.0

async def run_high_layer_loop():
    """
    Runs high-level cadence tasks: self-model decay, memory consolidation, goal evaluations.
    """
    from src.skills import DynamicSkillExecutor
    from src.database import get_connection
    
    logger.info("High-level strategic loop started.")
    try:
        while True:
            cadence = get_cadence_seconds("high", 60000)
            await asyncio.sleep(cadence)
            
            logger.info("High-level strategic tick processing...")
            
            # Execute self-model decay
            try:
                res = DynamicSkillExecutor.execute("decay_self_model", {}, party_id="system")
                logger.info(f"High-level decay_self_model result: {res}")
            except Exception as e:
                logger.error(f"High-level decay_self_model failed: {e}")
                
            # Execute memory consolidation
            try:
                res = DynamicSkillExecutor.execute("consolidate_memories", {}, party_id="system")
                logger.info(f"High-level consolidate_memories result: {res}")
            except Exception as e:
                logger.error(f"High-level consolidate_memories failed: {e}")
                
            # Execute goal evaluations
            try:
                res = DynamicSkillExecutor.execute("evaluate_goals", {}, party_id="system")
                logger.info(f"High-level evaluate_goals result: {res}")
            except Exception as e:
                logger.error(f"High-level evaluate_goals failed: {e}")
                
            # Execute episodic memory cleanup
            try:
                res = DynamicSkillExecutor.execute("cleanup_episodic_memory", {}, party_id="system")
                logger.info(f"High-level cleanup_episodic_memory result: {res}")
            except Exception as e:
                logger.error(f"High-level cleanup_episodic_memory failed: {e}")
                
            # Update last run timestamp in database
            conn = get_connection(read_only_constitution=True)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE cognitive_layers SET last_run_at = CURRENT_TIMESTAMP WHERE layer_name = 'high';"
                )
                conn.commit()
            except Exception as e:
                logger.error(f"Failed to update high layer last_run_at: {e}")
            finally:
                conn.close()
                
    except asyncio.CancelledError:
        logger.info("High-level loop cancelled.")

async def run_mid_layer_loop():
    """
    Runs mid-level real-time loop: presence check, drive increments, background maintenance, and schedules reflection ticks.
    """
    from src.skills import DynamicSkillExecutor
    from src.database import get_connection
    global _consecutive_stagnant_cycles
    
    logger.info("Mid-level real-time loop started.")
    try:
        while True:
            cadence = get_cadence_seconds("mid", 5000)
            await asyncio.sleep(cadence)
            
            logger.info("Mid-level real-time tick processing...")
            
            # 1. Presence check
            try:
                res = DynamicSkillExecutor.execute("check_presence", {}, party_id="system")
                logger.debug(f"Mid-level check_presence result: {res}")
            except Exception as e:
                logger.error(f"Mid-level check_presence failed: {e}")
                
            # 2. Drive increments
            try:
                res = DynamicSkillExecutor.execute("evaluate_drives", {}, party_id="system")
                logger.debug(f"Mid-level evaluate_drives result: {res}")
            except Exception as e:
                logger.error(f"Mid-level evaluate_drives failed: {e}")
                
            # 3. Background maintenance
            try:
                run_background_maintenance()
            except Exception as e:
                logger.error(f"Failed to run background maintenance: {e}")
                
            # 4. Check user presence status from database
            presence_status = "idle"
            conn = get_connection(read_only_constitution=True)
            try:
                row = conn.execute("SELECT config_value FROM system_config WHERE config_key = 'user_presence_status';").fetchone()
                if row:
                    presence_status = row[0]
            except Exception as e:
                logger.error(f"Failed to query presence_status: {e}")
            finally:
                conn.close()
                
            user_active = (presence_status == "active")
            
            if not user_active:
                increment_consecutive_background_loops()
                loop_count = get_consecutive_background_loops()
                logger.info(f"Background Loop Count: {loop_count}/{src.config.N_LOOP_LIMIT}")
                
                # Check smart governor progress and stagnation
                is_stagnant, justification = check_smart_governor_stagnation()
                logger.info(f"Smart Governor: {justification}")
                
                stagnant_threshold = 3
                conn = get_connection(read_only_constitution=True)
                try:
                    r = conn.execute("SELECT config_value FROM system_config WHERE config_key = 'governor.stagnant_threshold';").fetchone()
                    if r:
                        stagnant_threshold = int(r[0])
                except Exception:
                    pass
                finally:
                    conn.close()

                hard_cap = getattr(src.config, "N_LOOP_LIMIT", 20)
                
                if _consecutive_stagnant_cycles >= stagnant_threshold:
                    log_msg = f"Smart Governor Halt: background cycle stagnation threshold of {stagnant_threshold} met. Pausing background automations."
                    logger.warning(log_msg)
                    log_episodic_memory(
                        speaker="system",
                        message_content=log_msg,
                        context_type="background_thought"
                    )
                    await pause_until_user_active()
                elif loop_count > hard_cap:
                    log_msg = f"Smart Governor Halt: background loop hard cap of {hard_cap} exceeded. Pausing background automations."
                    logger.warning(log_msg)
                    log_episodic_memory(
                        speaker="system",
                        message_content=log_msg,
                        context_type="background_thought"
                    )
                    await pause_until_user_active()
            else:
                reset_consecutive_background_loops()
                _consecutive_stagnant_cycles = 0
                
            # 5. Run other interval skills (excluding high-level ones and presence/drive check since we ran them)
            try:
                run_interval_skills()
            except Exception as e:
                logger.error(f"Failed to run interval skills: {e}")
                
            # 6. Process reflection triggers if any
            global _pending_swarm_triggers
            if _pending_swarm_triggers:
                logger.info("Processing pending swarm reflection trigger in mid-level loop...")
                while _pending_swarm_triggers:
                    _pending_swarm_triggers.pop(0)
                try:
                    res = DynamicSkillExecutor.execute("run_reflection_cycle", {}, party_id="system")
                    logger.info(f"Reflection cycle result: {res}")
                except Exception as e:
                    logger.error(f"Reflection cycle failed: {e}")
                    
            # 7. Update last run timestamp in database
            conn = get_connection(read_only_constitution=True)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE cognitive_layers SET last_run_at = CURRENT_TIMESTAMP WHERE layer_name = 'mid';"
                )
                conn.commit()
            except Exception as e:
                logger.error(f"Failed to update mid layer last_run_at: {e}")
            finally:
                conn.close()
                
    except asyncio.CancelledError:
        logger.info("Mid-level loop cancelled.")

async def reflex_queue_worker():
    """
    Pops reflex actions from the priority queue and executes them immediately.
    """
    from src.skills import DynamicSkillExecutor
    from src.database import get_connection
    
    logger.info("Reflex queue worker started.")
    try:
        while True:
            # Pop next item
            neg_priority, action = await _reflex_queue.get()
            priority = -neg_priority
            logger.info(f"Reflex popped: action='{action}', priority={priority}")
            
            try:
                res = DynamicSkillExecutor.execute(action, {}, party_id="system")
                if res["success"]:
                    logger.info(f"Reflex action '{action}' executed successfully: {res['result']}")
                else:
                    logger.error(f"Reflex action '{action}' execution failed: {res['error']}")
            except Exception as e:
                logger.error(f"Error executing reflex action '{action}': {e}")
                
            # Update last run timestamp in database
            conn = get_connection(read_only_constitution=True)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE cognitive_layers SET last_run_at = CURRENT_TIMESTAMP WHERE layer_name = 'low';"
                )
                conn.commit()
            except Exception as e:
                logger.error(f"Failed to update low layer last_run_at: {e}")
            finally:
                conn.close()
                
            _reflex_queue.task_done()
            
    except asyncio.CancelledError:
        logger.info("Reflex queue worker cancelled.")

async def run_heartbeat_loop():
    """
    Main entry point for the layered cognition daemon.
    Runs concurrent high-level, mid-level, and priority reflex queue worker routines.
    """
    global _reflex_queue, _loop
    logger.info("Initializing Janus Layered Cognition Heartbeat Loop...")
    
    _reflex_queue = asyncio.PriorityQueue()
    _loop = asyncio.get_running_loop()
    
    reset_consecutive_background_loops()
    reset_boredom()
    
    log_episodic_memory(
        speaker="system",
        message_content="Janus Layered Cognition Heartbeat Loop started.",
        context_type="background_thought"
    )

    # Build initial codebase index on startup
    try:
        from src.codebase import index_codebase
        index_codebase()
    except Exception as e:
        logger.error(f"Failed to build initial codebase index: {e}")

    # Start DirectoryWatcher in background thread
    import threading

    stop_watcher_event = threading.Event()
    
    def watcher_callback(changes):
        try:
            orchestrate_workspace_snapshot(changes)
        except Exception as e:
            logger.error(f"Error orchestrating workspace snapshot: {e}")
            
        added_files = changes.get('added', [])
        modified_files = changes.get('modified', [])
        changed_files = added_files + modified_files
        
        if not changed_files:
            return
            
        from src.database import get_connection
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT trigger_pattern, action, priority FROM reflex_rules WHERE is_enabled = 1;")
            rules = cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to fetch reflex rules: {e}")
            rules = []
        finally:
            conn.close()
            
        for pattern, action, priority in rules:
            try:
                rx = re.compile(pattern)
                for filepath in changed_files:
                    if rx.search(filepath):
                        logger.info(f"File '{filepath}' matches reflex trigger '{pattern}'. Enqueuing action '{action}' (priority {priority}).")
                        enqueue_reflex_action(action, priority)
                        break
            except Exception as e:
                logger.error(f"Error evaluating pattern '{pattern}' on file changes: {e}")

    watcher = DirectoryWatcher(
        path=str(src.config.ROOT_DIR),
        callback=watcher_callback
    )
    def watcher_thread_func():
        watcher.watch(interval=2.0, stop_event=stop_watcher_event)

    watcher_thread = threading.Thread(target=watcher_thread_func, daemon=True)
    watcher_thread.start()
    logger.info(f"DirectoryWatcher started on path: {src.config.ROOT_DIR}")

    try:
        await asyncio.gather(
            run_high_layer_loop(),
            run_mid_layer_loop(),
            reflex_queue_worker()
        )
    except asyncio.CancelledError:
        logger.info("Heartbeat loop cancelled gracefully.")
    except Exception as e:
        logger.critical(f"Unhandled error in heartbeat daemon loop: {e}", exc_info=True)
    finally:
        logger.info("Stopping DirectoryWatcher...")
        stop_watcher_event.set()
        watcher_thread.join(timeout=3.0)
        logger.info("DirectoryWatcher stopped.")
