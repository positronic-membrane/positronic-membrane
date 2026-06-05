import os
from pathlib import Path
from dotenv import load_dotenv

# Load env variables from .env if present
load_dotenv()

# Root directory of the project
ROOT_DIR = Path(__file__).resolve().parent.parent

# Database configuration
DB_PATH = os.getenv("DB_PATH", str(ROOT_DIR / "janus.db"))

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
N_LOOP_LIMIT = int(os.getenv("N_LOOP_LIMIT", "5"))

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