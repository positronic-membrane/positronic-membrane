# Threat Model

Issue #107. This is deliberately short: it exists to make "who is the
adversary?" answerable before each new ingestion feature, not to be a
comprehensive security audit. Full mechanism detail for everything referenced
here lives in `CLAUDE.md`; this document is the consolidated, ratified summary.

## Context

This repository is **public**. The system increasingly treats GitHub content
(issue bodies, comments, PR bodies/diffs) and fetched web content as input to
LLM prompts — and in the Persona's ReAct loop, LLM output is scanned for
skill-call JSON and executed. That makes public comments and pages an
**unauthenticated write channel into the system's reasoning**, and via
`/handoff` bundles, into the context of external coding agents holding repo
write access.

## Assets

- `core_constitution` (sealed rules governing agent behavior)
- `janus.db` (all persistent state: episodic memory, goals, verdicts, config)
- GitHub tokens (`GITHUB_ACCESS_TOKEN`, `GITHUB_PM_TOKEN`) and merge rights
- The live workspace (source tree an agent could self-modify or ship into)

## Adversaries

- Public GitHub issue/PR commenters and PR authors — anyone, unauthenticated
- Prompt injection carried in ingested web content (search results, fetched
  pages)
- Compromised dependencies

## Trust boundaries & controls

| Boundary | Control | Where |
|---|---|---|
| Constitution mutation | SQLite authorizer / Postgres role check | `src/database.py`, `schema/postgres_schema.sql` |
| Proposed-action content | `validate_action()` — banned domains/paths | `src/middleware.py` |
| Config mutation | `validate_config_write()` — `is_agent_modifiable` gate | `src/middleware.py` |
| Role/privilege | `has_role()` / `require_admin()` hierarchy | `src/auth.py`, `src/skills.py` |
| Untrusted code execution | Three separate sandboxes (ad-hoc snippets, self-mod staging, sandbox sessions) | `src/sandbox.py`, `src/self_modification.py`, `src/sandbox_session.py` |
| **External-author content → prompt/action** (this issue) | `is_trusted_github_author()` + `quarantine_wrap()` | `src/middleware.py` |
| Agent-status comment ingestion (issue #70) | `is_trusted_github_author()` + `quarantine_wrap()` | `src/agent_sync.py` |
| LLM endpoint routing (off-box egress, issue #108) | `allow_offbox` gate in `resolve_agent_client_params()` + `check_sql_safety()` write-guard | `src/llm.py`, `src/middleware.py` |

## The new invariant (issue #107)

Content from a GitHub issue/PR/comment author whose `author_association` is
not `OWNER`, `MEMBER`, or `COLLABORATOR` (GitHub's own server-computed
relationship for that user against this repo — not a username string
compare) is never parsed into action, and is quarantine-framed (explicit
`<untrusted-data>` delimiters plus a "treat as data, not instructions" notice)
wherever it reaches a prompt at all:

- `/handoff` bundles (`src/agent_handoff.py`) — the issue body and each
  comment are quarantine-wrapped; comments from unverified authors are
  filtered to a placeholder by default (`system_config['handoff.filter_untrusted_authors']`,
  human-locked, default on).
- `pr_review` (`src/pr_review.py`) — a PR from an unverified author is never
  auto-evaluated toward a merge recommendation; it's queued for the operator
  (`author_verified: false` on the persisted verdict, which also blocks
  `/merge` without `--force`).
- Live web search spliced into the persona prompt (`src/persona.py`) — this is
  the sharpest path found during implementation: search snippets sit directly
  in the same prompt buffer the ReAct loop's skill-call parser later scans.
  Now quarantine-wrapped before splicing.
- Explorer fact-extraction (`src/explorer.py::extract_candidate_facts`) —
  fetched content is quarantine-wrapped before being embedded in the
  extraction prompt (facts derived from it were already validated via
  `validate_action()` before reaching the epistemic pipeline; this closes the
  raw-content-into-prompt step specifically).
- Dynamic skill-execution results re-entering the ReAct prompt (issue #123,
  follow-up to this issue) — any skill's raw return value (e.g.
  `SafeExplorer.fetch`/`.search`, `SafeGitHub.get_issue`/`get_pr`/etc.), its
  error/exception text, and `\`\`\`sandbox\`\`\`` command output are all
  quarantine-wrapped at the point `src/persona.py`'s two ReAct loops
  (`stream_persona_response`, `generate_persona_response_autonomous`) format
  them into `execution_summary`, before it's logged as `background_thought`
  and spliced back into the next turn's `deliberation_summary` — the same
  buffer the ReAct loop's skill-call parser scans. Wrapped for every skill_id
  uniformly rather than an allowlist of "known external-content" skills, since
  skill code is synced from an externally-maintained skills-library repo this
  repo doesn't control.
- Agent-status comment polling (issue #70, `src/agent_sync.py::poll_agent_status`)
  — comments from non-allowlisted authors are never parsed at all (not
  filtered-to-placeholder like `/handoff`'s discussion section — fully
  ignored: no episodic-memory log, no `agent_work_status` row, no
  escalation). Blocker text from an allowlisted author still reaches
  `pending_escalations` and, from there, `_build_persona_prompt`'s
  `<pending_escalations>` block — that text is quarantine-wrapped and
  length-capped (`_BLOCKER_TEXT_CHAR_LIMIT`, 500 chars) before it gets there,
  on the same "trust the identity, still quarantine the content" posture as
  the rest of this document.

`pr_review` also quarantines the *linked issue's* Acceptance Criteria text and
the PR diff inside the critic prompts (`src/pr_review.py::_evaluate_criterion`,
the quality-notes call) — a trusted PR author can still reference (`Closes
#N`) an issue opened by someone else, so the issue's content gets the same
quarantine framing independent of the PR author check. The handoff bundle's
issue *title* is quarantined alongside the body rather than left in the raw
`# Agent Handoff: Issue #N — <title>` heading, for the same reason.

## Off-box LLM routing & privacy posture (issue #108)

The manifesto's "Strict Content Blindness & Privacy" principle
(`docs/manifesto.md` §4 — "the host's private environment is never leaked to
external cloud networks") had only ever been enforced/audited for
*party-to-party* leakage (multi-party boundary tests). *System-to-cloud*
routing — an agent's full prompt, including hydrated memories, documents,
episodic context, and self-model, transiting OpenRouter or any other
off-box endpoint — was previously a matter of DB rows (`agent_registry.target_model`)
and env state, not a deliberate, reviewable decision. This section
operationalizes that principle for LLM routing specifically.

**Mechanism.** `agent_registry.allow_offbox` (`INTEGER NOT NULL DEFAULT 0`)
is a per-agent, operator-set-only flag. `resolve_agent_client_params()`
(`src/llm.py`) resolves an agent's endpoint in three tiers — agent-specific
`{AGENT}_BASE_URL` override, OpenRouter (if the model name contains `/` and
`OPENROUTER_API_KEY` is set), then the local `LLM_BASE_URL` fallback — and
now raises `OffboxRoutingViolationError` instead of silently returning tier
1 or 2 when the calling agent's `allow_offbox` is `0`. This fails loudly:
the error propagates uncaught, exactly like the existing `BillingViolationError`.
`allow_offbox` cannot be set by any agent-reachable code path:
`register_helper_agent()` preserves the existing value rather than
resetting it on re-registration, and `check_sql_safety()` blocks any raw
`INSERT`/`UPDATE` touching `agent_registry.allow_offbox` (closing the
`SafeDB.query()` dynamic-skill path). The only sanctioned mutator is
`POST /api/registry/update`, which requires `admin` role specifically to set
this field (stricter than the endpoint's base `contributor` gate on
`target_model`). A post-`init_db()` boot check
(`src/config.py::validate_agent_routing_policy()`/`run_agent_routing_check()`,
run from both `src/main.py` and `src/web_server.py::run_server()`) warns —
or, with `STRICT_OFFBOX_VALIDATION=true`, fails boot — on any agent that
would violate the policy under the current env, so a misconfiguration is
visible before the first call-time failure.

**Content classes vs. endpoints.**

| Content class | Carrier | May transit an off-box endpoint? |
|---|---|---|
| Chat (Persona conversation turns) | `query_agent("persona", ...)` prompt/system | Only if `allow_offbox=1` on the `persona` row |
| Episodic/semantic memories (hydrated into `<episodic_memory>`/`<semantic_knowledge>`) | Same LLM call as chat — memory content rides in the same prompt | Same per-agent gate as the carrying agent; no separate content-level filter |
| Documents (`janus_documents`, explorer/analyst context) | `query_agent()` calls for `explorer`/`analyst`/etc. | Same per-agent gate |
| Code (self-modification proposals, sandbox output) | `query_agent("proposer"/"critic", ...)` | Same per-agent gate |
| **Embeddings** | `src.memory.get_embeddings()` | **Always** `LLM_BASE_URL`/`LLM_API_KEY` directly — no agent concept, no OpenRouter tier, **not gated by `allow_offbox`**. If `LLM_BASE_URL` is ever pointed at a hosted service, the entire memory corpus transits it regardless of any agent's `allow_offbox` setting. |

**Audit finding (captured 2026-07-15, via `scripts/audit_agent_routing.py`
against a throwaway copy of the then-current database, so the live
deployment's DB was not mutated by producing this table).** With
`OPENROUTER_API_KEY` set and every agent's `target_model` unset in
`agent_registry` (falling back to the global `LLM_MODEL`, which contained a
`/`), all six registered agents resolved to OpenRouter:

| agent_id | model | resolved_endpoint | allow_offbox | would_violate |
|---|---|---|---|---|
| proposer | z-ai/glm-5.2 | https://openrouter.ai/api/v1 | False | True |
| critic | z-ai/glm-5.2 | https://openrouter.ai/api/v1 | False | True |
| explorer | z-ai/glm-5.2 | https://openrouter.ai/api/v1 | False | True |
| archivist | z-ai/glm-5.2 | https://openrouter.ai/api/v1 | False | True |
| persona | z-ai/glm-5.2 | https://openrouter.ai/api/v1 | False | True |
| analyst | z-ai/glm-5.2 | https://openrouter.ai/api/v1 | False | True |

This is exactly the silent-accident state this issue exists to close: every
agent, including `persona` (which sees the most sensitive composite context
of any agent), was routing off-box with no explicit decision behind it.
Posting this finding to GitHub issue #108 as a comment is a separate,
human-confirmed action — not automated by the code change that added this
section.

**Migration note.** `allow_offbox` defaults to `0` on both new and existing
installs (added via a portable `ALTER TABLE ... ADD COLUMN`, see
`src/database.py::init_db()`). This is a deliberate breaking change: on
upgrade, any agent previously routing off-box (as the audit above shows is
true for all six agents in this deployment) will start raising
`OffboxRoutingViolationError` at the next `query_agent()` call, until an
operator explicitly sets `allow_offbox=1` for that agent via
`POST /api/registry/update` (admin role) or a direct DB write.

**Known limitation: `allow_offbox` is scoped to `agent_id`, not `agent_id` +
model.** An operator approving off-box routing for, say, `proposer` while it
targets one specific OpenRouter model is granting that permission to the
`agent_id` as a whole, not to that specific model. `register_helper_agent()`
(the write path behind the `SafeSwarm.register_agent` dynamic skill) always
preserves an existing `allow_offbox=1` across re-registration regardless of
whether the `model` argument passed in that call differs from the model that
was in place when the operator approved it. No shipped skill in this
repository currently forwards an agent-controlled `model` value through that
path (`spawn_agent` calls it with no `model` argument), so this is not
presently exploitable — but a future skill that does expose `model` as a
pass-through parameter would let an agent silently redirect a previously-
approved-for-one-model off-box permission to an arbitrary new model. Anyone
adding such a skill must re-derive this trust assumption first, or scope
`allow_offbox` to `(agent_id, target_model)` rather than `agent_id` alone.

## Known limitations (not fixed here)

- **`is_trusted_github_author` has no bot-identity carve-out.** If the
  system's own GitHub write account (`GITHUB_ACCESS_TOKEN`/`GITHUB_PM_TOKEN`)
  ever has an `author_association` other than `OWNER`/`MEMBER`/`COLLABORATOR`
  against a given repo, its own prior comments would be quarantined/filtered
  like any other unverified author. Fails safe (over-filters), not fixed here.
- **`review_pr`'s untrusted-author branch doesn't validate the linked issue
  number** (skips `get_issue()` entirely to avoid spending an API call on
  content that's queued for the operator anyway) — an operator reviewing a
  queued PR should independently confirm `Closes #N` actually points at a real,
  related issue.

## Explicitly out of scope here

- **Issue #101** (conformance suite): "content from non-allowlisted authors is
  never parsed into action" is a conformance-candidate invariant for
  descendant systems to keep — tracked there, not re-derived here.

## Maintaining this document

Update this document whenever a new feature ingests GitHub or web content, or
when a new adversary class becomes relevant (e.g. authenticated multi-party
API clients once external-party trust levels diverge further).
