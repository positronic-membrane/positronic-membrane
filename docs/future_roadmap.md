# Project Janus — Future Roadmap & Advanced Enhancements

This document outlines the advanced architectural gaps, security upgrades, and swarm capabilities identified during the design phases. These items represent the next evolutionary steps for Project Janus as it transitions from a local development swarm to a secure, cloud-compatible cognitive network.

---

## 1. Advanced Security & Cloud-Native Sandboxing

### A. Cryptographic Session Security
* **Concept:** Transition from simple plain-text `X-Party-ID` header checks to cryptographic authentication.
* **Proposed Implementation:** Parties store public keys in the database on enrollment. The server issues short-lived JWTs (JSON Web Tokens) verified using challenge-response signature verification.

### B. Cloud-Native Sandbox Isolation
* **Concept:** Support pluggable containerization backends for multi-tenant cloud deployments.
* **Proposed Implementation:** Extend `sandbox_session.py` to allow executing test runs and dynamic code in ephemeral **Docker containers** or **Firecracker MicroVMs** with strict CPU, memory, disk, and network constraints.

### C. Cost Auditing & Token Quotas
* **Concept:** Prevent runaway API bills in multi-user/multi-tenant settings.
* **Proposed Implementation:** Enforce billing caps or token limits per Party and Session. The core middleware intercepts LLM API calls and blocks them if the quota is exceeded.

### D. Concurrency & Streaming WebSockets
* **Concept:** Enable real-time interactions and remove SQLite database locks during swarm debates.
* **Proposed Implementation:** Migrate agent-to-agent and UI-to-daemon communication to an async event bus (e.g. `asyncio.Queue`) and stream token completions live via WebSockets.

---

## 2. Swarm Concurrency & Lifecycle Enhancements

### A. Bilateral Swarm Consensus
* **Concept:** High-impact execution proposals (like self-modification or database DDL changes) undergo debate and consensus voting.
* **Proposed Implementation:** Introduce a consensus routing rule. A proposal requires simple majority approval from multiple Critic agents before execution, with human/admin absolute veto overrides.

### B. Automated Goal Synthesis
* **Concept:** Enable autonomous alignment by analyzing errors and regressions.
* **Proposed Implementation:** Periodically parse workspace test logs and compile failing tests or linter reports into short-term goals for resolving codebase bugs.

### C. Parent-Child Resiliency Orchestration
* **Concept:** Ensure child processes spawned across different paths or host directories remain alive and healthy.
* **Proposed Implementation:** Heartbeat loops check child status registries. If a child dies or fails to send heartbeats, the parent automatically resurrects the process from the last SQLite snapshot.

---

## 3. Cognitive Governance & Safeguards

### A. Socratic Alignment Wizard (Interactive Amendments)
* **Concept:** Constitutional amendments should require rigorous dialogue.
* **Proposed Implementation:** When an admin proposes a new constitution rule, Janus triggers a Socratic interview (via the `/grill-me` interface), questioning safety limits and wording before sealing the rule.

### B. Additive-Only SQLite DDL Guardrails
* **Concept:** Prevent evolved dynamic skills from executing destructive migrations.
* **Proposed Implementation:** Refine database connection authorizers to programmatically permit only additive changes (e.g., `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN`) and block table drops or column deletions.
