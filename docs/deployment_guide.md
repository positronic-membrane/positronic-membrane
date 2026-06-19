# Project Janus: Cloud & Local Deployment Guide

This guide outlines deployment steps to transition Project Janus from a local experiment to a production-grade, containerized cloud swarm.

---

## 1. Local Deployment

### Prerequisites
*   Python 3.10 to 3.12.
*   Local Ollama installation or access keys to OpenRouter / OpenAI compatible endpoint.

### Steps
1.  Clone the repository and install requirements:
    ```bash
    pip install -r requirements.txt
    ```
2.  Define parameters in your `.env` file (see `.env.example`).
3.  Boot the application locally:
    ```bash
    python -m src.main
    ```

---

## 2. Dockerizing Janus

To run in the cloud, containerize Janus using the following `Dockerfile` outline:

```dockerfile
FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    docker.io \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency configs
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source directory
COPY src/ ./src
COPY tests/ ./tests

EXPOSE 8000

# Entrypoint script to start Web Server and background Swarm Daemon
CMD ["sh", "-c", "python -m src.web_server & python -m src.daemon"]
```

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

### Docker Mode
Set `SANDBOX_PROVIDER=docker` in your container environment.
*   Ensure the host container has access to the Docker daemon socket (mount `/var/run/docker.sock`).
*   Janus will run validation tests inside ephemeral containers using the `janus:latest` image.

### E2B Sandboxes (Micro-VMs)
Set `SANDBOX_PROVIDER=e2b` and define your api key:
```env
SANDBOX_PROVIDER=e2b
E2B_API_KEY=your_e2b_api_key_here
```
*   Janus automatically spins up isolated micro-VM sandboxes via E2B API, uploads the modified code files, runs tests, and reports outcomes without touching local disks.

---

## 6. Cloud Orchestrator Deployment (AWS ECS / GCP)

When deploying to AWS ECS or Google Cloud Run:
*   **Persistent Service:** The swarm requires a persistent background heartbeat task. Configure the service task count to exactly 1 (`min-instances: 1` and `replica=1`) with "Always On" CPU allocation.
*   **Spawn Integration:** Set `SPAWN_PROVIDER=ecs`. This directs the `spawn_child` skill to submit a `boto3` ECS RunTask call, spawning a new sibling container task on the ECS cluster dynamically.
