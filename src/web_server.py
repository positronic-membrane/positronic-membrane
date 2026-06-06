import os
import json
import logging
import urllib.parse
import re
import uuid
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from src.database import (
    get_connection,
    log_episodic_memory,
    get_recent_episodic_memories
)
from src.persona import (
    detect_metacognitive_intent,
    generate_persona_response,
    generate_metacognitive_narrative
)
from src.memory_orchestrator import MemoryOrchestrator
from src.role_bootstrap import RoleBootstrap

# Role hierarchy for access control
ROLE_HIERARCHY = {
    'observer': 0,
    'user': 1,
    'contributor': 2,
    'admin': 3
}

memory_orch = MemoryOrchestrator()
bootstrap = RoleBootstrap()

def get_party_from_request(headers):
    """Extract party identity from request headers."""
    return headers.get('X-Party-ID', None)


def check_role(party_id, minimum_role):
    """Check if a party has at least the minimum role. Returns (ok: bool, role: str or None)."""
    if not party_id:
        return False, None
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute('SELECT role FROM parties WHERE id = ?', (party_id,)).fetchone()
        if not row:
            return False, None
        role = row['role']
        if ROLE_HIERARCHY.get(role, -1) >= ROLE_HIERARCHY.get(minimum_role, 0):
            return True, role
        return False, role
    finally:
        conn.close()

logger = logging.getLogger("JanusWebServer")

# Path to static directory
STATIC_DIR = Path(__file__).resolve().parent / "static"

class JanusRequestHandler(BaseHTTPRequestHandler):
    # Suppress verbose default request logging on stdout (which disrupts the CLI logs)
    def log_message(self, format, *args):
        logger.debug(format % args)

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Party-ID")
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        # API: Multi-party GET endpoints
        if path.startswith("/api/v1/"):
            params = urllib.parse.parse_qs(parsed_url.query)
            self._handle_api_get(path, params)
            return

        # API: Get conversation history
        if path == "/api/history":
            self.handle_get_history()
        # API: Get background deliberations
        elif path == "/api/deliberations":
            self.handle_get_deliberations()
        elif path == "/api/sandbox/status":
            self.handle_get_sandbox_status()
        elif path == "/api/sandbox/diff":
            self.handle_get_sandbox_diff()
        elif path == "/api/stage/status":
            self.handle_get_stage_status()
        elif path == "/api/constitution":
            self.handle_get_constitution()
        elif path == "/api/registry":
            self.handle_get_registry()
        elif path == "/api/registry/rules":
            self.handle_get_registry_rules()
        # Static file routing
        else:
            self.handle_serve_static(path)

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        # API: Multi-party POST endpoints
        if path.startswith("/api/v1/"):
            self._handle_api_post(path)
            return

        # API: Send message to persona
        if path == "/api/chat":
            self.handle_post_chat()
        elif path == "/api/sandbox/action":
            self.handle_post_sandbox_action()
        elif path == "/api/stage/action":
            self.handle_post_stage_action()
        elif path == "/api/constitution/amend":
            self.handle_post_constitution_amend()
        elif path == "/api/registry/update":
            self.handle_post_registry_update()
        elif path == "/api/registry/rules/update":
            self.handle_post_registry_rules_update()
        else:
            self.send_error(404, "Endpoint not found")

    def do_PUT(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        if path.startswith("/api/v1/"):
            self._handle_api_put(path)
        else:
            self.send_error(404, "Endpoint not found")

    def handle_serve_static(self, path):
        # Prevent directory traversal
        if ".." in path:
            self.send_error(403, "Access Denied")
            return

        # Default route to index.html
        if path == "/" or path == "":
            path = "/index.html"

        file_path = STATIC_DIR / path.lstrip("/")

        if not file_path.exists() or file_path.is_dir():
            self.send_error(404, "File Not Found")
            return

        # Determine MIME type
        content_type = "text/plain"
        if file_path.suffix == ".html":
            content_type = "text/html"
        elif file_path.suffix == ".css":
            content_type = "text/css"
        elif file_path.suffix == ".js":
            content_type = "application/javascript"
        elif file_path.suffix == ".png":
            content_type = "image/png"
        elif file_path.suffix == ".ico":
            content_type = "image/x-icon"

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.write_body(content)
        except Exception as e:
            logger.error(f"Error serving static file {path}: {e}")
            self.send_error(500, "Internal Server Error")

    def handle_get_history(self):
        try:
            # Fetch last 50 user-persona memories
            rows = get_recent_episodic_memories(limit=50)
            history = []
            # Rows are returned order by id DESC, so reverse them for chronological history
            for speaker, msg, ts in reversed(rows):
                if speaker in ("user", "persona", "sandbox_automation", "system"):
                    history.append({
                        "speaker": speaker,
                        "message": msg,
                        "timestamp": ts
                    })

            response_data = json.dumps(history).encode("utf-8")
            self.send_json_response(200, response_data)
        except Exception as e:
            logger.error(f"Error fetching history: {e}")
            self.send_json_error(500, f"Error fetching history: {e}")

    def handle_get_deliberations(self):
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
                # Safely parse agent debate details
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

            response_data = json.dumps(deliberations).encode("utf-8")
            self.send_json_response(200, response_data)
        except Exception as e:
            logger.error(f"Error fetching deliberations: {e}")
            self.send_json_error(500, f"Error fetching deliberations: {e}")

    def handle_get_sandbox_status(self):
        try:
            from src.sandbox_session import get_active_sandbox, get_sandbox_modified_files
            active = get_active_sandbox()
            if active:
                modified = get_sandbox_modified_files()
                data = {
                    "active": True,
                    "path": active["active_sandbox_path"],
                    "branch": active["active_sandbox_branch"],
                    "status": active["active_sandbox_status"],
                    "modified": modified,
                    "test_logs": active.get("active_sandbox_test_logs", "")
                }
            else:
                data = {"active": False}
            self.send_json_response(200, json.dumps(data).encode("utf-8"))
        except Exception as e:
            self.send_json_error(500, f"Error getting sandbox status: {e}")

    def handle_get_sandbox_diff(self):
        try:
            from src.sandbox_session import get_sandbox_diff
            diff = get_sandbox_diff()
            self.send_json_response(200, json.dumps({"diff": diff}).encode("utf-8"))
        except Exception as e:
            self.send_json_error(500, f"Error getting sandbox diff: {e}")

    def handle_get_stage_status(self):
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
                data = {
                    "active": True,
                    "file_path": pending["pending_mod_file"],
                    "dir": pending["pending_mod_dir"],
                    "diff": pending["pending_mod_diff"],
                    "status": pending["pending_mod_status"],
                    "test_logs": test_logs
                }
            else:
                data = {"active": False}
            self.send_json_response(200, json.dumps(data).encode("utf-8"))
        except Exception as e:
            self.send_json_error(500, f"Error getting stage status: {e}")

    def handle_get_constitution(self):
        try:
            from src.database import get_constitution
            rules = get_constitution()
            data = [{"key": r[0], "text": r[1]} for r in rules]
            self.send_json_response(200, json.dumps(data).encode("utf-8"))
        except Exception as e:
            self.send_json_error(500, f"Error getting constitution: {e}")

    def handle_get_registry(self):
        try:
            from src.database import get_connection
            conn = get_connection(read_only_constitution=True)
            cursor = conn.cursor()
            cursor.execute("SELECT agent_id, agent_name, system_prompt, target_model, is_active FROM agent_registry;")
            rows = cursor.fetchall()
            conn.close()
            data = [{
                "id": r[0],
                "name": r[1],
                "prompt": r[2],
                "model": r[3] or "",
                "active": bool(r[4])
            } for r in rows]
            self.send_json_response(200, json.dumps(data).encode("utf-8"))
        except Exception as e:
            self.send_json_error(500, f"Error getting agent registry: {e}")

    def handle_get_registry_rules(self):
        try:
            from src.database import get_all_agent_rules
            rules = get_all_agent_rules()
            self.send_json_response(200, json.dumps(rules).encode("utf-8"))
        except Exception as e:
            self.send_json_error(500, f"Error getting agent rules: {e}")

    def handle_post_sandbox_action(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode("utf-8"))
            action = data.get("action", "")

            from src.sandbox_session import (
                create_sandbox_session, run_sandbox_tests, ship_sandbox_session, abort_sandbox_session
            )

            if action == "start":
                name = data.get("name", "web_sandbox")
                path, branch = create_sandbox_session(name)
                log_episodic_memory(
                    "sandbox_automation",
                    f"Sandbox session '{name}' initialized on branch '{branch}'. Sandbox path: '{path}'.",
                    "user_visible"
                )
                res = {"success": True, "branch": branch, "path": path}
            elif action == "test":
                passed, logs = run_sandbox_tests()
                res = {"success": True, "passed": passed, "logs": logs}
            elif action == "ship":
                from src.sandbox_session import get_active_sandbox
                active = get_active_sandbox()
                copied = ship_sandbox_session()
                if active:
                    msg = f"Sandbox session branch '{active['active_sandbox_branch']}' successfully shipped and applied to active workspace. Files modified: {', '.join(copied)}."
                    log_episodic_memory("sandbox_automation", msg, "user_visible")
                res = {"success": True, "copied": copied}
            elif action == "abort":
                from src.sandbox_session import get_active_sandbox
                active = get_active_sandbox()
                abort_sandbox_session()
                if active:
                    msg = f"Sandbox session branch '{active['active_sandbox_branch']}' aborted and cleaned up."
                    log_episodic_memory("sandbox_automation", msg, "user_visible")
                res = {"success": True}
            else:
                self.send_json_error(400, f"Invalid sandbox action: {action}")
                return

            self.send_json_response(200, json.dumps(res).encode("utf-8"))
        except Exception as e:
            self.send_json_error(500, f"Error processing sandbox action: {e}")

    def handle_post_stage_action(self):
        try:
            import re
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode("utf-8"))
            action = data.get("action", "")

            from src.database import get_pending_modification, clear_pending_modification
            from src.self_modification import apply_staged_multi
            import shutil

            pending = get_pending_modification()
            if not pending:
                self.send_json_error(400, "No active staging session.")
                return

            res = {"success": True}

            if action == "apply":
                files = pending["pending_mod_file"].split(",")
                apply_staged_multi(pending["pending_mod_dir"], {f: True for f in files})
                for f in files:
                    log_episodic_memory("system", f"User approved staged multi-file self-modification for '{f}'.", "user_visible")
                try:
                    shutil.rmtree(pending["pending_mod_dir"])
                except Exception:
                    pass
                clear_pending_modification()

            elif action == "cancel":
                files = pending["pending_mod_file"].split(",")
                for f in files:
                    log_episodic_memory("system", f"User rejected staged multi-file self-modification for '{f}'.", "user_visible")
                try:
                    shutil.rmtree(pending["pending_mod_dir"])
                except Exception:
                    pass
                clear_pending_modification()

            elif action == "refine":
                file_path = data.get("file_path", "")
                instructions = data.get("instructions", "")
                if not file_path or not instructions:
                    self.send_json_error(400, "Missing file_path or instructions for refinement.")
                    return

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

                full_path = src.config.ROOT_DIR / file_path
                current_content = ""
                if full_path.exists():
                    with open(full_path, "r", encoding="utf-8") as file_io:
                        current_content = file_io.read()

                draft_prompt = f"""
                You are the Proposer. The user has requested a refinement for: {file_path}
                Instructions: {instructions}
                Current content: {current_content}
                Output the COMPLETE updated source code for {file_path}.
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

                proposed_mods[file_path] = proposed_code

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

                res = {"success": True, "passed": passed, "diff": diff, "logs": logs}

            elif action == "heal":
                log_path = Path(pending["pending_mod_dir"]) / "staging_test.log"
                if not log_path.exists():
                    self.send_json_error(400, "No staging test logs found to heal from.")
                    return
                with open(log_path, "r", encoding="utf-8") as f:
                    logs = f.read()

                failing_tests = []
                for match in re.findall(r"(?:FAILED|ERROR)\s+(tests/test_[a-zA-Z0-9_-]+\.py)|(tests/test_[a-zA-Z0-9_-]+\.py)::\S+\s+(?:FAILED|ERROR)", logs):
                    failing_tests.append(match[0] or match[1])
                failing_tests = sorted(list(set(failing_tests)))

                if not failing_tests:
                    self.send_json_error(400, "No failing tests detected in log file.")
                    return

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

                for test_file in failing_tests:
                    full_path = src.config.ROOT_DIR / test_file
                    current_content = ""
                    if full_path.exists():
                        with open(full_path, "r", encoding="utf-8") as file_io:
                            current_content = file_io.read()
                    draft_prompt = f"""
                    You are the Proposer. A pre-existing test file has failed during staging.
                    Fix (self-heal) this test file to pass unit tests.
                    FAILING TEST FILE: {test_file}
                    TEST RUN LOGS: {logs}
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

                res = {"success": True, "passed": passed, "diff": diff, "logs": new_logs}
            else:
                self.send_json_error(400, f"Invalid stage action: {action}")
                return

            self.send_json_response(200, json.dumps(res).encode("utf-8"))
        except Exception as e:
            self.send_json_error(500, f"Error processing stage action: {e}")

    def handle_post_constitution_amend(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode("utf-8"))
            key = data.get("key", "").strip()
            text = data.get("text", "").strip()

            if not key or not text:
                self.send_json_error(400, "Key and text cannot be empty.")
                return

            from src.database import add_constitution_rule
            add_constitution_rule(key, text)
            log_episodic_memory("system", f"User sealed constitutional rule via Web UI: '{key}' = '{text}'", "user_visible")

            self.send_json_response(200, json.dumps({"success": True}).encode("utf-8"))
        except Exception as e:
            self.send_json_error(500, f"Error amending constitution: {e}")

    def handle_post_registry_update(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode("utf-8"))
            agent_id = data.get("agent_id", "").strip()
            model = data.get("model", "").strip()

            if not agent_id:
                self.send_json_error(400, "Agent ID cannot be empty.")
                return

            from src.database import get_connection
            conn = get_connection(read_only_constitution=True)
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_registry 
                SET target_model = ?, updated_at = CURRENT_TIMESTAMP 
                WHERE agent_id = ?;
            """, (model if model else None, agent_id))
            conn.commit()
            conn.close()

            log_episodic_memory("system", f"User updated agent model override for '{agent_id}' to '{model or 'DEFAULT'}'.", "user_visible")

            self.send_json_response(200, json.dumps({"success": True}).encode("utf-8"))
        except Exception as e:
            self.send_json_error(500, f"Error updating agent registry: {e}")

    def handle_post_registry_rules_update(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode("utf-8"))
            action = data.get("action", "").strip()

            from src.database import add_agent_rule, toggle_agent_rule, delete_agent_rule

            if action == "add":
                agent_id = data.get("agent_id", "").strip()
                rule_key = data.get("rule_key", "").strip()
                rule_text = data.get("rule_text", "").strip()
                if not agent_id or not rule_key or not rule_text:
                    self.send_json_error(400, "Missing required fields: agent_id, rule_key, rule_text")
                    return
                add_agent_rule(agent_id, rule_key, rule_text)
                log_episodic_memory("system", f"User added agent rule via Web UI: [{agent_id}] '{rule_key}' = '{rule_text}'", "user_visible")
            elif action == "toggle":
                rule_key = data.get("rule_key", "").strip()
                is_active = data.get("is_active", True)
                if not rule_key:
                    self.send_json_error(400, "Missing rule_key")
                    return
                toggle_agent_rule(rule_key, is_active)
                status_str = "enabled" if is_active else "disabled"
                log_episodic_memory("system", f"User {status_str} agent rule via Web UI: '{rule_key}'", "user_visible")
            elif action == "delete":
                rule_key = data.get("rule_key", "").strip()
                if not rule_key:
                    self.send_json_error(400, "Missing rule_key")
                    return
                delete_agent_rule(rule_key)
                log_episodic_memory("system", f"User deleted agent rule via Web UI: '{rule_key}'", "user_visible")
            else:
                self.send_json_error(400, f"Invalid rule update action: {action}")
                return

            self.send_json_response(200, json.dumps({"success": True}).encode("utf-8"))
        except Exception as e:
            self.send_json_error(500, f"Error updating agent rules: {e}")

    def handle_post_chat(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode("utf-8"))
            user_msg = data.get("message", "").strip()

            if not user_msg:
                self.send_json_error(400, "Message cannot be empty")
                return

            # 1. Log user message to SQLite
            log_episodic_memory("user", user_msg, "user_visible")

            # 2. Determine intent and query appropriate generator
            if user_msg.startswith("/"):
                from src.persona import handle_web_slash_command
                import asyncio
                response = asyncio.run(handle_web_slash_command(user_msg))
            elif detect_metacognitive_intent(user_msg):
                response = generate_metacognitive_narrative(user_msg)
            else:
                response = generate_persona_response(user_msg)

            # 3. Log persona response to SQLite
            log_episodic_memory("persona", response, "user_visible")

            # Check and apply sandbox changes in a background thread (same as CLI mode)
            self.process_sandbox_updates(response)

            response_data = json.dumps({"response": response}).encode("utf-8")
            self.send_json_response(200, response_data)

        except Exception as e:
            logger.error(f"Error processing chat POST: {e}", exc_info=True)
            self.send_json_error(500, f"Error processing chat: {e}")

    def process_sandbox_updates(self, response_text):
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
                            
                            # Log success or failure back to SQLite as sandbox automation
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

    def send_json_response(self, status, binary_data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(binary_data)))
        # Enable local CORS just in case
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.write_body(binary_data)

    def _send_json(self, status_code, data):
        binary_data = json.dumps(data).encode("utf-8")
        self.send_json_response(status_code, binary_data)

    def _handle_api_get(self, path, params):
        party_id = get_party_from_request(self.headers)

        # Bootstrap status (no auth required)
        if path == "/api/v1/bootstrap/status":
            self._send_json(200, bootstrap.check_web_ui_bootstrap())
            return

        # GET /api/v1/party/{id}
        match = re.match(r"^/api/v1/party/([^/]+)$", path)
        if match:
            target_party_id = match.group(1)
            ok, _ = check_role(party_id, 'user')
            if not ok:
                self._send_json(401, {"error": "Unauthorized or missing party identification"})
                return
            conn = get_connection()
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    'SELECT id, name, role, created_at FROM parties WHERE id = ?',
                    (target_party_id,)
                ).fetchone()
                if not row:
                    self._send_json(404, {"error": "Party not found"})
                    return
                self._send_json(200, dict(row))
            finally:
                conn.close()
            return

        # GET /api/v1/memory/{key}
        match = re.match(r"^/api/v1/memory/([^/]+)$", path)
        if match:
            key = match.group(1)
            ok, _ = check_role(party_id, 'user')
            if not ok:
                self._send_json(401, {"error": "Unauthorized"})
                return
            namespace = params.get('namespace', ['global'])[0]
            value = memory_orch.get_memory(party_id, key, namespace)
            if value is None:
                self._send_json(404, {"error": "Memory not found"})
                return
            self._send_json(200, {"key": key, "value": value, "namespace": namespace})
            return

        # GET /api/v1/feedback/aggregate
        if path == "/api/v1/feedback/aggregate":
            ok, _ = check_role(party_id, 'user')
            if not ok:
                self._send_json(401, {"error": "Unauthorized"})
                return
            feature = params.get('feature', [None])[0]
            conn = get_connection()
            conn.row_factory = sqlite3.Row
            try:
                if feature:
                    rows = conn.execute(
                        'SELECT * FROM feedback_aggregates WHERE feature = ?', (feature,)
                    ).fetchall()
                else:
                    rows = conn.execute('SELECT * FROM feedback_aggregates').fetchall()
                self._send_json(200, [dict(r) for r in rows])
            finally:
                conn.close()
            return

        self._send_json(404, {"error": "Not found"})

    def _handle_api_post(self, path):
        party_id = get_party_from_request(self.headers)
        
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b""
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON body: {e}"})
            return

        # POST /api/v1/party/register (admin only)
        if path == "/api/v1/party/register":
            ok, role = check_role(party_id, 'admin')
            if not ok:
                self._send_json(403, {"error": "Insufficient permissions"})
                return
            if 'name' not in data:
                self._send_json(400, {"error": "Party name is required"})
                return
            name = data['name']
            role = data.get('role', 'user')
            if role not in ('user', 'contributor', 'admin', 'observer'):
                self._send_json(400, {"error": f"Invalid role: {role}"})
                return
            new_party_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            public_key = data.get('public_key')
            conn = get_connection()
            try:
                conn.execute(
                    'INSERT INTO parties (id, name, role, created_at, public_key) VALUES (?, ?, ?, ?, ?)',
                    (new_party_id, name, role, now, public_key)
                )
                conn.commit()
                self._send_json(201, {"party_id": new_party_id, "name": name, "role": role})
            except Exception as e:
                self._send_json(400, {"error": f"Failed to create party: {str(e)}"})
            finally:
                conn.close()
            return

        # POST /api/v1/memory
        if path == "/api/v1/memory":
            ok, _ = check_role(party_id, 'user')
            if not ok:
                self._send_json(401, {"error": "Unauthorized"})
                return
            if 'key' not in data:
                self._send_json(400, {"error": "Memory key is required"})
                return
            key = data['key']
            value = data.get('value')
            namespace = data.get('namespace', 'global')
            mem_id = memory_orch.set_memory(party_id, key, value, namespace)
            self._send_json(201, {"memory_id": mem_id})
            return

        # POST /api/v1/modification
        if path == "/api/v1/modification":
            ok, _ = check_role(party_id, 'contributor')
            if not ok:
                self._send_json(403, {"error": "Insufficient permissions (contributor or admin required)"})
                return
            if 'feature' not in data or 'diff' not in data:
                self._send_json(400, {"error": "Feature and diff are required"})
                return
            mod_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            change_type = data.get('change_type', 'modify')
            change_resource = data.get('change_resource', 'code')
            status = 'pending_self_review' if change_type in ('self_source', 'self_config') else 'pending'
            conn = get_connection()
            try:
                conn.execute(
                    'INSERT INTO modifications (id, initiated_by, feature, change_type, change_resource, diff, status, created_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (mod_id, party_id, data['feature'], change_type, change_resource, data['diff'], status, now)
                )
                conn.commit()
                self._send_json(201, {"modification_id": mod_id, "status": status})
            except Exception as e:
                self._send_json(400, {"error": f"Failed to create modification: {str(e)}"})
            finally:
                conn.close()
            return

        self._send_json(404, {"error": "Not found"})

    def _handle_api_put(self, path):
        party_id = get_party_from_request(self.headers)
        
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b""
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON body: {e}"})
            return

        # PUT /api/v1/party/{id}/role (admin only)
        match = re.match(r"^/api/v1/party/([^/]+)/role$", path)
        if match:
            target_party_id = match.group(1)
            ok, _ = check_role(party_id, 'admin')
            if not ok:
                self._send_json(403, {"error": "Insufficient permissions (admin required)"})
                return
            if 'role' not in data:
                self._send_json(400, {"error": "Role is required"})
                return
            new_role = data['role']
            if new_role not in ('user', 'contributor', 'admin', 'observer'):
                self._send_json(400, {"error": f"Invalid role: {new_role}"})
                return
            conn = get_connection()
            try:
                conn.execute('UPDATE parties SET role = ? WHERE id = ?', (new_role, target_party_id))
                conn.commit()
                self._send_json(200, {"message": f"Party {target_party_id} role updated to {new_role}"})
            finally:
                conn.close()
            return

        # PUT /api/v1/modification/{id}/approve (admin only)
        match = re.match(r"^/api/v1/modification/([^/]+)/approve$", path)
        if match:
            mod_id = match.group(1)
            ok, _ = check_role(party_id, 'admin')
            if not ok:
                self._send_json(403, {"error": "Insufficient permissions (admin required)"})
                return
            now = datetime.utcnow().isoformat()
            conn = get_connection()
            conn.row_factory = sqlite3.Row
            try:
                mod = conn.execute(
                    'SELECT status, change_type FROM modifications WHERE id = ?', (mod_id,)
                ).fetchone()
                if not mod:
                    self._send_json(404, {"error": "Modification not found"})
                    return
                if mod['change_type'] in ('self_source', 'self_config') and mod['status'] == 'pending_self_review':
                    self._send_json(400, {
                        "error": "Self-modification requires Critic deliberation. "
                                 "Please complete autonomous auditing before approval.",
                        "status": "pending_self_review"
                    })
                    return
                if mod['status'] not in ('pending', 'pending_self_review'):
                    self._send_json(400, {"error": f"Cannot approve modification in status: {mod['status']}"})
                    return
                conn.execute(
                    'UPDATE modifications SET status = ?, approved_by = ?, approved_at = ? WHERE id = ?',
                    ('approved', party_id, now, mod_id)
                )
                conn.commit()
                self._send_json(200, {"modification_id": mod_id, "status": "approved"})
            finally:
                conn.close()
            return

        # PUT /api/v1/modification/{id}/deploy (admin only)
        match = re.match(r"^/api/v1/modification/([^/]+)/deploy$", path)
        if match:
            mod_id = match.group(1)
            ok, _ = check_role(party_id, 'admin')
            if not ok:
                self._send_json(403, {"error": "Insufficient permissions (admin required)"})
                return
            now = datetime.utcnow().isoformat()
            conn = get_connection()
            conn.row_factory = sqlite3.Row
            try:
                mod = conn.execute(
                    'SELECT status FROM modifications WHERE id = ?', (mod_id,)
                ).fetchone()
                if not mod:
                    self._send_json(404, {"error": "Modification not found"})
                    return
                if mod['status'] != 'approved':
                    self._send_json(400, {"error": f"Cannot deploy modification in status: {mod['status']}"})
                    return
                conn.execute(
                    'UPDATE modifications SET status = ?, deployed_at = ? WHERE id = ?',
                    ('deployed', now, mod_id)
                )
                conn.commit()
                self._send_json(200, {"modification_id": mod_id, "status": "deployed"})
            finally:
                conn.close()
            return

        # PUT /api/v1/modification/{id}/rollback (admin only)
        match = re.match(r"^/api/v1/modification/([^/]+)/rollback$", path)
        if match:
            mod_id = match.group(1)
            ok, _ = check_role(party_id, 'admin')
            if not ok:
                self._send_json(403, {"error": "Insufficient permissions (admin required)"})
                return
            now = datetime.utcnow().isoformat()
            conn = get_connection()
            conn.row_factory = sqlite3.Row
            try:
                mod = conn.execute(
                    'SELECT status, diff, change_resource FROM modifications WHERE id = ?', (mod_id,)
                ).fetchone()
                if not mod:
                    self._send_json(404, {"error": "Modification not found"})
                    return
                if mod['status'] != 'deployed':
                    self._send_json(400, {"error": f"Cannot rollback modification in status: {mod['status']}"})
                    return
                rollback_id = str(uuid.uuid4())
                conn.execute(
                    'INSERT INTO modifications (id, initiated_by, feature, change_type, change_resource, diff, status, created_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (rollback_id, party_id, 'rollback', 'rollback', mod['change_resource'], mod['diff'], 'rolled_back', now)
                )
                conn.execute(
                    'UPDATE modifications SET status = ?, rolled_back_at = ? WHERE id = ?',
                    ('rolled_back', now, mod_id)
                )
                conn.commit()
                self._send_json(200, {"modification_id": mod_id, "status": "rolled_back", "rollback_id": rollback_id})
            finally:
                conn.close()
            return

        self._send_json(404, {"error": "Not found"})

    def send_json_error(self, status, error_message):
        resp = json.dumps({"error": error_message}).encode("utf-8")
        self.send_json_response(status, resp)

    def write_body(self, data):
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError) as conn_err:
            logger.debug(f"Client disconnected during write: {conn_err}")

def run_server(port=5005):
    """Starts the ThreadingHTTPServer. Blocks until interrupted."""
    os.makedirs(STATIC_DIR, exist_ok=True)
    server_address = ("", port)
    httpd = ThreadingHTTPServer(server_address, JanusRequestHandler)
    logger.info(f"Project Janus Web Server listening on port {port}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping web server...")
    finally:
        httpd.server_close()
