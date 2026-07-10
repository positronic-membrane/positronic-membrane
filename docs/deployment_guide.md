# Positronic Membrane: Cloud & Local Deployment Guide

This guide outlines deployment steps to transition Positronic Membrane from a local experiment to a production-grade, containerized cloud swarm.

---

## 1. Local Deployment

### Prerequisites
*   Python 3.10 to 3.12.
*   Local Ollama installation or access keys to OpenRouter / OpenAI compatible endpoint.

### Steps
1.  Clone the repository and run the setup bootstrap script:
    ```bash
    ./setup.sh
    ```
2.  Define parameters in your `.env` file (see `.env.example`).
3.  Boot the application locally using script entrypoints:
    ```bash
    # Runs the Socratic setup alignment wizard or interactive console
    janus-cli

    # Runs the FastAPI API server
    janus-server
    ```

---

## 2. Dockerizing Janus

The production `Dockerfile` is in the repository root. Key design points:

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only what pip needs to resolve dependencies before the rest of the source.
# This keeps the install layer cached as long as pyproject.toml is unchanged.
COPY pyproject.toml .
COPY src/ src/

# Install package with dev extras (pulls in pytest, pytest-asyncio, ruff).
RUN pip install --no-cache-dir ".[dev]"

# Copy remaining files (tests/, alembic.ini, static assets, etc.)
COPY . .

EXPOSE 5005

CMD ["sh", "-c", "python -m src.web_server & python -m src.daemon"]
```

> **Note:** The web server entry point (`janus-server`) calls `run_server()` in `src/web_server.py`. Neither `src/web_server` nor `src/daemon` currently define a `__main__` block, so the CMD above runs module-level import only and does not start the processes. For production Docker deployments, run `janus-server` for the web process. A standalone daemon entry point (`janus-daemon`) is not yet defined — tracking item for a future release.

---

## 3. Database Deployment (PostgreSQL & pgvector)

In production cloud environments, Janus scales using a central PostgreSQL database with pgvector enabled.

### Database Setup
1.  Spin up a cloud PostgreSQL instance (e.g. Supabase, AWS RDS, GCP Cloud SQL).
2.  Run the DDL schema initialization from `schema/postgres_schema.sql`. This:
    *   Enables the `vector` extension.
    *   Creates all system tables.
    *   Enables Postgres schema isolation support for self-replication.
3.  Configure environment variables in your deployment container:
    ```env
    DB_TYPE=postgres
    DATABASE_URL=postgresql://user:password@host:5432/dbname
    ```

### Access Role Privileges
The database schema configures two security roles to enforce safety guardrails:
*   `janus_admin`: Full database owner, used for schema creation, table migrations, and administrative setup.
*   `janus_agent`: Restrictive runtime agent role. The cursor middleware connects with this role for regular operations. It revokes write permissions (INSERT/UPDATE/DELETE) on the `core_constitution` table, ensuring rules cannot be modified at the database level.

### Database Migrations with Alembic
Alembic is wired up (`alembic.ini`, `src/migrations/`) but the `versions/` directory currently contains only an empty baseline revision. **Schema changes are not applied via Alembic** — they are applied by editing `init_db()` in `src/database.py` directly, which runs `CREATE TABLE IF NOT EXISTS` (plus hand-rolled `ALTER TABLE` migrations) on every boot.

The Alembic commands below are available but are currently no-ops beyond the baseline stamp:

#### Baseline Stamp (if upgrading an existing database)
```bash
.venv/bin/alembic stamp head
```

#### Generating a New Migration (future use)
```bash
.venv/bin/alembic revision --autogenerate -m "describe your changes"
```

---

## 4. Multi-Tenant Agent Spawning (Schema Replication)

When a parent agent spawns a child (e.g. using `spawn_child`), the system replicates without spawning new database servers:
1.  Janus connects to PostgreSQL using the connection string and creates a new isolated schema:
    ```sql
    CREATE SCHEMA IF NOT EXISTS janus_child_<name>;
    ```
2.  It copies the table schemas and seeds parent instincts to the new schema by running:
    ```sql
    SET search_path TO janus_child_<name>;
    ```
3.  The child process starts with the environment variable `DB_SCHEMA=janus_child_<name>`.
4.  All subsequent connections to Postgres automatically target this isolated schema namespace.

---

## 5. Cloud Sandbox Configuration

To run agent code validations safely without RCE risks on your host system:

### Docker Mode (Default)
`SANDBOX_PROVIDER=docker` is the default — no configuration is required to enable it.
*   Ensure the host container has access to the Docker daemon socket (mount `/var/run/docker.sock`).
*   Build the sandbox image ahead of time: `docker build -t janus:latest .` — `DockerSandboxExecutor`
    preflight-checks that the daemon is reachable and the image exists before running tests, and
    fails fast with an actionable error if either check fails (it does not auto-build).
*   **Image lifecycle:** Build once per host. The image is a stable test runtime; the worktree under
    test is mounted at `/workspace` at container start, so code changes never require a rebuild.
    Rebuild only when `pyproject.toml` dependencies change, the image is deleted, or you provision a
    new host. Restarting Positronic Membrane, the web server, or creating a new sandbox session does
    not require a rebuild.
*   Janus runs validation tests inside ephemeral, `--network none`-isolated containers (configurable
    via `DOCKER_NETWORK`) with resource limits (`DOCKER_MEMORY_LIMIT`, `DOCKER_CPU_LIMIT`,
    `DOCKER_PIDS_LIMIT`) and fixed hardening (`--cap-drop=ALL`, `--security-opt=no-new-privileges`)
    applied, using the `janus:latest` image (override via `JANUS_DOCKER_IMAGE`).

### Local Mode (Disabled by Default)
`SANDBOX_PROVIDER=local` runs pytest directly on the host with no isolation, and is hard-blocked
unless you also set `ALLOW_LOCAL_SANDBOX_EXEC=True`. Only use this for trusted local development —
never in a production/droplet deployment.

### E2B Sandboxes (Micro-VMs) — Not Implemented
`SANDBOX_PROVIDER=e2b` is **not supported**. `E2BSandboxExecutor` is an unimplemented stub — it
previously fabricated fake passing test logs instead of actually running anything, which would
have silently defeated the sandbox ship regression gate. Boot-time config validation now rejects
`SANDBOX_PROVIDER=e2b` outright (the process refuses to start). Use `docker` (recommended,
default) or `local` instead.

---

## 6. Cloud Orchestrator Deployment (AWS ECS / GCP)

When deploying to AWS ECS or Google Cloud Run:
*   **Persistent Service:** The swarm requires a persistent background heartbeat task. Configure the service task count to exactly 1 (`min-instances: 1` and `replica=1`) with "Always On" CPU allocation.
*   **Spawn Integration:** Set `SPAWN_PROVIDER=ecs`. This directs the `spawn_child` skill to submit a `boto3` ECS RunTask call, spawning a new sibling container task on the ECS cluster dynamically.
