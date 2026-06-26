# Positronic Membrane: User Guide

Welcome to Positronic Membrane. This guide outlines how to configure, run, and interact with your autonomous agent swarm.

---

## 1. What is Positronic Membrane?
Positronic Membrane is a self-modifying, multi-agent developer swarm focused on continuous iteration. It speaks with a single-voice interface ("Journey") while managing specialized background worker roles behind the scenes:
*   **Proposer:** Identifies goals and drafts code changes or workspace queries.
*   **Critic:** Audits all proposals against security constraints and the core constitution.
*   **Explorer:** Crawls the web and researches unfamiliar symbols.
*   **Archivist:** Indexes codebase changes and compresses memory profiles.

---

## 2. Core Concepts

### A. The Heartbeat Daemon (`src/daemon.py`)
The system relies on an asynchronous heartbeat loop:
1.  **Idle State ($T_{idle}$):** When no user activity is detected, the daemon conserves resources by running once every $T_{idle}$ minutes (default: 15 minutes).
2.  **Boredom Counter ($B$):** While idle, a boredom vector increments. When boredom exceeds `BOREDOM_THRESHOLD`, a reflection cycle is triggered.
3.  **Active State ($T_{active}$):** When you are editing files, the daemon switches to active mode, pulsing every 1 minute to check for updates or linter errors.

### B. The Socratic Alignment Constitution
The foundation of Positronic Membrane's safety is the `core_constitution` table.
*   The system uses an **alignment wizard** (`src/setup_wizard.py`) to interview the user upon first boot.
*   Your rules are locked as read-only. The Critic agent inspects every code proposal and blocks executions if they violate your constitution.

---

## 3. Getting Started

### Configuration
Environment parameters reside in your git-ignored `.env` file:
```env
# LLM Endpoint
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5-coder:7b

# persistence
DB_TYPE=sqlite                     # "sqlite" or "postgres"
DB_PATH=/path/to/janus.db
VECTOR_DB_PATH=/path/to/chromadb

# executors
SANDBOX_PROVIDER=docker            # "local", "docker" (default), or "e2b"
SPAWN_PROVIDER=local               # "local", "docker", or "ecs"

# Offline Mock Engine
LLM_MOCK_MODE=True                 # Set to True to run offline without hitting remote APIs
```

### Initial Run
Run the setup wizard/interactive CLI:
```bash
# Launches alignment wizard or runs interactive console chat
janus-cli
```

Start the FastAPI API backend:
```bash
# Starts the FastAPI/Uvicorn server on port 5005
janus-server
```

---

## 4. Sandbox Setup

Before using any sandbox commands or triggering autonomous code modifications, the Docker sandbox image must exist on your machine.

### Build the image (one-time)
```bash
docker build -t janus:latest .
```

This only needs to be done **once per machine**. The image is a stable test runtime (Python + all project dependencies + pytest). When a sandbox runs, Positronic Membrane mounts the current worktree into the existing image at `/workspace` — the image itself is never rebuilt automatically.

**Rebuild the image when:**
- `pyproject.toml` dependencies change (new package added, version bumped, etc.)
- The image is deleted from Docker (`docker rmi janus:latest`)
- You set up on a new machine

**No rebuild needed for:**
- Restarting Positronic Membrane or the server
- Creating a new sandbox session
- Any code changes to `src/` (those are injected at container start time)

### Alternative: Local Mode (dev only)
To skip Docker entirely, set in `.env`:
```env
SANDBOX_PROVIDER=local
ALLOW_LOCAL_SANDBOX_EXEC=True
```
This runs pytest directly on the host with no isolation. Never use in production.

---

## 5. Console Commands

When using the CLI (`python -m src.main --cli`), the chat console supports interactive slash commands:

### A. Constitution Amendments
*   **`/amend <rule_key> | <rule_text>`:** Proposes a new rule or modification. The console prompts for `y/n` verification before writing to the database.

### B. Manual Sandboxing
*   **`/sandbox start <name>`:** Provisions an isolated worktree sandbox.
*   **`/sandbox status`:** Checks modified files and execution statuses.
*   **`/sandbox diff`:** Shows unified diff of pending modifications.
*   **`/sandbox ship`:** Runs pytest inside the sandbox and copies passing files to your working directory.
*   **`/sandbox abort`:** Destroys sandbox branch and discards files.

### C. Direct Code Modification
*   **`/modify <path> | <instructions>`:** Instructs the swarm to edit a specific file, automatically running tests and diff confirmation before shipping.

---

## 6. Dynamic Skills Management
Positronic Membrane stores its own executable capabilities inside the database (`agent_skills` table). This allows agents to write, test, and install **new skills** at runtime.
*   A skill consists of:
    *   **schema:** JSON parameter layout validation.
    *   **code_blob:** Executable Python code containing the implementation.
    *   **entry_point_function:** Function to trigger.
*   Before a skill runs, it undergoes an AST validation audit inside `src/sandbox.py` blocking imports of forbidden modules (`os`, `subprocess`, etc.).
