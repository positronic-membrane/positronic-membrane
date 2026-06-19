# Decoupled Development Environments & On-Demand Skills Library

This document specifies the architectural design for Project Janus's refined development sandboxes and its dynamic, database-driven capability system. 

---

## 1. Architectural Overview

To keep the core engine of Project Janus lightweight and focus-driven, all development tasks are split into three distinct sandboxing models:

```
                  ┌─────────────────────────────────────────┐
                  │          Project Janus Core             │
                  └────────────────────┬────────────────────┘
                                       │
         ┌─────────────────────────────┼─────────────────────────────┐
         ▼                             ▼                             ▼
┌──────────────────┐          ┌──────────────────┐          ┌──────────────────┐
│  Evolution SB    │          │    Project SB    │          │  Skill Staging   │
├──────────────────┤          ├──────────────────┤          ├──────────────────┤
│ • Branch:        │          │ • Empty git repo │          │ • Temp local dir │
│   evolution/*    │          │ • Isolated root  │          │ • Mock SDK stubs │
│ • Isolated DB    │          │ • App building   │          │ • Pytest run     │
│ • Child daemon   │          │ • Multi-lang test│          │ • Db compilation │
└──────────────────┘          └──────────────────┘          └──────────────────┘
```

---

## 2. Dynamic Skill Staging Harness

### The Problem
Historically, when coding agents or assistants created new skills for Janus, they edited code directly in the core codebase (`src/` or tests directory). While this was convenient, it resulted in core codebase bloat and contaminated git history with tools that should be dynamic.

### The Solution: Zero-Codebase Staging
We introduce `src/skill_harness.py`. When a skill is created or edited:
1. **Directory Isolation:** Janus creates an ephemeral, git-ignored workspace under `.janus_sandboxes/temp_skills/<skill_id>/`.
2. **Local Staging:** Janus writes the skill logic to `skill.py` and the corresponding unit tests to `test_skill.py`.
3. **Mock SDK Harness:** To test the skill without modifying the production environment (e.g. running destructive filesystem or database writes), Janus runs the tests inside a python context where the global `sdk` is mocked (`MockSafeFS`, `MockSafeDB`, `MockSafeMemory`).
4. **Execution:** The harness executes `pytest` against this temporary directory.
5. **Compilation & Installation:** If the tests pass:
   * The harness reads the contents of `skill.py` into a raw string.
   * It runs a structural AST audit on the string.
   * It performs an upsert into the SQLite `agent_skills` table (`code_blob=code_string`).
   * The temporary directory is deleted.

No physical files are ever committed to the core repository, yet the skill is fully functional and ready to run dynamically.

---

## 3. Sibling "Skills Library" Repository

Instead of keeping all skills strictly in the SQLite database (which makes collaborative development, rollbacks, and code reviews difficult), we store raw skill source code in a **dedicated, external repository** (e.g. `janus-skills-library`).

### Repository Structure
The external repository acts as a package registry for Janus skills:
```text
janus-skills-library/
  ├── registry.json             # High-level index of all available skills
  └── skills/
        ├── slack_notifier/
        │     ├── metadata.json # Name, description, required role, parameters schema, dependencies
        │     ├── skill.py      # Executable Python code blob
        │     └── test_skill.py # Pytest unit tests for the skill
```

### On-Demand Discovery and Loading
1. **Semantic Indexing:** Janus clones/tracks the Skills Library repository locally. During boot or background memory consolidation, it reads the `metadata.json` files and indexes their semantic descriptions into ChromaDB.
2. **Dynamic Resolution:** When the planner daemon identifies a goal that requires a capability not currently present in its database, it queries the index for matching library skills.
3. **Automated Verification:** The system uses the **Skill Staging Harness** to run the library skill's test suite against the live/mocked environment.
4. **Just-in-Time Compilation:** Once verified, the skill is compiled into the SQLite database and activated instantly.

---

## 4. Sandbox Refinements

To support clean development workflows, the core sandboxing framework in `src/sandbox_session.py` is expanded to support two distinct modes:

### A. Evolution Sandboxes
* **Purpose:** Developing the "next version" of Janus without writing changes back to the active working copy.
* **Mechanism:** 
  1. A separate Git worktree is created on an `evolution/<version>` branch.
  2. The production database is duplicated into the worktree.
  3. A concurrent child Janus daemon process is spawned in the worktree, pointing to the duplicated database and running on an offset port (e.g., `5001`).
  4. Parent and child can communicate in the background using SQLite `swarm_messages`.
* **Promotion:** When the new version is verified, the parent Janus runs a promotion script that merges the code changes into the main branch, updates the database schema, and ports over the delta memories/logs accumulated in the parent's DB during the sandbox's lifespan.

### B. Project Sandboxes
* **Purpose:** Creating entirely new, independent software applications from scratch.
* **Mechanism:**
  1. Janus provisions an empty folder under `.janus_sandboxes/projects/<app_name>`.
  2. It initializes a clean, independent Git repository (`git init`).
  3. It scopes all safe file operations (`SafeFS`) to this empty root.
  4. Build commands and test suites (e.g. `npm run test`, `pip install`) are executed in the context of this external sandbox directory.
