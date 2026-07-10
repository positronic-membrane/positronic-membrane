import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load env variables from .env if present
load_dotenv()

# Root directory of the project
ROOT_DIR = Path(__file__).resolve().parent.parent

# Process start time (monotonic clock, immune to wall-clock adjustments — used
# for /health uptime_seconds) — captured once at import time
PROCESS_START_TIME = time.monotonic()

# Database configuration
DB_PATH = os.getenv("DB_PATH", str(ROOT_DIR / "janus.db"))
DB_TYPE = os.getenv("DB_TYPE", "sqlite")  # "sqlite" or "postgres"
DATABASE_URL = os.getenv("DATABASE_URL", "")  # e.g., postgresql://user:pass@host:port/dbname

# LLM Configs (Dual-Mode Compliance)
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-coder:7b")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
LLM_MOCK_MODE = os.getenv("LLM_MOCK_MODE", "False").lower() in ("true", "1", "yes")

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
SANDBOX_TEST_TIMEOUT = int(os.getenv("SANDBOX_TEST_TIMEOUT", "300"))
CHAT_TIMEOUT = int(os.getenv("CHAT_TIMEOUT", "85"))
LLM_CALL_TIMEOUT = float(os.getenv("LLM_CALL_TIMEOUT", "80.0"))
SANDBOX_PROVIDER = os.getenv("SANDBOX_PROVIDER", "docker")  # "local", "docker", or "e2b"
ALLOW_LOCAL_SANDBOX_EXEC = os.getenv("ALLOW_LOCAL_SANDBOX_EXEC", "False").lower() in ("true", "1", "yes")
E2B_API_KEY = os.getenv("E2B_API_KEY", "")
SPAWN_PROVIDER = os.getenv("SPAWN_PROVIDER", "local")      # "local", "docker", or "ecs"

# Security Configuration
REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "True").lower() in ("true", "1", "yes")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5005,http://127.0.0.1:5005").split(",")
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "60"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))

# GitHub Integration Settings
GITHUB_ENABLED = os.getenv("GITHUB_ENABLED", "False").lower() in ("true", "1", "yes")
GITHUB_ACCESS_TOKEN = os.getenv("GITHUB_ACCESS_TOKEN", "")   # restricted token (read-mostly)
GITHUB_PM_TOKEN = os.getenv("GITHUB_PM_TOKEN", "")           # PM's write-capable token
GITHUB_REPO = os.getenv("GITHUB_REPO", "")                   # e.g., "owner/repo"
# Repos that must use GITHUB_ACCESS_TOKEN even when GITHUB_PM_TOKEN is set.
# Comma-separated "owner/repo" values, e.g. "jmccauley75gh/positronic-membrane"
GITHUB_READONLY_REPOS: list = [r.strip() for r in os.getenv("GITHUB_READONLY_REPOS", "").split(",") if r.strip()]

# Agent Handoff Protocol (see src/agent_handoff.py)
AGENT_HANDOFF_TEMPLATE = os.getenv("AGENT_HANDOFF_TEMPLATE", "generic")  # "claude_code", "codex", or "generic"

# Docker Sandbox Networking & Hardening
DOCKER_NETWORK = os.getenv("DOCKER_NETWORK", "none")  # Default network isolation: none
JANUS_DOCKER_IMAGE = os.getenv("JANUS_DOCKER_IMAGE", "janus:latest")
DOCKER_MEMORY_LIMIT = os.getenv("DOCKER_MEMORY_LIMIT", "512m")
DOCKER_CPU_LIMIT = os.getenv("DOCKER_CPU_LIMIT", "1.0")
DOCKER_PIDS_LIMIT = os.getenv("DOCKER_PIDS_LIMIT", "256")

# Neo4j Graph DB (Aura or self-hosted)
NEO4J_URI      = os.getenv("NEO4J_URI", "")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

# Skills Library (sibling repo for registry sync)
SKILLS_LIBRARY_REPO   = os.getenv("SKILLS_LIBRARY_REPO",   "git@github.com:jmccauley75gh/janus-skills-library.git")
SKILLS_LIBRARY_REF    = os.getenv("SKILLS_LIBRARY_REF",    os.getenv("SKILLS_LIBRARY_BRANCH", "main"))
SKILLS_LIBRARY_BRANCH = SKILLS_LIBRARY_REF  # backward-compat alias

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
    If a sandbox session is active, returns its worktree folder.
    Else returns ROOT_DIR.
    """
    try:
        from src.sandbox_session import get_active_sandbox
        active = get_active_sandbox()
        if active and active.get("active_sandbox_path"):
            return Path(active["active_sandbox_path"])
    except Exception:
        pass

    return ROOT_DIR


_config_logger = logging.getLogger("JanusConfig")


def _nearest_existing_ancestor(path: Path) -> Path:
    current = path
    while not current.exists():
        if current.parent == current:
            return current
        current = current.parent
    return current


def _is_writable_dir(path: Path) -> bool:
    """Probes an actual write instead of trusting os.access(): os.access()
    reports a directory as writable for uid 0 even on a read-only mount,
    which would silently defeat this check for a root-run daemon."""
    probe = path / f".janus_config_check_{os.getpid()}"
    try:
        with open(probe, "w"):
            pass
        probe.unlink()
        return True
    except OSError:
        return False


@dataclass
class ConfigValidationResult:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_config() -> ConfigValidationResult:
    """
    Checks the current module-level config for boot-blocking problems
    (errors) and degraded-functionality problems (warnings). Reads live
    src.config.* values so tests can monkeypatch attributes directly.
    """
    errors = []
    warnings = []

    # Database — .lower() matches src.database.get_connection()'s own comparison
    db_type = (DB_TYPE or "").lower()
    if db_type == "postgres" and not DATABASE_URL:
        errors.append("DB_TYPE=postgres but DATABASE_URL is empty")
    if db_type != "postgres":
        parent = Path(DB_PATH).parent
        if not _is_writable_dir(_nearest_existing_ancestor(parent)):
            errors.append(
                f"DB_PATH parent directory '{parent}' is not writable "
                f"(resolved from DB_PATH={DB_PATH})"
            )

    # Auth: keys_available() is the same predicate src.auth._load_keys() uses
    # to decide whether it needs to auto-generate — kept as a lazy import (not
    # module-level) since src.auth imports ROOT_DIR from this module.
    if REQUIRE_AUTH:
        from src.auth import KEYS_DIR, keys_available
        if not keys_available() and not _is_writable_dir(_nearest_existing_ancestor(KEYS_DIR)):
            errors.append(
                "REQUIRE_AUTH=True but no JWT key pair is available via "
                "env and '.keys/' is not writable for auto-generation"
            )

    # GitHub
    if GITHUB_ENABLED and not (GITHUB_ACCESS_TOKEN or GITHUB_PM_TOKEN):
        warnings.append(
            "GITHUB_ENABLED=True but neither GITHUB_ACCESS_TOKEN nor "
            "GITHUB_PM_TOKEN is set — GitHub integration will fail at call time"
        )

    # OpenRouter routing (mirrors the routing condition in src/llm.py)
    if "/" in (LLM_MODEL or "") and not OPENROUTER_API_KEY:
        warnings.append(
            f"LLM_MODEL='{LLM_MODEL}' looks like an OpenRouter model but "
            "OPENROUTER_API_KEY is empty"
        )

    # Sandbox / spawn provider enums — compare case-insensitively, matching
    # get_sandbox_executor()'s own `.lower()` dispatch in src/sandbox_session.py,
    # so e.g. SANDBOX_PROVIDER=E2B can't slip past this check into the warning branch.
    sandbox_provider_normalized = (SANDBOX_PROVIDER or "").lower()
    if sandbox_provider_normalized not in ("local", "docker", "e2b"):
        warnings.append(f"SANDBOX_PROVIDER='{SANDBOX_PROVIDER}' is not one of local/docker/e2b")
    elif sandbox_provider_normalized == "e2b":
        errors.append(
            "SANDBOX_PROVIDER=e2b is not supported — the E2B executor is unimplemented "
            "and previously fabricated passing test results. Use 'docker' (recommended) "
            "or 'local'."
        )
    elif sandbox_provider_normalized == "local" and not ALLOW_LOCAL_SANDBOX_EXEC:
        warnings.append(
            "SANDBOX_PROVIDER=local but ALLOW_LOCAL_SANDBOX_EXEC is not set — "
            "local sandbox calls will be refused"
        )

    if SPAWN_PROVIDER not in ("local", "docker", "ecs"):
        warnings.append(f"SPAWN_PROVIDER='{SPAWN_PROVIDER}' is not one of local/docker/ecs")

    # Neo4j partial config
    if NEO4J_URI and not (NEO4J_USERNAME and NEO4J_PASSWORD):
        warnings.append("NEO4J_URI is set but NEO4J_USERNAME/NEO4J_PASSWORD are incomplete")

    return ConfigValidationResult(errors=errors, warnings=warnings)


def run_config_check() -> int:
    """
    Runs validate_config(), logs the results, and returns a process exit
    code (0 = ok, possibly with warnings; 1 = critical errors present).
    Safe to call before init_db() — does not touch the database.
    """
    result = validate_config()
    for w in result.warnings:
        _config_logger.warning(f"[config] {w}")
    if not result.ok:
        _config_logger.critical(
            "Startup configuration validation failed with %d error(s):\n  - %s",
            len(result.errors), "\n  - ".join(result.errors),
        )
        return 1
    _config_logger.info(
        "Configuration validation passed"
        + (f" with {len(result.warnings)} warning(s)." if result.warnings else ".")
    )
    return 0
