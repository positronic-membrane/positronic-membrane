import os
from pathlib import Path
from dotenv import load_dotenv

# Load env variables from .env if present
load_dotenv()

# Root directory of the project
ROOT_DIR = Path(__file__).resolve().parent.parent

# Database configuration
DB_PATH = os.getenv("DB_PATH", str(ROOT_DIR / "janus.db"))
DB_TYPE = os.getenv("DB_TYPE", "sqlite")  # "sqlite" or "postgres"
DATABASE_URL = os.getenv("DATABASE_URL", "")  # e.g., postgresql://user:pass@host:port/dbname

# LLM Configs (Dual-Mode Compliance)
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-coder:7b")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")

# Embedding & Vector DB Configs
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
VECTOR_DB_PATH = os.getenv("VECTOR_DB_PATH", str(ROOT_DIR / "data" / "chromadb"))
MEMORY_RELEVANCE_THRESHOLD = float(os.getenv("MEMORY_RELEVANCE_THRESHOLD", "1.0"))
CONSOLIDATION_THRESHOLD = int(os.getenv("CONSOLIDATION_THRESHOLD", "5"))
DEFAULT_BANNED_WEBSITES = [
    "doubleclick.net", "googleadservices.com", "adsystem.com", "adnxs.com",
    "facebook.com", "twitter.com", "instagram.com", "tiktok.com"
]

# Heartbeat Pacing (in seconds/minutes)
T_IDLE = int(os.getenv("T_IDLE", "15"))  # Default 15 minutes
T_ACTIVE = int(os.getenv("T_ACTIVE", "1"))  # Default 1 minute
BOREDOM_THRESHOLD = int(os.getenv("BOREDOM_THRESHOLD", "5"))
N_LOOP_LIMIT = int(os.getenv("N_LOOP_LIMIT", "20"))

# Sandbox execution configs
SANDBOX_TEST_TIMEOUT = int(os.getenv("SANDBOX_TEST_TIMEOUT", "60"))
SANDBOX_PROVIDER = os.getenv("SANDBOX_PROVIDER", "local")  # "local", "docker", or "e2b"
E2B_API_KEY = os.getenv("E2B_API_KEY", "")
SPAWN_PROVIDER = os.getenv("SPAWN_PROVIDER", "local")      # "local", "docker", or "ecs"

# Security Configuration
REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "True").lower() in ("true", "1", "yes")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5005,http://127.0.0.1:5005").split(",")
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "60"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))

# GitHub Integration Settings
GITHUB_ENABLED = os.getenv("GITHUB_ENABLED", "False").lower() in ("true", "1", "yes")
GITHUB_ACCESS_TOKEN = os.getenv("GITHUB_ACCESS_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # e.g., "owner/repo"

# Docker Sandbox Networking
DOCKER_NETWORK = os.getenv("DOCKER_NETWORK", "none")  # Default network isolation: none

# OpenRouter Configuration
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# Agent-specific Endpoint and Key Configs
PROPOSER_BASE_URL = os.getenv("PROPOSER_BASE_URL", None)
PROPOSER_API_KEY = os.getenv("PROPOSER_API_KEY", None)

CRITIC_BASE_URL = os.getenv("CRITIC_BASE_URL", None)
CRITIC_API_KEY = os.getenv("CRITIC_API_KEY", None)

EXPLORER_BASE_URL = os.getenv("EXPLORER_BASE_URL", None)
EXPLORER_API_KEY = os.getenv("EXPLORER_API_KEY", None)

ARCHIVIST_BASE_URL = os.getenv("ARCHIVIST_BASE_URL", None)
ARCHIVIST_API_KEY = os.getenv("ARCHIVIST_API_KEY", None)

def get_effective_workspace_root() -> Path:
    """
    Returns the Path to the active workspace directory.
    If a staging modification is active, returns its staging folder.
    Else if a sandbox session is active, returns its worktree folder.
    Else returns ROOT_DIR.
    """
    # 1. Check if staging session is active
    try:
        from src.database import get_pending_modification
        pending = get_pending_modification()
        if pending and pending.get("pending_mod_dir"):
            return Path(pending["pending_mod_dir"])
    except Exception:
        pass
        
    # 2. Check if sandbox session is active
    try:
        from src.sandbox_session import get_active_sandbox
        active = get_active_sandbox()
        if active and active.get("active_sandbox_path"):
            return Path(active["active_sandbox_path"])
    except Exception:
        pass
        
    return ROOT_DIR