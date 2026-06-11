# V1 Foundations Roadmap

## Overview

This document defines the remaining technical foundations required to evolve Project Janus from a functional prototype into a fully autonomous, self-sustaining version 1 system. Each foundation addresses a critical gap between the current state and a system that can operate, learn, and improve without constant manual intervention.

---

## Priority Matrix

| # | Foundation | Priority | Effort | Dependencies |
|---|------------|----------|--------|--------------|
| 1 | Memory Hydration Layer | P0 | Low | Self-model, Episodic Memory, Primary Concepts |
| 2 | Smart Loop Governor | P0 | Medium | Daemon (src/daemon.py), Loop Safety Valve |
| 3 | Regression Watcher | P1 | Medium | Sandbox Ship Flow (src/sandbox_session.py), Pytest |
| 4 | Skill Factory | P1 | Medium | Sandbox Archetypes (roadmap P0), Skills Registry |
| 5 | Prompt Versioning | P2 | Low | Configuration Database, Agent Registry |
| 6 | Circuit Breaker / Graceful Degradation | P2 | Low | LLM Client, Skill Executor |

---

## Foundation Details

### 1. Memory Hydration Layer (P0)

**What it is:** On daemon startup or new conversation, automatically load the most relevant primary concepts, recent episodic memories, and current self-model traits into the active context window. This ensures continuity between sessions.

**Ship target:** Database tables (memory_orchestrator, self_model)

**Required extensions:**
- A `hydrate_context()` function in `src/persona.py` or a new `src/memory_hydration.py` that queries the top-N primary concepts (by recency/relevance), last M episodic memories, and current self-model traits.
- Integration with the daemon heartbeat so hydration runs on initialisation, not on every cycle.
- Load the hydrated context into the system prompt for the LLM.

**Key considerations:**
- Context window limits: must compress or summarise if the combined memories exceed token budget.
- Should be idempotent — re-hydrating mid-session should not duplicate content.

---

### 2. Smart Loop Governor (P0)

**What it is:** Replace the current loop safety valve (hard limit of 5 consecutive background loops) with a governor that distinguishes between productive work and true deadlock/stuck states. Autonomous goal pursuit should stay active as long as progress is being made.

**Ship target:** Daemon / Loop safety valve module

**Required extensions:**
- Add a progress metric to the daemon loop (e.g., number of files modified, skills successfully executed, goals advanced).
- Only trigger the safety valve if no progress is detected over N consecutive cycles.
- Expose loop state via the Web API for monitoring.

**Key considerations:**
- Must prevent infinite resource consumption: cap total cycles per daemon session even with progress.
- Should log a narrative summary when the loop is paused so the user understands why.

---

### 3. Regression Watcher (P1)

**What it is:** After every sandbox ship event, automatically run the full test suite. If coverage drops below a threshold or any tests fail, roll back the change and alert the user with a summary of the regression.

**Ship target:** Sandbox ship flow (src/sandbox_session.py)

**Required extensions:**
- Hook into `ship_sandbox_session()` to invoke `run_sandbox_tests()` on the merged workspace.
- Compare test results against a baseline (stored in the database).
- On failure, revert the merge and roll back the git branch.
- Send a structured alert (WebSocket or chat) with the failing test names and error snippets.

**Key considerations:**
- Baseline must be recomputed periodically (e.g., every major release).
- False positives from flaky tests must be handled gracefully (retry once).

---

### 4. Skill Factory (P1)

**What it is:** A sandbox archetype that allows the system to scaffold, test, register, and deploy new skills autonomously. This is the first step toward genuine self-modification.

**Ship target:** Sandbox archetype (roadmap P0: Skills → Database Records)

**Required extensions:**
- A template engine that generates a skill class stub with proper signatures, permissions, and descriptions.
- Isolation in a sandbox so the new skill can be executed without affecting production.
- On approval (via test pass + human or automated sign-off), write the skill record to the database and reload the skill registry.

**Key considerations:**
- Security: new skills must be AST-audited and permission-scoped before registration.
- Must not allow creation of duplicate or conflicting skills.

---

### 5. Prompt Versioning (P2)

**What it is:** A versioned registry for persona prompts, system instructions, and agent definitions. Enables A/B testing of different configurations and safe rollback if a change degrades response quality.

**Ship target:** Database (new table or extension to configuration store)

**Required extensions:**
- Add a `prompt_versions` table with columns for version_id, content, timestamp, and active flag.
- Expose API endpoints to activate a specific version.
- Integrate with sandbox sessions so prompt changes can be tested before activation (e.g., use sandbox use case P5: Configuration/Prompt Experimentation).

**Key considerations:**
- Prompts that reference dynamic data (e.g., memory hydration) must be versioned separately from static instructions.
- Rollback must be instantaneous (flip the active flag).

---

### 6. Circuit Breaker / Graceful Degradation (P2)

**What it is:** Implement retry with exponential backoff for transient failures (e.g., LLM API timeouts). When failures persist, fall back to cached responses for non-critical queries. Escalate unresolved errors to a human with full context.

**Ship target:** LLM client (src/llm.py) and Skill Executor (src/skills.py)

**Required extensions:**
- Wrap API calls in a retry decorator with configurable backoff (e.g., `tenacity` or custom).
- Cache successful responses in the database with TTL.
- Define a fail-open strategy: critical path queries block and escalate; non-critical queries serve cached data.
- Add a circuit state endpoint to the Web API for monitoring.

**Key considerations:**
- Cache invalidation: stale responses must not be served indefinitely.
- Escalation should include the full error trace, input context, and suggested next steps.

---

## Recommended Implementation Order

1. **Memory Hydration Layer** — shortest path to session continuity (2–3 days)
2. **Smart Loop Governor** — unlocks true autonomy (3–5 days)
3. **Regression Watcher** — ensures quality while the system evolves (3–5 days)
4. **Skill Factory** — enables self-modification, must come after regression safety (5–7 days)
5. **Prompt Versioning** — fine-tunes behaviour after the core loop is stable (2–3 days)
6. **Circuit Breaker** — hardening for production readiness (2–3 days)

**Total estimated effort:** 17–26 days for a solo developer.

---

*This roadmap was generated by Project Janus (Journey) on 2025-03-22.*
