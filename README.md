# Positronic Membrane

Swarm AI experimentation and local autonomous agent daemon orchestration. Designed to run as an autonomous, self-modifying developer swarm. It is fully cloud-ready with decoupled API, persistence, and execution sandboxing layers.

---

## Current Architecture State

*   **API & Web Server:** FastAPI + Uvicorn. Real-time chat and background deliberation streaming via SSE.
*   **Cryptographic Security:** RS256 JWT Authentication using automated local RSA 2048-bit key generation. Role levels (`admin`, `contributor`, `user`, `observer`) enforced on REST and WebSocket layers.
*   **Pluggable Persistence Adapters:** Abstracts both relational and vector data structures behind a dialect-aware query wrapper. Dynamically routes to:
    *   **Local Mode:** SQLite3 file-backed database and filesystem-backed ChromaDB collections.
    *   **Cloud Mode:** PostgreSQL instance (with restricted schema role privileges) and **pgvector** collection tables.
*   **Pluggable Sandbox Executors:** Sandbox code executions run dynamically via the `SandboxExecutor` interface based on environment configuration (`local` git worktrees or `docker` containers).
*   **PostgreSQL Spawning Replication:** Child agent swarms are spawned into isolated database schemas (e.g. `janus_child_<name>`) on the database cluster dynamically using search path routing.

---

## Skills Architecture

Capabilities are implemented as **dynamic skills** — Python source stored as rows in the `agent_skills` database table. Skills are loaded from [janus-skills-library](https://github.com/jmccauley75gh/janus-skills-library) at boot via `sync_from_registry()` and can be reloaded on demand with the `sync_skill_library` skill.

Each skill's `code_blob` is compiled and executed by `DynamicSkillExecutor` into a namespace pre-populated with an `sdk` dict of Safe\* wrapper instances:

| Wrapper | Capability |
|---|---|
| `SafeDB` | Database queries (SQL-safety checked) |
| `SafeFS` | Filesystem access (workspace-bounded) |
| `SafeMemory` | Semantic memory (add/query) |
| `SafeSwarm` | Inter-agent messaging |
| `SafeGoals` | Goal CRUD and checkpoints |
| `SafeDocuments` | Document store |
| `SafeSandbox` | Ad-hoc code execution |
| `SafeSelfModel` | Self-model trait reads/writes |
| `SafeDrives` | Boredom/curiosity drives |
| `SafeAgentOrchestration` | Task dispatch and sandbox sessions |
| `SafeReplication` | Child instance spawning |
| `SafeLayeredCognition` | Daemon cognitive layer access |
| `SafeExplorer` | Web search / page fetch |
| `SafeCodebase` | Codebase query/index |
| `SafeGitHub` | GitHub REST API (issues, PRs, comments) |

Two skills are hardcoded in `init_db()` rather than the library: `check_presence` (the daemon's heartbeat sensor, must exist before the library sync runs) and `sync_skill_library` (the bootstrap tool that performs the sync itself).

---

## Documentation

*   **[User Guide](docs/user_guide.md)**: Guide on how to run, configure, and align Positronic Membrane instances.
*   **[Deployment Guide](docs/deployment_guide.md)**: Cloud provisioning, Docker builds, and Postgres schema setups.
*   **[Ollama Setup & Model Guide](docs/ollama_setup.md)**: Steps to download, install, run, and integrate local LLMs.
*   **[Gemini.md Specification](docs/gemini_md_specification.md)**: Core rules, schema specifications, and constraints for the Positronic Membrane system.

---

## Quick Start

1. Run the setup bootstrap script (creates `.venv`, installs all dependencies, copies `.env.example` → `.env`):
   ```bash
   ./setup.sh
   ```
2. Configure your `.env` file using the [Ollama Setup & Model Guide](docs/ollama_setup.md).
3. Initialize the database and run the alignment wizard:
   ```bash
   janus-cli
   ```
4. To start the FastAPI API server (port 5005):
   ```bash
   janus-server
   ```
5. Build the Docker sandbox image (one-time — required before using `/sandbox` commands):
   ```bash
   docker build -t janus:latest .
   ```

---

## Console Escaped Commands

When running Positronic Membrane in CLI mode (`python -m src.main --cli`), the interactive Persona chat surface supports the following escaped/slash commands:

*   `/exit`: Gracefully shuts down the active conversation console and cancels the background daemon loops.
*   `/amend <rule_key> | <rule_text>`: Proposes a new rule or amendment to be sealed in the read-only core constitution table (requires interactive `y/n` confirmation).
*   `/stage [limit]`: Automatically parses proposed codebase changes from the most recent message (or the last `limit` messages) in the conversation, presenting an interactive menu to confirm, remove, or edit changes before running a combined staging, auditing, and test validation transaction.
*   `/sandbox start <name>`: Initializes an isolated sandbox workspace using Git Worktree on branch `evolution/sandbox-<name>` under `.janus_sandboxes/session_<name>`.
*   `/sandbox status`: Displays the active sandbox path, branch, status, and modified files.
*   `/sandbox diff`: Shows a cumulative unified diff of all changes currently in the sandbox.
*   `/sandbox ship`: Runs validation tests inside the sandbox, prompts to apply all sandbox changes back to the active workspace, and disposes of the worktree environment and branch.
*   `/sandbox abort`: Discards all sandbox changes, removes the worktree environment, and deletes the branch.
