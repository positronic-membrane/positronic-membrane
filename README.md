# Positronic Membrane: Project Janus

Swarm AI experimentation and local autonomous agent daemon orchestration (Project Janus). Designed to run as an autonomous, self-modifying developer swarm. It is now fully cloud-ready with decoupled API, persistence, and execution sandboxing layers.

---

## Current Architecture State

*   **API & Web Server:** Upgraded from standard Python server to **FastAPI + Uvicorn**. Features bi-directional WebSockets for real-time chat (`/ws/chat`) and background agent deliberation streaming (`/ws/deliberations`).
*   **Cryptographic Security:** Secured by **RS256 JWT Authentication** using automated local RSA 2048-bit key generation. Verifies role levels (`admin`, `user`, etc.) dynamically on REST and WebSocket layers.
*   **Pluggable Persistence Adapters:** Abstracts both relational and vector data structures behind a dialect-aware query wrapper. Dynamically routes to:
    *   **Local Mode:** SQLite3 file-backed database and filesystem-backed ChromaDB collections.
    *   **Cloud Mode:** PostgreSQL instance (with restricted schema role privileges) and **pgvector** collection tables.
*   **Pluggable Sandbox Executors:** Sandbox code executions run dynamically via the `SandboxExecutor` interface based on environment configuration (`local` git worktrees, `docker` containers, or `e2b` micro-VMs).
*   **PostgreSQL Spawning Replication:** Child agent swarms are spawned into isolated database schemas (e.g. `janus_child_<name>`) on the database cluster dynamically using search path routing.

---

## Documentation

*   **[User Guide](file:///Users/jsmccauley/projects/positronic-membrane/docs/user_guide.md)**: Guide on how to run, configure, and align Janus instances.
*   **[Deployment Guide](file:///Users/jsmccauley/projects/positronic-membrane/docs/deployment_guide.md)**: Cloud provisioning, Docker builds, and Postgres schema setups.
*   **[Future Roadmap](file:///Users/jsmccauley/projects/positronic-membrane/docs/future_roadmap.md)**: Prerequisites and plans for GitHub integrations, parallel releases, and token cost caps.
*   **[Ollama Setup & Model Guide](file:///Users/jsmccauley/projects/positronic-membrane/docs/ollama_setup.md)**: Steps to download, install, run, and integrate local LLMs.
*   **[Gemini.md Specification](file:///Users/jsmccauley/projects/positronic-membrane/docs/gemini_md_specification.md)**: Core rules, schema specifications, and constraints for the Janus system.

---

## Quick Start

1. Install system and python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Set up your environment file `.env` using [Ollama Setup & Model Guide](file:///Users/jsmccauley/projects/positronic-membrane/docs/ollama_setup.md).
3. Initialize the database and run the alignment wizard:
   ```bash
   python -m src.main
   ```
4. To start the FastAPI API server:
   ```bash
   python -m src.web_server
   ```

---

## Console Escaped Commands

When running Project Janus in CLI mode (`python -m src.main --cli`), the interactive Persona chat surface supports the following escaped/slash commands:

*   `/exit`: Gracefully shuts down the active conversation console and cancels the background daemon loops.
*   `/amend <rule_key> | <rule_text>`: Proposes a new rule or amendment to be sealed in the read-only core constitution table (requires interactive `y/n` confirmation).
*   `/modify <relative_file_path> | <instructions>`: Synchronously drafts, audits, stages, and runs tests for a single file modification, prompting you to commit or reject it immediately.
*   `/stage [limit]`: Automatically parses proposed codebase changes from the most recent message (or the last `limit` messages) in the conversation, presenting an interactive menu to confirm, remove, or edit changes before running a combined staging, auditing, and test validation transaction.
*   `/sandbox start <name>`: Initializes an isolated sandbox workspace using Git Worktree on branch `janus/sandbox-<name>` under `.janus_sandboxes/session_<name>`.
*   `/sandbox status`: Displays the active sandbox path, branch, status, and modified files.
*   `/sandbox diff`: Shows a cumulative unified diff of all changes currently in the sandbox.
*   `/sandbox ship`: Runs validation tests inside the sandbox, prompts to apply all sandbox changes back to the active workspace, and disposes of the worktree environment and branch.
*   `/sandbox abort`: Discards all sandbox changes, removes the worktree environment, and deletes the branch.
