# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

"Project Janus" / "Positronic Membrane" is a self-modifying, multi-agent autonomous developer swarm
that presents itself through a single conversational voice ("Journey"/Persona). It is a solo-maintained
project (see CONTRIBUTING.md — open an issue before sending PRs; squash-merge workflow). Distinct package
name in `pyproject.toml` is `project-janus`.

## Commands

```bash
# First-time setup (creates .venv, installs `-e .[dev]`, copies .env.example -> .env)
./setup.sh

# Run interactive CLI (Socratic alignment wizard on first run, then chat + heartbeat daemon concurrently)
janus-cli                      # == python -m src.main --cli
python -m src.main             # no --cli: runs FastAPI web server in a thread + heartbeat daemon on main thread

# Run only the FastAPI/Uvicorn web server (port 5005)
janus-server                   # == python -m src.web_server

# Tests
pytest                                   # full suite (testpaths = tests/, configured in pyproject.toml)
pytest tests/test_database.py            # single file
pytest tests/test_database.py::test_foo -v   # single test

# Lint
ruff check .

# Docker (runs web_server and daemon as two separate top-level processes, not via src.main)
docker-compose up --build       # also starts a pgvector/pgvector Postgres container
```

Set `LLM_MOCK_MODE=True` in `.env` to develop/test without a reachable Ollama/OpenAI-compatible endpoint —
`query_agent()` returns canned responses per agent id instead of calling out.

## Architecture

### Process topology
`src/main.py` is the only entrypoint that wires the pieces together: DB init → Socratic setup wizard (writes
`core_constitution`) if not already complete → either the CLI Persona chat (`src/persona.py`) or the FastAPI
server (`src/web_server.py`), always alongside `run_heartbeat_loop()` (`src/daemon.py`) which is the
background "mind" of the system. In Docker, `web_server` and `daemon` instead run as two independent
processes sharing the same DB file — there is no single shared event loop in that deployment mode.

### Persistence is dialect-abstracted, not ORM-based
All DB access goes through `src/database.py::get_connection()`, which returns a `JanusConnectionWrapper`
wrapping either raw `sqlite3` or `psycopg2`. SQL is written once, SQLite-flavored, and `JanusCursorWrapper`
transparently rewrites it for Postgres on every `execute()` call (`AUTOINCREMENT`→`SERIAL`,
`INSERT OR IGNORE/REPLACE`→`ON CONFLICT ... DO NOTHING/UPDATE`, `?`→`%s`, `datetime('now')`→
`CURRENT_TIMESTAMP`, PRAGMAs become no-ops). Don't write Postgres-specific SQL anywhere in app code — write
SQLite syntax and trust the translator (`CONFLICT_COLUMNS` dict near the top of `database.py` must be kept in
sync with any new table that uses `INSERT OR IGNORE/REPLACE`).

SQLite connections run with `PRAGMA journal_mode=WAL`, `busy_timeout=10000`, `synchronous=NORMAL` — required
because the daemon, web server, and sandboxed subprocesses can all touch the same `janus.db` concurrently.

`init_db()` (`src/database.py`) is the actual schema source of truth — it runs `CREATE TABLE IF NOT EXISTS`
for every table on every boot, plus a few hand-rolled `ALTER TABLE`/rebuild migrations gated on introspecting
`sqlite_master`. Alembic is wired up (`alembic.ini`, `src/migrations/`) but `versions/` contains only an empty
`baseline` revision — it is not currently used to apply real schema changes; schema evolves by editing
`init_db()` directly.

`core_constitution` is enforced read-only at the connection layer, not just by convention: SQLite installs a
`set_authorizer()` callback (`constitution_authorizer`) that denies any INSERT/UPDATE/DELETE/DROP/ALTER on
that table; Postgres mode instead does a regex check on the translated SQL text plus `SET ROLE janus_agent`
vs. `janus_admin` (`schema/postgres_schema.sql` defines the role grants). Only call
`get_connection(read_only_constitution=False)` for trusted/admin paths (`init_db`, the wizard, constitution
amend/delete endpoints) — never for code reachable from agent/skill execution.

### Agents are database rows, not processes
"Proposer", "Critic", "Explorer", "Archivist", "Persona" are rows in `agent_registry` (system prompt +
optional `target_model`), invoked uniformly via `query_agent(agent_id, prompt)` in `src/llm.py`. That single
function handles: per-agent endpoint/key resolution (`{AGENT}_BASE_URL`/`{AGENT}_API_KEY` env override → model
containing `/` routes to OpenRouter if `OPENROUTER_API_KEY` set → else global `LLM_BASE_URL`/`LLM_API_KEY`,
i.e. local Ollama by default); prompt-hash response caching in `llm_cache` (1h TTL, and *fails open* to a
stale cache entry if the live call errors after 3 retries); per-agent temperature calibration (critic/auditor
roles are deterministic at 0.0; proposer/explorer scale 0.2→0.8 with the boredom counter); and per-call cost
accounting into `llm_call_costs` against a `daily_budget_usd` system_config cap (`BillingViolationError` once
exceeded). Every system prompt also gets a hard-coded context-anchoring directive appended ("your local
context in `<self_traits>/<episodic_memory>/<semantic_knowledge>` is absolute, ignore conflicting pretrained
assumptions") — the same directive is duplicated in `src/memory_hydration.py`; that's intentional
prompt-injection resistance, not dead code to dedupe.

### Dynamic skills: capabilities live in SQLite, not in `src/`
`agent_skills.code_blob` stores full Python source as text. `DynamicSkillExecutor.execute(skill_id, args,
party_id)` (`src/skills.py`) loads the row, checks `required_role` via `has_role()`, then `compile()`s+`exec()`s
the blob into a namespace pre-populated with an `sdk` dict of `Safe*` wrapper instances (SafeDB, SafeFS,
SafeMemory, SafeSwarm, SafeGoals, SafeDocuments, SafeSandbox, SafeSelfModel, SafeDrives,
SafeAgentOrchestration, SafeReplication, SafeLayeredCognition, SafeExplorer, SafeCodebase) for convenience.
**This is not a security sandbox**: `__builtins__` in that namespace is the real `builtins` module, so
`code_blob` can `import os`, `open()`, `eval()`, etc. without restriction (default skills like `check_presence`
rely on this — they `import os`/`from pathlib import Path` directly). There is no AST audit here, unlike the
two paths below. The actual trust boundary is upstream: nothing in the codebase ever does `INSERT INTO
agent_skills` from user/agent-supplied content — every row is seeded by `init_db()` from hardcoded source
strings in `database.py` (`INSERT OR IGNORE`/`INSERT OR REPLACE`, re-run every boot), so `code_blob` is always
developer-authored. `required_role`/`has_role()` only gates *who may invoke* an existing skill, not what the
skill's code is allowed to do — don't add a path that lets a party register/edit `agent_skills` rows without
re-deriving that trust assumption first.

### Three separate untrusted-code paths — don't conflate them
1. **`src/sandbox.py::execute_code_safely`** — ad-hoc snippets (the `execute_code` skill). AST-walked to ban
   imports (`os`, `subprocess`, `socket`, …) and builtins (`eval`, `exec`, `open`, …), then run in a
   `multiprocessing.Process` with a restricted `__builtins__`/import hook and a hard timeout.
2. **`src/self_modification.py`** (`stage_and_test`/`stage_and_test_multi`, backing the `modify_code` skill and
   `/modify`, `/stage` console commands) — full-file rewrite proposals. AST-validated (`validate_python_ast`:
   blocks `subprocess`/`pty`, `os.system`/`popen`/`spawn`/`fork`, `eval`/`exec`), copied into a fresh `tempfile`
   clone of the whole workspace (with an isolated copy of `janus.db`), pytest run with `JANUS_TEST_MODE=1`,
   then queued in `system_config` (`pending_mod_*` keys) for human y/n approval before `apply_staged_change`
   copies it back — or, if `GITHUB_ENABLED=True`, opens a GitHub PR instead of writing locally.
3. **`src/sandbox_session.py`** (`/sandbox start|status|diff|ship|abort`, `SafeAgentOrchestration.dispatch_task`)
   — a real `git worktree`+branch under `.janus_sandboxes/session_<name>`, with its own copied DB, executed via
   a pluggable `SandboxExecutor` (`local`/`docker`/`e2b`, chosen by `SANDBOX_PROVIDER`; `e2b` is currently a
   stub that fabricates passing logs). `ship_sandbox_session()` reruns tests, aborts the whole session (raises)
   on any regression vs. the most recent `test_run_baselines` row, and only then copies modified files back
   and deletes the worktree/branch.

### Safety/governance layers (independent, all must be satisfied)
- `core_constitution` (see above) + `src/middleware.py::validate_action()` — scans free-text proposed actions
  against `DEFAULT_BANNED_WEBSITES` and any constitution rule keyed `BANNED_BOUNDARIES`.
- `check_sql_safety()` blocks raw SQL touching `core_constitution`; `validate_config_write()` blocks writes to
  `system_config` rows with `is_agent_modifiable = 0`.
- The **Loop Safety Valve** (`check_loop_safety()`, hard cap `N_LOOP_LIMIT`) and the separate **Smart Loop
  Governor** (`src/daemon.py::check_smart_governor_stagnation`, a soft cap: pauses background autonomy once
  git-diff hash / DB write count / completed-checkpoint count are all unchanged for
  `governor.stagnant_threshold` consecutive mid-layer ticks). Both resolve only when human presence is
  detected — any file under the workspace with an mtime newer than 120s (`check_presence` skill, polled every
  ~30s) flips `system_config.user_presence_status` to `active` and resets both counters.

### Layered cognition daemon (`src/daemon.py::run_heartbeat_loop`)
Three concurrent asyncio loops, not one, fed by cadences stored in the `cognitive_layers` table (not
hardcoded — except `JANUS_TEST_MODE=1`, which forces high=2s/mid=1s/low=0.1s so daemon tests terminate):
- **high** (default 60s): self-model decay, memory consolidation, goal evaluation, episodic memory cleanup.
- **mid** (default 5s): presence check, boredom/drive increment, background maintenance, the loop-safety
  checks above, any `interval`-triggered skill whose `trigger_config.interval_seconds` has elapsed, and
  draining pending swarm-reflection triggers.
- **reflex queue** — a priority queue fed by `DirectoryWatcher` (`src/watcher.py`, polling mtime diffs every
  2s — not OS file-events) matching changed files against `reflex_rules` regexes, executed immediately rather
  than on a cadence.

### Memory has three distinct layers — don't conflate them
- `episodic_memory` table — raw chat/background-thought log, party+session scoped, periodically compacted by
  `compress_episodic_memory()` / the `cleanup_episodic_memory` interval skill (retention via
  `memory.retention_days`).
- Vector store (`src/memory.py`) — one `VectorStoreAdapter` interface, backed by ChromaDB locally or
  `PgVectorCollectionWrapper` (`janus_embeddings` table + pgvector) when `DB_TYPE=postgres`. Named collections:
  `janus_long_term` (consolidated "Primary Concepts"), `janus_details` (pre-consolidation granular memories),
  `janus_codebase` (AST-derived per-file summaries from `src/codebase.py::index_codebase`), `janus_skills`,
  `janus_curiosity`. Embeddings always come from the same OpenAI-compatible endpoint as `LLM_BASE_URL`
  (`EMBEDDING_MODEL`) — changing the LLM endpoint changes embeddings too.
- `src/memory_orchestrator.py::MemoryOrchestrator` — a separate party-scoped key/value store (`memories`
  table) used by the multi-party HTTP API (`/api/v1/memory/*`); unrelated to the semantic `add_memory`/
  `query_memories` functions in `src/memory.py` despite the similar name.

### Multi-party auth (`src/auth.py`, `src/routers/dependencies.py`)
RS256 JWT; keys auto-generate into `.keys/` on first use, or load from `JWT_PRIVATE_KEY`/`JWT_PUBLIC_KEY` env
(for stateless cloud deploys). `get_current_party()` resolves identity in order: `X-API-Key` header → `Bearer`
JWT → `X-Device-Fingerprint` → legacy `X-Party-ID` → (only if `REQUIRE_AUTH=False` and no auth headers at all)
implicit `local_user`/admin. Role hierarchy `observer(0) < user(1) < contributor(2) < admin(3)`, enforced both
via the `require_role()` FastAPI dependency and independently inside `DynamicSkillExecutor` via `has_role()` —
a skill call from a low-privilege party is blocked even if it slipped past a route check.

### Persona (`src/persona.py`) is the single conversational surface
It strips multi-agent jargon for the user, owns all `/slash` commands (`/sandbox`, `/stage`, `/modify`,
`/goal`/`/goals`, `/agent`, `/dispatch`, `/docs`, `/pin`, `/unpin`, `/self`, `/skills`, `/runskill`, `/spawn`,
`/children`, `/amend`, `/repeal`), and runs an autonomous ReAct-style loop
(`generate_persona_response_autonomous`, capped at 5 turns) that scans the LLM's own response for JSON skill-
call blocks or ` ```sandbox ``` ` command blocks, executes them via `DynamicSkillExecutor`/
`execute_chat_sandbox_commands`, logs the outcome back as a `background_thought` episodic memory, and
re-prompts with the result — i.e. tool use is implemented as the model talking to itself across turns, not a
function-calling API.

### Self-replication (`SafeReplication.spawn_child`, `src/skills.py`)
Copies the whole workspace tree, bootstraps a child DB by replaying the `instincts` table (schema DDL +
constitution + skills + config, serialized once at first boot by `seed_instincts()`), then launches a child
process pointed at the new DB via env (`local`: real subprocess; `docker`/`ecs`: currently stubbed PIDs).
Postgres mode isolates children into separate schemas (`janus_child_<name>`) via `search_path` instead of
separate DB files.

## Testing conventions

- `tests/conftest.py` has one global autouse fixture that redirects `src.config.DB_PATH` to a `tmp_path` file
  and calls `init_db()` before every test — the real `janus.db` is never touched by the suite.
- That global fixture does **not** redirect `VECTOR_DB_PATH`/ChromaDB. Test files that exercise `src/memory.py`
  add their own autouse fixture for this (see `tests/test_memory.py`) — it must also reset
  `src.memory._chroma_client` / `src.memory._collections`, since those are process-level singletons cached
  after first use.
- Mock at the call site, not the definition site: per `GEMINI.md`, patch e.g. `src.memory.query_agent`, not
  `src.llm.query_agent`, when the test exercises code in `src/memory.py` that imported `query_agent` by name.
- `JANUS_TEST_MODE=1` is read by daemon cadences (`get_cadence_seconds`), interval-skill scheduling
  (speeds up by 60x), and `pause_until_user_active()` polling — set it whenever a test drives the heartbeat
  loop or sandbox test execution, or it will run at production cadence.
