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
        # Static file routing
        else:
            self.handle_serve_static(path)

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        # API: Send message to persona
        if path == "/api/chat":
            self.handle_post_chat()
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
                if speaker in ("user", "persona"):
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
            if detect_metacognitive_intent(user_msg):
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
