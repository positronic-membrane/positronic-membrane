import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

import src.config
from src.metrics import increment_http_requests_total
from src.routers.dependencies import (  # noqa: F401 -- unused here; re-exported so tests can patch/reach these via the web_server module
    bootstrap,
    get_connection,
    get_current_party,
    ip_request_history,
    memory_orch,
    resolve_party_by_api_key,
    resolve_party_by_fingerprint,
)

logger = logging.getLogger("JanusWebServer")

# Initialize FastAPI App
app = FastAPI(title="Positronic Membrane API Layer", version="1.0.0")

# Setup CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=src.config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple IP-based Rate Limiter (Sliding Window)
import time

from fastapi.responses import JSONResponse


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

# Request logging + counting. Registered after rate_limit_middleware so it
# becomes the OUTERMOST layer (Starlette wraps middleware in reverse
# registration order) — that way even a 429 short-circuit from the rate
# limiter still passes back through here and gets logged/counted.
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = (time.monotonic() - start) * 1000
    logger.info(
        f"{request.method} {request.url.path} {response.status_code} {duration_ms:.1f}ms"
    )
    increment_http_requests_total()
    return response

# Path to static directory
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Import routers
from src.routers import auth, chat, constitution, goals, governor, health, metrics, sandbox

# Register routers
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(sandbox.router)
app.include_router(constitution.router)
app.include_router(goals.router)
app.include_router(health.router)
app.include_router(governor.router)
app.include_router(metrics.router)

# --- Static Files / Single-Page-App Fallback ---

@app.get("/")
def serve_index():
    """Serves the front-end chat SPA interface."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse("<h1>Positronic Membrane Web Interface</h1><p>Static files directory not found or index.html missing.</p>")


@app.get("/{path:path}")
def serve_static(path: str):
    """Fallback static router for assets (scripts, styling, pictures)."""
    file_path = STATIC_DIR / path
    if file_path.exists() and file_path.is_file():
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="File not found")


# --- Server Startup ---

def run_server(port=5005, skip_agent_routing_check=False):
    """Starts the Uvicorn ASGI server. Blocks until process is interrupted.

    skip_agent_routing_check: set by src/main.py's threaded WEB-mode launch,
    which already ran run_agent_routing_check() right after init_db() in the
    same process — running it a second time here would be pure redundant
    work (issue #108). Left False by default so the standalone janus-server
    console-script entrypoint, which never calls init_db()/the routing check
    itself, still gets this safety net.
    """
    from src.config import run_agent_routing_check, run_config_check
    from src.logging_config import setup_logging
    setup_logging()
    if run_config_check() != 0:
        # os._exit (not sys.exit/raise SystemExit) because main.py may run this
        # in a background thread, where SystemExit only kills that thread and
        # is silently swallowed — the whole process must come down here.
        logger.critical("Aborting web server startup due to configuration errors.")
        os._exit(1)
    # issue #108: per-agent off-box LLM routing policy check. Degrades to a
    # warning (not a crash) if agent_registry doesn't exist yet — the
    # standalone janus-server entrypoint does not call init_db() itself.
    if not skip_agent_routing_check and run_agent_routing_check() != 0:
        logger.critical("Aborting web server startup due to agent routing policy violations.")
        os._exit(1)
    import uvicorn
    os.makedirs(STATIC_DIR, exist_ok=True)
    logger.info(f"Starting Positronic Membrane FastAPI Web Server on port {port} via Uvicorn...")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
