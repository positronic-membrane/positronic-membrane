# Engineering Specification: GEMINI.md Context Anchor
## Version: 1.0
## Classification: Project Memory Vault (Content-Blind)

### 1. Architectural Purpose
The `GEMINI.md` file acts as the persistent, long-term memory system for all asynchronous development agents (e.g., Jules) interacting with this codebase. It forms the authoritative "Context Anchor" that prevents context drift, eliminates the need to repeatedly copy/paste rules into chat boundaries, and insulates the system against hallucinated boilerplate solutions that violate core project constraints.

### 2. Required Root Markdown Schema
Any agent updating this file must strictly maintain the following three structural sections:

#### Section I: Core Constraints & Logic Gates
This section documents the non-negotiable architectural boundaries and performance thresholds of the system.
* **Performance Overhead:** (e.g., "The ingestion daemon must consume less than 1% CPU overhead on modern Apple Silicon chips.")
* **Privacy Posture:** (e.g., "The codebase must adhere to a strict content-blind policy, processing only localized file structure metadata and size deltas without keylogging strings.")
* **Fail-State Boundaries:** (e.g., "In urban fantasy grand strategy templates, visibility hitting 100% triggers an immediate baseline human counter-force event.")

#### Section II: Technical Schema Definitions
This section serves as a deterministic blueprint for database layouts, API payloads, and state mappings. 
* **Relational Assets:** Explicit schemas for local SQLite containers (e.g., projects, sessions, events, metrics tables).
* **NoSQL Graphs:** Document layouts for live NoSQL listeners (e.g., Firestore collections mapping geographic points of interest derived from GIS datasets).

#### Section III: The Current Phased Roadmap
A strict, execution-focused sequence prioritizing immediate technical milestones over long-term goals.
* **Stage 1 Focus:** Clear description of the local, minimal viable prototype.
* **Deferred Stages:** Outlining ledger integrations, cryptographic zero-knowledge proof components, or advanced features so agents do not prematurely over-engineer the active workspace.