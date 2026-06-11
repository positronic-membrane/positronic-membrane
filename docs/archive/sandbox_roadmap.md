# Sandbox Roadmap — Project Janus

**Version:** 1.0  
**Status:** Draft / Planning  
**Last Updated:** 2025-04-06

---

## Overview

This document defines the long-term vision for Janus's sandboxing capabilities. Each sandbox archetype isolates a specific kind of work, provides its own test/validation pipeline, and ships to a distinct target. Priorities are assigned based on architectural readiness and expected value.

---

## Priority Matrix

| Priority | Use Case | Ship Target | Current Readiness | Effort |
|----------|----------|-------------|-------------------|--------|
| **P0** | Skills → Database Records | `janus.db` (skill table) | Medium — needs transaction wrapper | 2–3 days |
| **P0** | Janus Next Version → Branch & Merge | Git `main` branch | High — core flow exists | 1–2 days |
| **P1** | Multi-Agent Behaviour Testing | Agent registry | Medium — spawn_agent exists | 3–4 days |
| **P1** | Configuration / Prompt Experimentation | `.env` / DB | Medium — needs eval harness | 2–3 days |
| **P2** | Data Ingestion Sandbox | Config files / DB | Low — needs preview layer | 4–5 days |
| **P3** | New Application (Separate Repo) | New Git remote | Low — needs repo init flow | 5–7 days |

---

## P0 — Skills → Database Records

### Goal
Allow Janus to create, test, and ship new skills entirely within a sandbox. Skills are defined as Python classes and registered in the database. The sandbox must validate both the code and the database record before shipping.

### Design

1. **Sandbox Constructor** — `create_skill_sandbox(skill_name: str)`
   - Creates a temporary SQLite database copy of the skill schema.
   - Clones the current skill registry into the sandbox DB.
   - Creates a worktree branch for any companion Python files.

2. **Skill Definition** — The user (or Janus) writes a skill class in `skills/<name>.py` following the existing skill interface:
   ```python
   class MyNewSkill:
       skill_id = "my_new_skill"
       description = "Does something useful"
       parameters = {"query": {"type": "string"}}

       def execute(self, args: dict) -> str:
           return f"Executed with {args}"
   ```

3. **Registration** — A helper writes the skill record to the sandbox DB:
   ```sql
   INSERT INTO skills (skill_id, description, parameters_schema, source_file)
   VALUES ('my_new_skill', 'Does something useful', '{"query": {"type": "string"}}', 'skills/my_new_skill.py');
   ```

4. **Testing** — The sandbox runs:
   - Unit tests on the skill class.
   - Integration test: register the skill, invoke it via the skill executor, verify output.
   - Schema validation: ensure parameters match the expected JSON schema.

5. **Ship** — On approval:
   - Copy the `.py` file to the live workspace.
   - Execute the INSERT on the live database.
   - Optionally, run a quick smoke test against the live skill executor.

### Key Files
- `src/skill_executor.py` — needs a `register_skill()` method.
- `src/sandbox_session.py` — needs a new `create_skill_sandbox()` variant.
- `janus.db` — the `skills` table schema.

---

## P0 — Janus Next Version → Branch & Merge

### Goal
Allow Janus to modify its own source code, test the changes, and merge them back into `main` — all while preserving memory continuity.

### Current State
✅ The `create_sandbox_session()` / `ship_sandbox_session()` flow already supports this. The hardening document was successfully shipped using this exact mechanism.

### Remaining Work

1. **Memory Freeze / Replay**
   - Before branching, snapshot the memory store (`janus.db` memory tables) to a JSON archive at `memory/freeze_<timestamp>.json`.
   - After merging, load the snapshot and run consolidation to merge any new memories accumulated during the sandbox session.
   - This prevents context loss when switching branches.

2. **Self-Model Continuity**
   - The self-model (mood, curiosity, reflection state) should also be frozen and replayed.
   - Consider pinning the self-model during sandbox sessions so it doesn't drift while Janus is working on itself.

3. **Sandbox Naming Convention**
   - Enforce a naming pattern: `janus/upgrade-<short-description>`.
   - Auto-generate a changelog entry from the sandbox diff on ship.

### Key Files
- `src/self_model.py` — add freeze/replay methods.
- `src/memory.py` — add snapshot/restore methods.
- `src/sandbox_session.py` — integrate freeze before create, replay after ship.

---

## P1 — Multi-Agent Behaviour Testing

### Goal
Spin up a temporary swarm of agents, run a multi-turn interaction, and evaluate outcomes against defined criteria. Ship successful agent definitions to the live registry.

### Design

1. **Sandbox Constructor** — `create_agent_test_sandbox(agent_defs: list[dict])`
   - Registers the agents in a temporary registry (in-memory or sandbox DB).
   - Creates a test harness that simulates a conversation or task.

2. **Test Scenarios** — Predefined or user-defined:
   - "Does the critic catch a deliberately flawed proposal?"
   - "Does the explorer return relevant results for a given query?"
   - "Does the archivist correctly summarize a code change?"

3. **Evaluation** — The sandbox logs all interactions and scores them:
   - Pass/fail per criterion.
   - Latency and token usage metrics.
   - A summary report.

4. **Ship** — On approval:
   - Register the agents in the live registry via `spawn_agent`.
   - Save the test scenario as a regression test.

### Key Files
- `src/swarm_registry.py` — needs a `register_temporary()` method.
- `src/sandbox_session.py` — new `create_agent_sandbox()` variant.
- `tests/` — regression test suite for agent behaviours.

---

## P1 — Configuration / Prompt Experimentation

### Goal
Iteratively tune Janus's system prompt, persona description, or configuration parameters in an isolated environment, with side-by-side comparison against a baseline.

### Design

1. **Sandbox Constructor** — `create_config_sandbox()`
   - Copies the current `.env` and prompt templates into the sandbox.
   - Allows modification of specific keys (e.g., `PERSONA_STYLE`, `CURIOSITY_THRESHOLD`).

2. **Test Suite** — A standard set of queries is run against both the baseline and the sandbox config:
   - "What is your purpose?" (persona consistency)
   - "Summarize the codebase." (context quality)
   - "What are you curious about?" (curiosity drive)

3. **Comparison Report** — The sandbox generates a diff of responses, highlighting:
   - Tone changes.
   - Length differences.
   - Content quality (subjective, flagged for user review).

4. **Ship** — On approval:
   - Apply the modified config keys to the live `.env` or database.
   - Optionally, save the winning prompt as a named template.

### Key Files
- `src/config.py` — needs a `load_config()` / `apply_config()` interface.
- `src/persona.py` — needs a `get_prompt_template()` method.
- `src/sandbox_session.py` — new `create_config_sandbox()` variant.

---

## P2 — Data Ingestion Sandbox

### Goal
Before pulling an external data source into Janus's context, preview and tune the ingestion pipeline in a sandbox.

### Design

1. **Sandbox Constructor** — `create_ingestion_sandbox(source: str)`
   - Downloads or copies a sample of the data (first N rows, first M bytes).
   - Applies the `ExternalContextLoader` formatting pipeline.

2. **Tunable Parameters** — The user can adjust:
   - Chunk size (tokens or characters).
   - Chunk overlap.
   - Cleaning rules (strip HTML, remove PII, etc.).
   - Output format (plain text, markdown, structured JSON).

3. **Preview** — The sandbox displays formatted chunks and allows the user to:
   - Search within the preview.
   - Compare different parameter settings side-by-side.
   - Flag chunks that look wrong.

4. **Ship** — On approval:
   - Save the ingestion configuration to a config file or database.
   - Trigger the full ingestion pipeline with the approved parameters.

### Key Files
- `src/external_context_loader.py` — already exists, needs parameter exposure.
- `src/sandbox_session.py` — new `create_ingestion_sandbox()` variant.
- `config/ingestion/` — directory for saved ingestion profiles.

---

## P3 — New Application (Separate Git Repo)

### Goal
Scaffold and develop a standalone application in its own Git repository, entirely within a Janus sandbox.

### Design

1. **Sandbox Constructor** — `create_app_sandbox(app_name: str, template: str = "python-package")`
   - Calls `git init` in a temporary directory.
   - Optionally creates a remote on GitHub via `gh repo create`.
   - Applies a template (Python package, CLI tool, web app, etc.).

2. **Development Flow** — Same as the code-modification sandbox:
   - Janus writes files, runs tests, iterates.
   - The user reviews diffs and approves changes.

3. **Ship** — On approval:
   - Push the repository to the remote.
   - Optionally, link the new repo back to Janus as an external project.

4. **Challenges**
   - This is the most architecturally distant from the current sandbox.
   - Requires GitHub CLI or SSH key management.
   - Memory management is simpler (no Janus code to preserve), but the sandbox lifecycle is longer.

### Key Files
- `src/sandbox_session.py` — new `create_app_sandbox()` variant.
- `templates/` — directory for app templates.
- `src/external_project_manager.py` — new module for linking external repos.

---

## Implementation Order

1. **P0 — Skills → Database** (next sprint)
   - Add `register_skill()` to `skill_executor.py`.
   - Add `create_skill_sandbox()` to `sandbox_session.py`.
   - Add integration test for skill registration + execution.

2. **P0 — Janus Next Version** (concurrent with #1)
   - Add freeze/replay to `self_model.py` and `memory.py`.
   - Integrate into `create_sandbox_session()` / `ship_sandbox_session()`.

3. **P1 — Multi-Agent Testing** (after #1 and #2)
   - Add temporary agent registration to `swarm_registry.py`.
   - Build test harness and evaluation framework.

4. **P1 — Config Experimentation** (after #3)
   - Add config load/apply to `config.py`.
   - Build side-by-side comparison report.

5. **P2 — Data Ingestion** (after #4)
   - Expose `ExternalContextLoader` parameters.
   - Build preview UI (or CLI output).

6. **P3 — New App Repo** (after #5)
   - Build repo init flow.
   - Integrate with GitHub CLI.

---

## Appendix: Sandbox Session Interface (Current)

```python
def create_sandbox_session(session_name: str) -> dict:
    """Creates a new Git branch and worktree."""

def apply_changes_to_sandbox(proposed_mods: dict) -> dict:
    """Writes file modifications to the sandbox worktree."""

def run_sandbox_tests() -> dict:
    """Runs pytest inside the sandbox."""

def ship_sandbox_session() -> dict:
    """Copies sandbox changes back to the main workspace."""

def abort_sandbox_session() -> dict:
    """Cleans up the sandbox worktree and branch."""
```

Each new sandbox archetype will extend this interface with its own constructor, test runner, and ship logic.

---

## Appendix: Memory Freeze / Replay Specification

### Freeze (before sandbox creation)
1. Export all rows from `memories` table to `memory/freeze_<timestamp>.json`.
2. Export all rows from `primary_concepts` table.
3. Export self-model state (mood, curiosity, reflection_count, pinned traits).
4. Store the freeze timestamp in the sandbox metadata.

### Replay (after ship)
1. Load the freeze JSON.
2. For each memory in the freeze, check if it still exists in the live DB.
   - If missing, re-insert it.
   - If present, skip (it was preserved naturally).
3. For each primary concept, merge with any new concepts created during the sandbox session.
4. Restore self-model state, but allow recent mood changes to persist.
5. Run consolidation to synthesize any new patterns.

---

*End of Sandbox Roadmap*