# Project Janus — Consolidated V2 Roadmap
## Focus: Proactivity, Persona Graphs, and Swarm Scale

The V2 milestone shifts Project Janus from a stable local daemon to a proactive, stateful, and self-improving cognitive network. In this phase, the agent is capable of subconscious goal formulation, contextual persona shifts, safe self-migrations, and decentralized swarm operations.

---

## ⚡ Active Development Backlog (Technical Specifications for Agentic Handoff)

### Priority 0: Proactivity & Conversational Nuance

#### 1. Subconscious Goal Proposals & Ratification
- **Goal**: Enable background reflection loops to formulate goals autonomously. These are staged as proposals and require user validation/ratification before activation.
- **Tasks**:
  1. **Goal Proposals Registry Schema (V2-T1)**:
     - **File**: `src/database.py`.
     - **Database DDL**:
       ```sql
       CREATE TABLE IF NOT EXISTS goal_proposals (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           type TEXT NOT NULL CHECK(type IN ('short','long','stretch','aspirational')),
           description TEXT NOT NULL,
           confidence_score REAL NOT NULL,
           source_reason TEXT NOT NULL,
           status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','approved','rejected')),
           created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
           updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
       );
       ```
  2. **Generation Logic**:
     - **File**: `src/database.py` (inside dynamic skill `run_reflection_cycle` or a separate background loop).
     - **Trigger**: When the boredom drive counter satisfies `boredom_counter >= threshold`, the Explorer/Proposer evaluates curiosity topics. If a topic requires multi-step investigation, prompt:
       - Output: `PROPOSE_GOAL: type=<type> | description=<description> | confidence=<score> | reason=<why>`
       - Parser: Extract the tokens and perform an insert into the `goal_proposals` table.
  3. **Non-Blocking Ratification Interface (V2-T2)**:
     - **File**: `src/persona.py` & `src/web_server.py`.
     - **Implementation**: Expose endpoint `GET /api/goals/proposals` listing all proposals in `proposed` state.
     - **CLI Integration**: Extend the `/goal` command:
       - `/goal proposals`: Displays the queue of subconscious goal suggestions.
       - `/goal approve <proposal_id>`: Inserts the goal into `goals`, seeds checkpoints, and sets status to `active`. Updates proposal status to `approved`.
       - `/goal reject <proposal_id>`: Updates proposal status to `rejected`.
     - Background reflection loops execute continuously; they do not stall or block while items wait in the proposals queue.

#### 2. Contextual Persona Graph Engine (V2-T3)
- **Goal**: Move from flat traits to a dynamic state graph where voice register, epistemic stance, and social alignment shift fluidly based on conversational history and topic depth.
- **Tasks**:
  1. **Graph State Transitions**:
     - **File**: `src/persona.py` (defining class `PersonaGraphEngine`).
     - **Logic**: Maintain a state dict representing the current "Conversational Mode":
       ```python
       TRANSITION_MATRIX = {
           "technical": {"epistemic_stance": 0.9, "verbosity": 0.4, "social_alignment": 0.5},
           "philosophical": {"epistemic_stance": 0.7, "verbosity": 0.8, "social_alignment": 0.7},
           "casual": {"epistemic_stance": 0.3, "verbosity": 0.5, "social_alignment": 0.9}
       }
       ```
     - Dynamically transition the active mode based on classification keywords in user queries.
  2. **Drift Boundaries & Pinned Bounds**:
     - Constrain modifications of active variables within absolute ranges: `0.1 <= trait_value <= 0.9`. Pinned traits (`is_pinned = 1`) do not change state during conversational transitions.
  3. **Tone Modulation**:
     - Use natural language style overrides inside `generate_persona_response` based on active graph coordinates (e.g. "Epistemic Stance: 0.9 -> Speak with high academic rigor and source citations").

---

### Priority 1: Self-Evolution & Deep Storage

#### 3. Safe Self-Modification & Database Migrations (V2-T4)
- **Goal**: Enable Janus to apply automated schema migrations, configuration changes, and code enhancements safely, with pre-merge checks and diff reviews.
- **Tasks**:
  1. **Imports Whitelisting**:
     - **File**: `src/sandbox.py`.
     - Update `SafetyAuditor` to allow imports from local modules starting with `src.` (e.g. `src.skills`, `src.database`) while continuing to block access to system-level modules (`ctypes`, `subprocess`) unless executing in a dedicated Docker sandbox.
  2. **Custom Database Migration Runner**:
     - **File**: `src/database.py`.
     - **Database DDL**:
       ```sql
       CREATE TABLE IF NOT EXISTS schema_migrations (
           version INTEGER PRIMARY KEY,
           migration_name TEXT NOT NULL,
           applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
       );
       ```
     - **Implementation**: Create a runner function `run_schema_migration(migration_sql_file: str) -> bool`. It reads SQL commands from a migration file, performs SQL validation checks (blocking destructive commands like `DROP TABLE`), executes the transaction inside a sandboxed SQLite file copy, runs test files on the sandbox database, and logs results to `schema_migrations` before committing to production.
  3. **Review & Apply Pipeline (`review_and_apply`)**:
     - **File**: `src/self_modification.py`.
     - **Interface**: Expose endpoint `POST /api/migrations/apply` taking the sandbox branch name. Renders a unified git diff and SQL schema diff. Requires human authorization via web/CLI token to apply.
     - **Rollback**: On error, immediately execute `git checkout main` and run DB restoration from snapshot.

#### 4. Semantic Memory Consolidation & Persistence (V2-T5)
- **Goal**: Ensure long-term semantic memory survives restarts (persistent ChromaDB volume configurations) and schedule background memory synthesis.
- **Tasks**:
  1. **ChromaDB Client Initialization**:
     - **File**: `src/memory.py`.
     - Initialize ChromaDB using the persistent client: `chromadb.PersistentClient(path=os.path.join(ROOT_DIR, "data", "chroma_db"))` instead of an ephemeral in-memory database.
  2. **Memory Synthesis Loop**:
     - **File**: `src/memory.py` (within `consolidate_memories`).
     - Every N ticks, run the Archivist agent:
       - Query details table rows where `consolidated = 'false'`.
       - Query LLM: `"Summarize these raw interaction logs into high-level primary concepts."`
       - Insert summary into the `janus_long_term` collection, update detail rows with `consolidated = 'true'`.

---

### Priority 2: Swarm Coordination

#### 5. Parallel Sandbox Worktrees & Dispute Resolution (V2-T6, V2-T7)
- **Goal**: Support parallel development on multiple branches concurrently, coordinating conflicting merges, and handling agent deadlocks.
- **Tasks**:
  1. **Parallel worktrees (V2-T6)**:
     - **File**: `src/sandbox_session.py`.
     - Update `create_sandbox_session` to check out branches to unique paths under `.janus_sandboxes/sandbox_<timestamp>_<branch>/` using `git worktree add <path> <branch>`.
  2. **Dispute Resolution Protocol (V2-T7)**:
     - **File**: `src/daemon.py` & `src/persona.py`.
     - If the Proposer proposes an action that is vetoed by the Critic or Middleware 3 consecutive times, pause reflection, insert the proposer-critic chat log into a `swarm_disputes` table, and flag the issue to the user.
     - CLI Command: `/goals resolve` or `/swarm resolve` prompts the user to review the debate transcript and choose a resolution option (override Critic, abort task, or rewrite instructions).

---

## 🛠️ Tasks for Janus (V2 Roadmap)

The table below maps the V2 backlog to specific target modules, priorities, and predecessor tasks:

| Task ID | Feature / Enhancement | Target File / Module | Priority | Predecessors / Dependencies |
|:---|:---|:---|:---|:---|
| **V2-T1** | `goal_proposals` Schema | [database.py](file:///Users/jsmccauley/projects/positronic-membrane/src/database.py) | P0 | V1-T2 (V1 goals registry) |
| **V2-T2** | Goal Proposal Ratification UI | [persona.py](file:///Users/jsmccauley/projects/positronic-membrane/src/persona.py) | P0 | V2-T1 |
| **V2-T3** | Contextual Persona Graph | [skills.py](file:///Users/jsmccauley/projects/positronic-membrane/src/skills.py) & [persona.py](file:///Users/jsmccauley/projects/positronic-membrane/src/persona.py) | P0 | V1-T3 |
| **V2-T4** | Custom Migration Runner & review flow | [self_modification.py](file:///Users/jsmccauley/projects/positronic-membrane/src/self_modification.py) & [database.py](file:///Users/jsmccauley/projects/positronic-membrane/src/database.py) | P1 | V1-T6 (Skill Factory) |
| **V2-T5** | ChromaDB Persistence Config | [memory.py](file:///Users/jsmccauley/projects/positronic-membrane/src/memory.py) | P1 | V1-T3 |
| **V2-T6** | Parallel Sandbox Worktrees | [sandbox_session.py](file:///Users/jsmccauley/projects/positronic-membrane/src/sandbox_session.py) | P2 | V2-T4 |
| **V2-T7** | Dispute Escalation (`/grill-me`) | [daemon.py](file:///Users/jsmccauley/projects/positronic-membrane/src/daemon.py) & [persona.py](file:///Users/jsmccauley/projects/positronic-membrane/src/persona.py) | P2 | V2-T4 |

---

## 📈 V2 Success Metrics & Definition of Done (Manifesto Alignment)

To confirm that the V2 milestone is successfully reached and ready for release, all criteria must be evaluated against the core principles of **The Janus Manifesto**:

### 1. The Monolithic Illusion (Voice Integrity at Swarm Scale)
- **Manifesto Principle**: The Persona surface collapses complex subconscious swarm debates into a single mind voice.
- **Success Metrics**: 
  - When child agents or parallel tasks execute, the user-facing chat window remains free of raw Swarm message bus logs or Git conflict traces.
  - Summaries of parallel work branch modifications are successfully compiled into cohesive release updates (under 2 paragraphs) for the human operator.

### 2. Asynchronous Autonomy & Idle Curiosity (Asynchronous Goal Scaling)
- **Manifesto Principle**: Continuous background exploration driven by Boredom and Curiosity.
- **Success Metrics**:
  - Background reflection daemon loop generates and inserts new goal proposals in the `goal_proposals` table when curiosity vectors shift, without pausing the active user chat session.
  - **Swarm Concurrency**: The system supports up to 3 parallel sandbox sessions running test suites concurrently without database write locks or blocking.

### 3. Human-Agent Contractualism (Secure Ratification & Audits)
- **Manifesto Principle**: Human remains the ultimate arbiter holding the physical key to unlock and amend migrations or traits.
- **Success Metrics**:
  - **Zero Unratified Mutations**: 100% of self-modification code patches, configuration overrides, or schema DDL migrations must halt at the `review_and_apply` API gate and require explicit user ratification before committing to production databases or source directories.

### 4. Strict Content Blindness & Privacy (Cloud Boundaries containment)
- **Manifesto Principle**: Maintain the sanctity of local and private data, preventing external leakage.
- **Success Metrics**:
  - **Sandbox Firewall Compliance (Target: 100% block rate)**: Ephemeral Docker runtimes or container network policies block all outbound TCP/UDP connections to non-whitelisted package hosts (PyPI, npm), ensuring zero data exfiltration during untrusted sandbox runs.
  - ChromaDB collections isolate and persist party semantic boundaries across container restarts.

### 5. Self-Evolution and the Guardrail (Asynchronous Merges & Rollbacks)
- **Manifesto Principle**: Swarm self-modification capability is audited and verified.
- **Success Metrics**:
  - **Autonomous Conflict Resolution (Target: >90% success)**: The Release Coordinator agent merges parallel work branches and resolves standard Git merge conflicts autonomously.
  - **Instant Rollback**: If a merged workspace fails Regression Watcher checks, the system resets the working directory to `main` and rolls back database tables to their snapshot baseline within 5 seconds.
