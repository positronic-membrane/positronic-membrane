# GEMINI.md Context Anchor

## Section I: Core Constraints & Logic Gates

This section documents the non-negotiable architectural boundaries, performance thresholds, and safety limits of the Project Janus system.

### 1. Performance & Compute Constraints
* **Compute Overhead:** The background heartbeat loop must consume less than 2% average CPU overhead on Apple Silicon chips.
* **Idle Pacing Mechanism:** When no human stimulus is received, the loop must toggle into a "Reflective State," slowing its execution pulse to run once every $T_{\text{idle}}$ minutes (default: 15 minutes) to conserve local compute resources.
* **Multi-Agent Memory Optimization:** The system must run a single local model instance (or query a single remote API endpoint) by default, routing all agent roles (Proposer, Critic, Explorer, Archivist) using system prompt injection to avoid local VRAM swapping latency.
* **Role-Specific Model Routing (Future Extensibility):** The system architecture must support pluggable routing configuration via the `.env` file or `agent_registry` (e.g., overriding specific agent models like `CRITIC_MODEL` or `PROPOSER_MODEL`). This allows routing simple tasks to smaller models (e.g., a 1.5B model for safety auditing) and complex tasks to larger models, which is optimal for high-performance serverless cloud mode or multi-GPU environments.
* **Inference Posture (Dual-Mode API Compliance):** The system must use an OpenAI-compatible API standard client interface supporting two modes configured via a `.env` file:
  * **Local Mode (Offline/Free):** Connects to a local Ollama instance (e.g., `http://localhost:11434/v1` running a model like `qwen2.5-coder:7b`).
  * **Cloud Serverless Mode (Plugged-In/Pay-as-you-go):** Connects to a low-cost, pay-as-you-go serverless open-weights API provider (e.g., DeepSeek, Groq, or OpenRouter) without relying on monthly flat subscriptions.

### 2. Privacy & Security Gates
* **Strict Content Blindness:** The codebase must adhere to a strict content-blind policy, processing only localized file structure metadata and size deltas without keylogging or reading raw creative text strings outside of structural system files and the `GEMINI.md` memory canvas itself.
* **Context Isolation:** Background thoughts (reflections) must be structurally separated in the database from user-driven interactions to prevent reality/hallucination confusion.
* **Credential Safety:** All API keys and secrets must reside in a git-ignored `.env` file. No plain-text credentials in the repository.

### 3. Ethical Alignment & Safety Valves
* **Utilitarian & Contractual Veto:** All autonomous goal formulations and web activities must be filtered through a core ethical matrix. The Critic agent must audit every proposed action by evaluating its systemic utility and vetoing any action that violates rules in the `core_constitution` database table.
* **The Loop Safety Valve:** A hard-coded execution cap must prevent background agents from triggering more than $N$ (default: 5) back-to-back automated loops without validating against the human interface, eliminating runaway compute loops. This loop counter must be tracked in SQLite and enforced by Python middleware rather than the LLM itself.
* **The Non-Disclosure Guardrail:** The Persona model must strip out raw syntax structures, JSON notation, or explicit agent names (e.g., "Agent_Critic said...") during standard user conversational outputs, preserving the monolithic illusion.

### 4. Self-Modification & Self-Evolution Guardrails
* **Constitutional Immutability:** Under no circumstances may autonomous agents modify, delete, or overwrite rows in the `core_constitution` database table. The database connection used by the agent swarm must enforce this restriction programmatically, or it must be validated by hard-coded Python middleware intercepting SQL statements.
* **Veto Gate for Swarm Alterations:** Any self-modification proposals—such as registering a new agent, altering an existing agent's prompt, or swapping an active model in `agent_registry`—must be logged in `internal_deliberations` and submitted to the Critic agent for an audit. The Critic must evaluate if the modification introduces cognitive bias, bypasses safety valves, or violates the core constitution.
* **Safe Configuration Mutation:** Agents can only modify configuration values in `system_config` where `is_agent_modifiable = 1`. Any modification to non-modifiable keys must trigger an immediate safety halt.
- **Automated Memory Retention**: The codebase utilizes a polling-based `FileWatcher` (`src/watcher.py`) coupled with a `MemoryOrchestrator` (`src/memory.py`). Any structural or logical modifications to the workspace must be intercepted by this orchestrator to generate point-in-time JSON snapshots in `.janus_snapshots/`, ensuring no contextual drift occurs during asynchronous development cycles.
- **Mocking Namespace Isolation**: All test files must mock classes/functions in the module where they are imported and used, rather than where they are defined (e.g., use `@patch("src.memory.query_agent")` instead of `@patch("src.llm.query_agent")`). The Proposer must write tests using this rule, and the Critic must veto any tests that violate it.

---

## Section II: Technical Schema Definitions

This section serves as a deterministic blueprint for database layouts, API payloads, and state mappings.

### 1. Relational Database Layout (SQLite - WAL Mode enabled)

#### Table: `core_constitution`
Initialized via early socratic dialogue with the user. Read-only by autonomous agents.
```sql
CREATE TABLE core_constitution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_key TEXT UNIQUE NOT NULL,
    rule_text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### Table: `internal_deliberations`
Holds the audit trail of internal agent discussions and the Critic's audits.
```sql
CREATE TABLE internal_deliberations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    proposed_action TEXT NOT NULL,
    agent_debate_json TEXT NOT NULL, -- JSON containing Proposer and Explorer outputs
    critic_decision INTEGER NOT NULL, -- 0 for vetoed, 1 for approved
    utility_score REAL NOT NULL,
    justification TEXT NOT NULL
);
```

#### Table: `episodic_memory`
Stores chronological interaction logs, separating user chats from background thoughts.
```sql
CREATE TABLE episodic_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    speaker TEXT NOT NULL,          -- e.g., 'user', 'persona', 'proposer', 'explorer'
    message_content TEXT NOT NULL,
    context_type TEXT NOT NULL      -- 'user_visible' or 'background_thought'
);
```

#### Table: `drive_state`
Tracks the internal drive state variables.
```sql
CREATE TABLE drive_state (
    boredom_counter INTEGER DEFAULT 0,
    curiosity_vector_json TEXT DEFAULT '[]', -- JSON array of active curiosity keys/topics, starts empty
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### Table: `agent_registry`
Tracks active agents in the swarm, their prompts, and their targeted models.
```sql
CREATE TABLE agent_registry (
    agent_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    target_model TEXT,                      -- Nullable (falls back to global LLM_MODEL)
    is_active INTEGER DEFAULT 1,             -- 0 for disabled, 1 for active
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### Table: `system_config`
Tracks global configuration parameters, isolating human-locked parameters from agent-modifiable variables.
```sql
CREATE TABLE system_config (
    config_key TEXT PRIMARY KEY,
    config_value TEXT NOT NULL,
    is_agent_modifiable INTEGER DEFAULT 1,   -- 0 for strictly human-only, 1 for self-modifiable
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 2. Semantic Memory Layout (ChromaDB / Milvus Lite)
* **Collection Name:** `janus_long_term`
* **Metadata Schema:**
  * `source_id`: Reference to database log ID or source document path.
  * `timestamp`: Unix timestamp of creation.
  * `tags`: Array of semantic category tags.
  * `relevance_score`: Metric tracking relevance to core constitution goals.

---

## Section III: The Current Phased Roadmap

This execution-focused roadmap outlines immediate technical milestones, preventing premature over-engineering of the active workspace.

### Stage 1: Local Daemon & Database Initialization (MVP)
* **Goal:** Create directory structures, initialize the SQLite database with WAL mode, and write the core background heartbeat daemon.
* **Tasks:**
  * Set up database schemas and populate initial configuration parameters.
  * Implement a first-run Socratic Setup CLI wizard to conduct the user-agent alignment interview and write agreed-upon rules to the read-only `core_constitution` table.
  * Implement the Python `asyncio` heartbeat daemon pacing mechanism ($T_{\text{idle}}$ changes between user-presence and idle modes).
  * Build the drive state machine incrementing Boredom ($B$) and triggering mock executive actions when $B \ge B_{\text{threshold}}$.

### Stage 2: Swarm Routing & Safety Guardrails
* **Goal:** Integrate LLM client interface (Ollama/remote API dual-mode) and run multi-agent prompts.
* **Tasks:**
  * Implement the OpenAI-compliant client with `.env` switching.
  * Write a dynamic agent prompt factory that reads system prompts from `agent_registry` and resolves targeted models.
  * Build the hard-coded Python middleware safety valve: intercept actions, validate self-modification/config writes, audit `core_constitution` rules, and enforce loop safety valve $N = 5$.

### Stage 3: Vector Memory & Explorer Web Fetching
* **Goal:** Connect long-term memory and allow safe background research.
* **Tasks:**
  * Integrate ChromaDB/Milvus Lite for long-term semantic storage.
  * Hook up the Explorer agent to search and parse restricted domains.
  * Implement self-generating Curiosity Vector updates based on background reflection logs (starting with an empty vector).

### Stage 4: Persona Surface & Metacognitive Auditing
* **Goal:** Build the single-voice front-end interface and audit trail.
* **Tasks:**
  * Build a front-end interface (command line or simple UI) serving the unified Persona voice.
  * Implement metacognitive query handler to retrieve and narrate details from `internal_deliberations`.