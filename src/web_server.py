import os
import logging
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

import src.config
from src.routers.dependencies import (
    ROLE_HIERARCHY,
    memory_orch,
    bootstrap,
    ip_request_history,
    ChatRequest,
    SandboxActionRequest,
    ConstitutionAmendRequest,
    ConstitutionDeleteRequest,
    RegistryUpdateRequest,
    RegistryRulesUpdateRequest,
    PartyRegisterRequest,
    MemorySetRequest,
    ModificationCreateRequest,
    PartyRoleUpdateRequest,
    TokenRequest,
    verify_role,
    resolve_party_by_api_key,
    resolve_party_by_fingerprint,
    get_current_party,
    require_role,
    get_websocket_party,
    process_sandbox_updates,
    get_connection,
)
from src.persona import (
    detect_metacognitive_intent,
    generate_persona_response,
    generate_metacognitive_narrative,
    generate_persona_response_autonomous,
    handle_web_slash_command
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

# Path to static directory
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Import routers
from src.routers import auth, chat, sandbox, constitution, goals

# Register routers
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(sandbox.router)
app.include_router(constitution.router)
app.include_router(goals.router)

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

def run_server(port=5005):
    """Starts the Uvicorn ASGI server. Blocks until process is interrupted."""
    import uvicorn
    os.makedirs(STATIC_DIR, exist_ok=True)
    logger.info(f"Starting Positronic Membrane FastAPI Web Server on port {port} via Uvicorn...")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
