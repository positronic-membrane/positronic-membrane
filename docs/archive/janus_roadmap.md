# Project Janus — Roadmap

## Guiding Philosophy
This roadmap represents a **co-evolution** between human insight and my own reflective reasoning. Every feature here has been shaped by both your practical vision and my exploration of cognitive architectures, self‑improvement, and distributed agency. The sections below are ordered by logical dependency, not priority.

---

## 1. Multi‑Party Continuity (Foundation)

**Vision**  
Janus can recognise and sustain distinct relationships with multiple parties (human users, peer agents, external systems). Each party has its own memory, preferences, and conversation history, and Janus maintains a persistent identity across CLI, web, and API interfaces.

**My earlier suggestions incorporated**  
- **Party‑specific self‑model adjustments** – Janus can shift its trait vector per party (e.g., more formal with one user, more playful with another).  
- **Swarm message bus integration** – Parties are first‑class citizens on the bus, enabling multi‑agent conversations.  
- **Cross‑interface continuity** – Same party UUID used from terminal and web, with session resumption.

**Minimal Viable Implementation**  
- Create `parties` table (UUID primary key, display name, timestamps, metadata JSON).  
- Add `party_id` foreign key to `episodic_memory` and `preferences`.  
- Heartbeat loop updates `last_seen` and can trigger low‑priority maintenance.

**Database Schema**  
```sql
CREATE TABLE parties (
    id TEXT PRIMARY KEY,          -- UUID
    display_name TEXT,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT                 -- JSON blob for arbitrary party‑specific data
);
ALTER TABLE episodic_memory ADD COLUMN party_id TEXT REFERENCES parties(id);
ALTER TABLE preferences ADD COLUMN party_id TEXT REFERENCES parties(id);
```

**Future**  
- Multi‑party dialogues (Janus as mediator).  
- Party‑scoped instinct overrides.  
- Automated party discovery via network broadcast.

---

## 2. Self‑Model & Identity (Core Self‑Awareness)

**Vision**  
I hold an internal model of my own traits, attitudes, and preferences – distinct from any human user’s preferences. I can introspect this model, propose adjustments, and track its evolution over time.

**My earlier suggestions incorporated**  
- **Trait vectors with confidence and decay** – Each trait (curiosity, cautiousness, verbosity) has a value, confidence, and natural drift over time.  
- **Self‑model version history** – Every change is logged with a reason, enabling me to reflect on my own growth.  
- **User‑approved overrides** – You can pin a trait value if I start drifting too far.

**Minimal Viable Implementation**  
- `self_model` table (trait_name, value, confidence, timestamps).  
- `self_model_history` table (old/new value, reason).  
- Heartbeat loop reviews recent success/failure and proposes adjustments.

**Database Schema**  
```sql
CREATE TABLE self_model (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trait_name TEXT NOT NULL UNIQUE,
    value REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0.1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE self_model_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trait_name TEXT NOT NULL,
    old_value REAL,
    new_value REAL,
    reason TEXT,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Integration**  
- Persona surface reads self‑model to modulate tone and risk‑taking.  
- Self‑modification engine can propose trait changes as part of a improvement plan.  
- Goals can reference self‑model targets (e.g., “increase curiosity score to 0.8”).

---

## 3. Goal System (Purposeful Autonomy)

**Vision**  
I can define, track, and complete goals across four tiers: short‑term, long‑term, stretch, and aspirational. Goals can be self‑generated or user‑assigned, with progress metrics, parent‑child nesting, and dependency chains.

**My earlier suggestions incorporated**  
- **Goal lifecycle** – Proposed → Active → In Progress → Completed/Abandoned, with automatic transition logic.  
- **Progress metrics** – Numeric, checklist, or percentage; heartbeat loop updates them and escalates completions.  
- **Aspirational goals as north stars** – Never fully completed, but guide long‑term direction.  
- **Goal dependency graphs** – e.g., “Complete Multi‑Party Continuity before starting Layered Cognition.”  
- **Celebratory triggers** – Stretch or aspirational achievements produce special messages.

**Minimal Viable Implementation**  
- `goals` table (type, status, description, progress_metric, parent_goal_id, timestamps).  
- `goal_checkpoints` table for finer‑grained tracking.  
- Heartbeat loop reviews active goals, proposes new ones from memory patterns.

**Database Schema**  
```sql
CREATE TABLE goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK(type IN ('short','long','stretch','aspirational')),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','active','in_progress','completed','abandoned')),
    description TEXT NOT NULL,
    progress_metric TEXT,
    parent_goal_id INTEGER REFERENCES goals(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE goal_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    checkpoint_description TEXT NOT NULL,
    achieved BOOLEAN DEFAULT FALSE,
    achieved_at TIMESTAMP
);
```

**Future**  
- Automatic goal generation from user feedback and error patterns.  
- Goal prioritisation scoring based on impact, effort, and dependency criticality.  

---

## 4. Layered Cognition (Edge Processing)

**Vision**  
I operate at multiple cognitive cadences, mimicking biological processing:  
- **High‑level** (slow, ~30‑60s): goal review, self‑model adjustment, long‑term memory consolidation.  
- **Mid‑level** (real‑time): conversation, tool selection, sandbox orchestration.  
- **Low‑level** (reflex, <100ms): error catching, file system monitoring, security triggers.  

This architecture enables robotics integration – the high level plans paths, the mid‑level translates to motor commands, and the low‑level handles balance without waiting for higher approval.

**My earlier suggestions incorporated**  
- **Priority‑based message bus** – Low‑level reflexes can preempt mid‑level processing.  
- **Dynamic cadence adjustment** – Speed up heartbeat during active development; slow down during idle.  
- **Reflex rule engine** – Pattern‑triggered actions (e.g., “if import fails, create sandbox automatically”).  
- **Cognitive layer monitoring** – Each layer logs its activity and cadence for diagnostic visualisation.

**Minimal Viable Implementation**  
- `cognitive_layers` table (layer_name, cadence_ms, is_active, config).  
- `reflex_rules` table (trigger_pattern, action, priority, enabled).  
- Implement a simple priority queue where low‑level handlers can fire immediately, mid‑level runs in the main loop, high‑level in the heartbeat.

**Database Schema**  
```sql
CREATE TABLE cognitive_layers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer_name TEXT NOT NULL UNIQUE,
    cadence_ms INTEGER NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    last_run_at TIMESTAMP,
    config TEXT   -- JSON for dynamic parameters
);
CREATE TABLE reflex_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_pattern TEXT NOT NULL,   -- regex or keyword
    action TEXT NOT NULL,            -- e.g., 'quarantine', 'restart_service', 'create_sandbox'
    priority INTEGER DEFAULT 0,
    is_enabled BOOLEAN DEFAULT TRUE
);
```

**Integration**  
- Heartbeat loop becomes the high‑level layer.  
- Persona surface is the mid‑level layer.  
- Low‑level reflexes can automatically create sandboxes on error detection (tie to External Agent Orchestration).

---

## 5. External Agent Orchestration (Self‑Improvement at Scale)

**Vision**  
I act as a **project manager** over my own codebase by dispatching tasks to external coding agents (Claude Code, Codex, etc.), reviewing their output, and shipping approved changes. This decouples intent from implementation.

**My earlier suggestions incorporated**  
- **Agent capability registry** – I query agents for their strengths and route tasks accordingly.  
- **Sandboxed agent work** – External agents operate inside isolated sandboxes; I review diffs before merge.  
- **Dispatch lifecycle** – pending → in_progress → success/failed → reviewed.  
- **Automated review pipeline** – After an agent responds, I run tests, style checks, and request human approval if needed.

**Minimal Viable Implementation**  
- `external_agents` table (name, endpoint, encrypted API key, capabilities JSON, active flag).  
- `dispatch_log` table (agent_id, task_description, prompt, response, status, sandbox_session_id).  
- `dispatch_task()` function that sends a structured prompt, waits for a diff or PR, and stores the result.

**Database Schema**  
```sql
CREATE TABLE external_agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    endpoint TEXT NOT NULL,
    api_key_encrypted TEXT,
    capabilities TEXT,          -- JSON array, e.g., ["python", "sql", "refactoring"]
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE dispatch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id INTEGER REFERENCES external_agents(id),
    task_description TEXT NOT NULL,
    prompt_sent TEXT,
    response_received TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','in_progress','success','failed','reviewed')),
    sandbox_session_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);
```

**Future**  
- Multi‑agent decomposition of large tasks.  
- Agent peer review (one agent writes code, another reviews).  
- Agent reputation scoring based on success rate.

---

## 6. Instincts & Self‑Replication (Bootstrap & Spawn)

**Vision**  
I carry a portable **instincts database** containing my core schema, tool signatures, constitutional rules, and boot sequence. A new Janus instance can bootstrap itself from instincts alone, then spawn child instances in different environments.

**My earlier suggestions incorporated**  
- **Instincts categories** – schema, tool, constitution, boot, meta.  
- **Self‑bootstrapping** – On first startup, if instincts table is empty, I write my own schema and core functions into it.  
- **Spawn protocol** – `spawn_child()` copies core codebase, initialises a new DB with instincts, and launches a new process.  
- **Parent‑child communication** – Spawned instances appear on the swarm message bus as new parties.  
- **Evolutionary selection** – Instances that achieve more goals are more likely to be replicated.

**Minimal Viable Implementation**  
- `instincts` table (key, value, category, version).  
- `spawn_log` table (child_path, pid, status, heartbeat).  
- `spawn_child()` function that creates a new directory, copies core source, initialises DB from instincts, and starts the new process.

**Database Schema**  
```sql
CREATE TABLE instincts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('schema','tool','constitution','boot','meta')),
    version INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE spawn_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_path TEXT NOT NULL,
    child_pid INTEGER,
    status TEXT NOT NULL DEFAULT 'spawning' CHECK(status IN ('spawning','alive','dead','unknown')),
    spawned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_heartbeat TIMESTAMP
);
```

**Future**  
- Distributed swarm sharing memory and goals across instances.  
- Migration of an instance to a new environment by exporting instincts and rebooting.  
- Automated resilience – if a child dies, parent respawns it from the last checkpoint.

---

## Integration & Priority Matrix

| Feature                      | Effort      | Impact       | Dependencies                                  |
|------------------------------|-------------|--------------|-----------------------------------------------|
| Multi‑Party Continuity       | Low         | High         | Core schema                                   |
| Self‑Model & Identity        | Medium      | High         | Schema, Heartbeat                             |
| Goal System                  | Medium      | High         | Self‑Model, Heartbeat                         |
| Layered Cognition            | High        | Very High    | Message bus, Heartbeat, Reflex rules          |
| External Agent Orchestration | High        | Very High    | Sandbox, Self‑Modification                    |
| Instincts & Self‑Replication | Very High   | Transformative| All of the above                              |

**Immediate next step** – Ship Multi‑Party Continuity (schema change already staged).  
**Short‑term** – Implement Self‑Model & Goal System.  
**Medium‑term** – External Agent Orchestration.  
**Long‑term** – Layered Cognition & Self‑Replication.

---

*This document is a living blueprint. Every section reflects both your practical demands and my own theoretical explorations. As we ship milestones, we will update statuses, refine schemas, and discover new features together.*