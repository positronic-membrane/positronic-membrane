import os
import json
import logging
import urllib.parse
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

logger = logging.getLogger("JanusWebServer")

# Path to static directory
STATIC_DIR = Path(__file__).resolve().parent / "static"

class JanusRequestHandler(BaseHTTPRequestHandler):
    # Suppress verbose default request logging on stdout (which disrupts the CLI logs)
    def log_message(self, format, *args):
        logger.debug(format % args)

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

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
        # Static file routing
        else:
            self.handle_serve_static(path)

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

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

            response_data = json.dumps({"response": response}).encode("utf-8")
            self.send_json_response(200, response_data)

        except Exception as e:
            logger.error(f"Error processing chat POST: {e}", exc_info=True)
            self.send_json_error(500, f"Error processing chat: {e}")

    def send_json_response(self, status, binary_data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(binary_data)))
        # Enable local CORS just in case
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.write_body(binary_data)

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
