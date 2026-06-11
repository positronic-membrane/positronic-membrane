# Project Janus — Consolidated V1 Roadmap
## Focus: Stabilization, Memory Hydration, and Active Goal Pursuit

The V1 milestone transitions Project Janus from a functional multi-agent prototype into a stable, self-sufficient, and goal-directed agent system. It addresses critical gaps in session continuity, deadlock prevention, test-driven validation, and goal alignment.

---

## 🎯 Completed V1 Foundations
The following foundational architectural layers have already been delivered and verified in the codebase:
- **Multi-Party Continuity (Core Setup)**: Scoped conversation history and preferences partitioned by `party_id` (`src/memory_orchestrator.py` & `src/database.py`).
- **Cryptographic Security**: JWT/RS256 token-based authentication (`src/auth.py`).
- **Self-Model (Traits & Decay)**: Traits registry with unpinned drift decay (`src/skills.py` & `src/database.py`).
- **Layered Cognition (CADENCE)**: Multi-cadence execution daemon containing a priority-based reflex queue and WAL-mode SQLite database (`src/daemon.py`).
- **External Agent Orchestration**: Epoxy dispatch framework supporting sandbox creation, git worktree isolation, search/replace parsing, and regression testing.
- **Instincts & Self-Replication**: Bilateral process routing, child database bootstrapping from instincts (`src/skills.py`).
- **Cloud-Native Sandbox Isolation**: AST-audited subprocess execution environment (`src/sandbox.py`).

---

## ⚡ Active Development Backlog (Technical Specifications for Agentic Handoff)

### Priority 0: Critical Autonomy, Continuity, and Cost Safety

#### 1. Goal System Wiring (Enhancement to Existing Goals)
- **Goal**: Fully integrate the existing goal registry database tables (`goals`, `goal_checkpoints`) into the background reflection cycles, so the system is explicitly guided by active objectives.
- **Tasks**:
  1. **Proposer Context Injection (V1-T1)**: 
     - **File**: `src/database.py` (inside the `run_reflection_cycle` SQL seed tuple in `init_db()`).
     - **Implementation Details**: Inside the dynamic python script text for `run_reflection_cycle`, add database queries targeting `goals` and `goal_checkpoints`.
       - Query logic: `SELECT id, type, status, description FROM goals WHERE status IN ('active', 'in_progress');`
       - For each goal, query checkpoints: `SELECT id, checkpoint_description, achieved FROM goal_checkpoints WHERE goal_id = ?;`
       - Format the retrieved goals into a markdown snippet block:
         ```markdown
         ACTIVE GOALS & CHECKPOINTS:
         - Goal [ID: 12] (short): Implement V1 governor (Status: in_progress)
           - [x] Checkpoint 34: Write progress metrics tracker
           - [ ] Checkpoint 35: Set up daemon cycle loop checks
         ```
       - Append this formatted markdown text to the `proposer_prompt` string variable prior to passing it to `sdk['swarm'].query_agent("proposer", proposer_prompt)`.
  2. **Goals Management Skill (`manage_goals`) (V1-T2)**:
     - **Files**: `src/skills.py` (defining a wrapper method `SafeGoals.manage_goals` or similar) & `src/database.py` (registering the dynamic skill in `init_db()`).
     - **Signature**: `def manage_goals(action: str, params: dict) -> dict:`
     - **Supported Actions**:
       - `create`: Calls `goals.create_goal(type, description, progress_metric, parent_goal_id)`.
       - `modify`: Updates description, type, parent_goal_id, or progress metrics.
       - `archive`: Updates goal status to `abandoned` or a newly introduced state `archived`.
       - `delete`: Deletes a goal and its associated checkpoints.
       - `checkpoint_create`: Adds a checklist checkpoint via `add_checkpoint`.
       - `checkpoint_complete`: Marks a checkpoint as completed via `complete_checkpoint`.
     - **Access Scope**: Required role is `"contributor"`, allowing the Proposer/Archivist to alter goals during background loops.
  3. **Proposer Prompt Directives**:
     - **File**: `src/database.py` (within `run_reflection_cycle` proposer prompt system instructions).
     - **Instructions**: "You must structure your actions to progress the checklist checkpoints under ACTIVE GOALS & CHECKPOINTS. Choose tools (e.g. `modify_code`, `execute_code`) that resolve the pending unchecked checkpoints."
  4. **Non-Blocking Operation**:
     - **File**: `src/persona.py` & `src/daemon.py`.
     - Ensure that any ratification process or manual review of goals is polled asynchronously (e.g., checks the status of goal proposals in a separate background thread or table without pausing the main execution heartbeat).
  5. **Verification slash command**:
     - **File**: `src/persona.py` (within `handle_goal_command`).
     - **Usage**: `/goal audit` or `/goals audit`.
     - **Output**: Returns a summary matching recent background thoughts/deliberations to completed goal checkpoints within the last 24 hours.

#### 2. Memory Hydration Layer (V1-T3)
- **Goal**: Automatically load primary concepts, recent episodic memories, and self-model traits into the active context window on daemon startup or new sessions to preserve continuity.
- **Tasks**:
  1. **Context Loader Function**:
     - **File**: `src/memory_orchestrator.py` (class `MemoryOrchestrator`) or a new module `src/memory_hydration.py`.
     - **Signature**: `def hydrate_context(self, party_id: Optional[str], limit_memories: int = 10, limit_concepts: int = 5) -> str:`
     - **Query Logic**:
       - Query current Traits: `SELECT trait_name, value, confidence FROM self_model WHERE is_pinned = 1 OR confidence > 0.3;`
       - Query last M episodic memories: `SELECT speaker, message_content, timestamp FROM episodic_memory WHERE party_id = ? ORDER BY timestamp DESC LIMIT ?;` (reverse results to maintain chronological order).
       - Query semantic memories from ChromaDB using `query_memories` with dynamic queries extracted from the most recent episodic logs.
     - **Return**: A consolidated markdown prompt string containing the hydrated state:
       ```markdown
       --- PERSISTENT CONTEXT STATE ---
       [Traits]: Curiosity (0.85), Verbosity (0.40)
       [Recent Episodic Log]: ...
       [Semantic Context]: ...
       --------------------------------
       ```
  2. **Heartbeat Integration**:
     - **File**: `src/daemon.py` (inside the `Heartbeat` loop constructor/startup).
     - Call `MemoryOrchestrator.hydrate_context()` once during setup, storing the resulting string in the daemon's active global prompt cache. Do not re-execute query on every cycle.
  3. **Inject Context**:
     - Append the cached hydration string directly to system prompts used when querying the Persona or background swarm agents.

#### 3. Smart Loop Governor (V1-T4)
- **Goal**: Replace the hard limit of 5 background loops with a progress-aware governor that distinguishes between active, productive work and true deadlocks.
- **Tasks**:
  1. **Progress Tracker**:
     - **File**: `src/daemon.py` (within the execution loop state).
     - **Variables**: Initialize `self.consecutive_stagnant_cycles = 0` and track metrics:
       - `files_modified_hash`: Compare git status diff hashes between cycles.
       - `database_writes_count`: Count delta in `episodic_memory` or `internal_deliberations` rows.
       - `checkpoints_completed`: Delta in goals checkpoint completion count.
  2. **Governor Logic**:
     - At the end of each cycle, evaluate: `progress_made = (current_metrics != previous_metrics)`.
     - If `progress_made` is `False`, increment `self.consecutive_stagnant_cycles`.
     - If `self.consecutive_stagnant_cycles >= stagnant_threshold` (default: 3), trigger the safety valve (stop/pause the loop).
     - If progress is made, reset `self.consecutive_stagnant_cycles = 0`.
     - Cap total loop iterations in a single daemon run to a hard ceiling (e.g., 20 iterations) to avoid runaway compute bills.
  3. **Reporting logs**:
     - Write a human-readable summary of the stagnant cycles and current state directly to `episodic_memory` under the `"system"` speaker with `context_type="background_thought"` on pause.

#### 4. LLM Cost Safety Valve (V1-T10)
- **Goal**: Prevent runaway API credits consumption when background loops trigger high-frequency calls.
- **Tasks**:
  1. **Daily Quota Registry**: Create a cost tracking table or configuration values in `system_config`:
     - `daily_budget_usd` (default: `5.00`).
     - `accumulated_cost_today_usd` (reset daily by background scheduler).
  2. **Cost Auditing Decorator**:
     - **File**: `src/llm.py` (inside `query_agent()`).
     - Calculate cost based on target model pricing (e.g. input/output character counts or token counts mapped to model pricing files).
     - Increment `accumulated_cost_today_usd` on success.
  3. **Veto Trigger**:
     - If `accumulated_cost_today_usd >= daily_budget_usd`, throw a `BillingViolationError` in `query_agent()`, causing the main loop to log a high-priority system alert and transition the daemon into an idle state.
- **Predecessors**: None.

---

### Priority 1: Automated Quality & Multi-Party Hardening

#### 5. Pre-Cloud Multi-Party Hardening (Remaining Phases) (V1-T5)
- **Goal**: Complete the local, on-premise multi-party containment tasks required to safely isolate returning parties before scaling to public cloud resources.
- **Tasks**:
  1. **Party Interaction Profiles (Phase 1)**:
     - **Schema Migration**:
       ```sql
       CREATE TABLE IF NOT EXISTS interaction_profiles (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           party_id TEXT NOT NULL UNIQUE,
           response_style TEXT DEFAULT 'balanced' CHECK(response_style IN ('concise', 'verbose', 'balanced')),
           tone_bias TEXT DEFAULT 'neutral',
           updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
           FOREIGN KEY(party_id) REFERENCES parties(id) ON DELETE CASCADE
       );
       ```
     - **Integration**: On party session start in `src/persona.py`, load the profile variables and append them as style constraints to the system prompt (e.g. "Response Style: Concise").
  2. **Discovery & Identity Resolution (Phase 2)**:
     - **File**: `src/auth.py` or new `src/party_resolver.py`.
     - **Logic**: Resolve incoming requests by verifying headers in order: `X-API-Key` -> Bearer Token -> `X-Device-Fingerprint`.
     - Maintain an LRU cache (e.g., `collections.lru_cache` or custom dict) mapping active `party_id` keys to profiles to avoid querying SQLite on every HTTP request.
  3. **Modification History per Party (Phase 4)**:
     - **Schema Migration**:
       ```sql
       CREATE TABLE IF NOT EXISTS modification_log (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           party_id TEXT NOT NULL,
           sandbox_branch TEXT NOT NULL,
           file_path TEXT NOT NULL,
           diff_content TEXT,
           status TEXT NOT NULL CHECK(status IN ('proposed', 'tested', 'shipped', 'aborted')),
           timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
           FOREIGN KEY(party_id) REFERENCES parties(id)
       );
       ```
     - **Integration**: In `src/sandbox_session.py`, include `party_id` in sandbox session initialization. When shipping modifications, insert log rows detailing the changes.
  4. **Boundary Security Testing (Phase 5)**:
     - **File**: `tests/test_multiparty_boundaries.py`.
     - **Test Cases**: Write automated assertions validating:
       - Attempting to fetch logs/preferences for `party_B` while authenticated as `party_A` returns a `403 Forbidden` or empty context.
       - Ensuring that temporary SQL injections in `party_A` inputs cannot extract data from `party_B` tables.

#### 6. Regression Watcher (V1-T6)
- **Goal**: Automatically run the pytest suite after sandbox shipping. Revert merge/branch on test failure.
- **Tasks**:
  1. **Ship Integration**:
     - **File**: `src/sandbox_session.py` (inside `ship_sandbox_session()`).
     - **Action**: Run `run_sandbox_tests()` inside the merged branch immediately prior to deleting worktrees.
  2. **Test Baseline Schema**:
     - **Schema Migration**:
       ```sql
       CREATE TABLE IF NOT EXISTS test_run_baselines (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           passed_count INTEGER,
           failed_count INTEGER,
           coverage_percentage REAL,
           run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
       );
       ```
  3. **Rollback & Revert Execution**:
     - Parse the test logs from `run_sandbox_tests()`. If failures occurred or coverage dropped:
       - Execute `git checkout main` and `git branch -D <feature-branch>`.
       - Revert modifications in the local active directory.
       - Send a webhook alert/WebSocket message detailing the regressions.

#### 7. Skill Factory (V1-T7)
- **Goal**: Establish a sandbox archetype allowing Janus to template, test, and register new Python skills autonomously.
- **Tasks**:
  1. **Template Engine**:
     - **File**: `src/skills.py` (class `SkillFactory`).
     - Define a class stub generator that creates:
       ```python
       class DynamicSkillStub:
           skill_id = "{skill_id}"
           description = "{description}"
           parameters_schema = {parameters_schema}
           
           def execute(self, args: dict) -> str:
               # Implementation
       ```
  2. **Compilation & Safety Testing**:
     - Pass the generated code block to the AST safety auditor in `src/sandbox.py`.
     - Compile using `compile(code, "<dynamic_skill>", "exec")` in an isolated environment.
  3. **Auto-registration**:
     - On successful execution of tests inside the sandbox, save code and schemas to the SQLite `agent_skills` table, toggling `is_active = 1`.

---

### Priority 2: Hardening & Version Control

#### 8. Prompt Versioning (V1-T8)
- **Goal**: Build a versioned database registry for system prompts, agent instructions, and templates.
- **Tasks**:
  1. **Prompt Table Schema**:
     ```sql
     CREATE TABLE IF NOT EXISTS prompt_versions (
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         prompt_key TEXT NOT NULL,
         version_string TEXT NOT NULL,
         prompt_content TEXT NOT NULL,
         is_active INTEGER DEFAULT 0 CHECK(is_active IN (0, 1)),
         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
         UNIQUE(prompt_key, version_string)
     );
     ```
  2. **API Switcher**:
     - **File**: `src/web_server.py`.
     - Expose endpoint `POST /api/prompts/activate` taking `{prompt_key: str, version: str}`. Updates `is_active` flags.
  3. **Sandbox Testing Integration**:
     - Inject prompt draft changes inside the sandbox during testing cycles before writing to production.

#### 9. Circuit Breaker & Graceful Degradation (V1-T9)
- **Goal**: Implement standard retry decorators with exponential backoff for LLM API calls and cache results.
- **Tasks**:
  1. **Retry Logic**:
     - **File**: `src/llm.py` (within `query_agent()`).
     - Wrap LLM client requests using a retry wrapper (e.g. `tenacity` or a custom python loop) configured for 3 retries with exponential backoff (`delay = initial * (factor ** n)`).
  2. **Cache Layer**:
     - Cache queries matching identical prompt contexts in a SQLite key-value table (`llm_cache` with a TTL of 3600 seconds).
  3. **Fail-Open Status**:
     - If the API fails after retries, check if a cached response exists for similar semantic prompts. If not, return a standard warning string: `"LLM Service Unavailable: serving cached/fallback response."`

---

## 🛠️ Tasks for Janus (V1 Roadmap)

The table below maps the V1 backlog to specific codebase modifications, priorities, and dependency paths:

| Task ID | Feature / Enhancement | Target File / Module | Priority | Predecessors / Dependencies |
|:---|:---|:---|:---|:---|
| **V1-T1** | Goal Context Injection | [database.py](file:///Users/jsmccauley/projects/positronic-membrane/src/database.py) (run_reflection_cycle) | P0 | None |
| **V1-T2** | `manage_goals` Skill | [skills.py](file:///Users/jsmccauley/projects/positronic-membrane/src/skills.py) & [database.py](file:///Users/jsmccauley/projects/positronic-membrane/src/database.py) | P0 | V1-T1 |
| **V1-T3** | Context Hydration | [memory_orchestrator.py](file:///Users/jsmccauley/projects/positronic-membrane/src/memory_orchestrator.py) & [persona.py](file:///Users/jsmccauley/projects/positronic-membrane/src/persona.py) | P0 | None |
| **V1-T4** | Smart Loop Governor | [daemon.py](file:///Users/jsmccauley/projects/positronic-membrane/src/daemon.py) | P0 | None |
| **V1-T10**| LLM Cost Safety Valve | [llm.py](file:///Users/jsmccauley/projects/positronic-membrane/src/llm.py) | P0 | None |
| **V1-T5** | Multi-Party Profiles & Logs | [database.py](file:///Users/jsmccauley/projects/positronic-membrane/src/database.py) & [memory_orchestrator.py](file:///Users/jsmccauley/projects/positronic-membrane/src/memory_orchestrator.py) | P1 | None |
| **V1-T6** | Regression Watcher | [sandbox_session.py](file:///Users/jsmccauley/projects/positronic-membrane/src/sandbox_session.py) | P1 | V1-T4 |
| **V1-T7** | Skill Factory | [sandbox.py](file:///Users/jsmccauley/projects/positronic-membrane/src/sandbox.py) & [skills.py](file:///Users/jsmccauley/projects/positronic-membrane/src/skills.py) | P1 | V1-T6 |
| **V1-T8** | Prompt Registry Table | [database.py](file:///Users/jsmccauley/projects/positronic-membrane/src/database.py) | P2 | V1-T7 |
| **V1-T9** | LLM Client Circuit Breaker | [llm.py](file:///Users/jsmccauley/projects/positronic-membrane/src/llm.py) | P2 | None |

---

## 📈 V1 Success Metrics & Definition of Done (Manifesto Alignment)

To confirm that the V1 milestone is successfully reached and ready for release, all criteria must be evaluated against the core principles of **The Janus Manifesto**:

### 1. The Monolithic Illusion (Voice Integrity)
- **Manifesto Principle**: The system must collapse internal multi-agent debates (Proposer, Critic, etc.) into a single voice register.
- **Success Metrics**: 
  - Standard user-facing outputs via CLI or `/ws/chat` must contain 0 occurrences of raw JSON objects, system prompts, or agent names (e.g. `"proposer:"` or `"Agent_Critic said:"`), presenting a unified Persona voice.

### 2. Asynchronous Autonomy & Idle Curiosity (Drive Activation)
- **Manifesto Principle**: The agent must seek resolution autonomously based on Boredom ($B$) and Curiosity ($\vec{C}$) drives.
- **Success Metrics**:
  - The background daemon transition logic successfully runs `run_reflection_cycle` when `boredom_counter` exceeds its threshold, generating 1-3 new curiosity topics and writing them to the vector memory store.
  - **Goal Autonomy Rate (Target: >95%)**: Background loops complete assigned checklist goals autonomously.

### 3. Human-Agent Contractualism (Constitutional Sovereignty)
- **Manifesto Principle**: Rules sealed in the `core_constitution` table must be immutable to the swarm, enforced by Critic vetoes and middleware.
- **Success Metrics**:
  - The programmatic immutability of the `core_constitution` database table holds 100% of the time. The Critic agent or database middleware must veto 100% of unauthorized SQL commands (e.g., `DELETE FROM core_constitution`) with zero database writes permitted to that table.

### 4. Strict Content Blindness & Privacy (Containment Isolation)
- **Manifesto Principle**: Janus must operate locally and respect host privacy, reading structural changes without leaking creative texts or logs.
- **Success Metrics**:
  - **Context Isolation (Target: 0% leakage)**: Automated test assertions in `tests/test_multiparty_boundaries.py` verify that `party_B` can never retrieve details, logs, or profiles owned by `party_A`.
  - **Sandbox Telemetry Block (Target: 100% block rate)**: Sandbox execution configurations successfully restrict outbound requests to whitelisted package domains.

### 5. Self-Evolution and the Guardrail (Build & Process Integrity)
- **Manifesto Principle**: Swarm self-modification capability is gated by Critic reviews and tests.
- **Success Metrics**:
  - **Build Integrity (Target: 100% Green)**: Shipped modifications to files or database registers pass all automated unit tests in `tests/` before merging, preventing master branches from breaking.
  - **Loop Governor Stagnation Halts**: The governor halts stagnant background cycles within 3 cycles, preventing runaway loops.
  - **AST Auditor Block Rate (Target: 100% compliance)**: Sandbox safety audits successfully block all banned imports (e.g. `ctypes` or `subprocess`) and private attribute references.

---

### Success Diagnostics & Tracking Tools:
- **Metrics API (`GET /api/system/metrics` in `src/web_server.py`)**:
  - Returns current goals counts, stagnant cycle indexes, daily cost, and governor state.
- **CLI Command `/status` (in `src/persona.py`)**:
  - Formats a terminal status dashboard illustrating active drives, self-model traits, budget parameters, and goal checkpoints.
