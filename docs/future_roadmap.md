# Project Janus — Future Roadmap & Advanced Enhancements

This document outlines the advanced architectural enhancements, security upgrades, and swarm capabilities identified during the design phases. These items represent the next evolutionary steps for Project Janus as it transitions from a local development swarm to a secure, cloud-compatible cognitive network.

---

## 0. Pre-Cloud Deployment Prerequisites (Critical Gates) [COMPLETED]

Before exposing Janus on public cloud environments or scaling to production, the following roadmap items **must** be addressed to ensure system safety, cost control, and stability:

> [!NOTE]
> ### 1. GitHub Integration & Pull-Request Gates (Section 1.D) [COMPLETED]
> Gating codebase edits behind **GitHub Pull Requests** ensures a human developer acts as the final merging authority, and automated CI tests run in isolation.
> 
> ### 2. Sandbox Network Policies (Section 1.E) [COMPLETED]
> Code sandboxes have restricted outbound network options (e.g. `--network none` in Docker runtimes) to prevent secret key exfiltration or malicious downloads.
> 
> ### 3. Context Window Management & Memory Decay (Section 2.D) [COMPLETED]
> An automated episodic memory compression scheduler synthesizes old logs and trims detail tables to protect context windows during 24/7 background loops.

---

## 1. Advanced Security & Cloud-Native Sandboxing

### A. Cryptographic Session Security [COMPLETED - Phase I]
* **Concept:** Transition from simple plain-text `X-Party-ID` header checks to cryptographic authentication using short-lived asymmetric RS256 JWTs.

### B. Cloud-Native Sandbox Isolation [COMPLETED - Phase III]
* **Concept:** Support pluggable execution environments, allowing test runs and dynamic code to run inside ephemeral Docker containers or E2B MicroVMs instead of the host command line.

### C. Cost Auditing & Token Quotas
* **Concept:** Prevent runaway API bills in multi-user/multi-tenant settings.
* **Proposed Implementation:** Enforce billing caps or token limits per Party and Session. The core middleware intercepts LLM API calls and blocks them if the quota is exceeded.

### D. GitHub Integration & PR-Driven Development [COMPLETED]
* **Concept:** Gate all codebase edits behind established software review interfaces.
* **Proposed Implementation:** Instead of editing files directly on disk, the Sandbox Executor pushes feature branches to GitHub and opens a Pull Request. Janus listens to PR review comment webhooks to iteratively improve code and waits for a human merge before pulling changes back to its host directory.

### E. Sandbox Network Policies [COMPLETED]
* **Concept:** Prevent data exfiltration or malware downloads during untrusted sandbox test executions.
* **Proposed Implementation:** Wrap container execution runtimes with firewall / network namespace rules whitelisting specific package hosts (e.g. `pypi.org`, `registry.npmjs.org`) and blocking all other outbound TCP connections.

---

## 2. Swarm Concurrency & Lifecycle Enhancements

### A. Parallel Task Execution & Release Branching
* **Concept:** Enable Janus to work on multiple enhancements or debug files concurrently.
* **Proposed Implementation:** Introduce parallel sandbox provisioning. The proposer forks multiple separate git branches (e.g., `feature-A`, `feature-B`) concurrently. When complete, a dedicated Release Coordinator agent merges branches in a consolidation sandbox and resolves merge conflicts.

### B. Multi-Project Workspaces (Blank Sandboxes)
* **Concept:** Allow Janus to build new software applications from scratch rather than just modifying its own code.
* **Proposed Implementation:** Provide a "Blank Sandbox" workspace type. The Sandbox Executor initializes a clean directory, runs boilerplate generators (e.g. `create-next-app`), and lets the agent build, test, and package the application before serving it via a Vercel/Netlify link.

### C. Parent-Child Resiliency Orchestration [COMPLETED - Phase III Database Isolation]
* **Concept:** Support running child agents isolated inside PostgreSQL database schemas (via search path routing) and configure launcher runtimes (ECS tasks or Docker containers) to ensure child containers remain healthy.

### D. Context Window Management & Memory Decay [COMPLETED]
* **Concept:** Prevent context bloating and model degradation in persistent loops.
* **Proposed Implementation:** Run a background Archivist loop. Every $N$ ticks, the Archivist compresses episodic records into high-level Primary Concept summaries, clears detailed memory tables, and stores the summaries in vector long-term memory.

---

## 3. Cognitive Governance & Safeguards

### A. Skills Marketplace & Registry
* **Concept:** Establish a marketplace where Janus instances can browse and install new capabilities.
* **Proposed Implementation:** Build a public skill registry API containing verified skill JSON definitions. Janus instances can query the marketplace, download skills, run AST audits on code blobs, and install them into their `agent_skills` table.

### B. Instinct Starter Pack Profiles
* **Concept:** Instantly specialize Janus instances into different roles (Coder, Researcher, Ops).
* **Proposed Implementation:** Create predefined seed configurations. Choosing a pack populates the database rules, prompts, and skills matching the specialty (e.g., researcher pack seeds crawler/arXiv skills).

### C. Multi-Agent Deadlock & Dispute Resolution
* **Concept:** Resolve infinite loops when the Proposer and Critic disagree on execution safety.
* **Proposed Implementation:** If a proposed action fails the Critic audit $N$ times, pause loop execution, write the agent debate transcript to the database, and escalate to a human reviewer via the `/grill-me` interface.

### D. Additive-Only Database DDL Guardrails [COMPLETED - Phase II]
* **Concept:** Prevent dynamic skills from dropping tables. Programmatically block destructive SQL keywords in cursor wrappers and database roles.
