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

`pr_review` also quarantines the *linked issue's* Acceptance Criteria text and
the PR diff inside the critic prompts (`src/pr_review.py::_evaluate_criterion`,
the quality-notes call) — a trusted PR author can still reference (`Closes
#N`) an issue opened by someone else, so the issue's content gets the same
quarantine framing independent of the PR author check. The handoff bundle's
issue *title* is quarantined alongside the body rather than left in the raw
`# Agent Handoff: Issue #N — <title>` heading, for the same reason.

## Known limitations (not fixed here)

- **Skill-execution results re-enter the ReAct prompt unquarantined.** Only
  the directly-spliced web-search snippet in `_build_persona_prompt` got
  `quarantine_wrap`. Any skill invocation that fetches external content
  (`SafeExplorer.fetch`, `SafeGitHub.get_issue`/`get_pr`/etc., available to
  any non-`"system"` caller) has its raw result logged as a `background_thought`
  episodic-memory row, which is spliced back into the *next* turn's prompt via
  `deliberation_summary` — the same buffer the ReAct loop's skill-call parser
  scans. This is architecturally the same vector this issue closes for web
  search, but fixing it generally means auditing/wrapping every skill's return
  value at the point it's logged, which is a larger change to the ReAct loop's
  prompt assembly than this issue's scope. Tracked for a follow-up.
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

- **Issue #70** (agent-status comment polling): does not exist yet
  (`src/agent_sync.py` is absent). Its author-allowlist gate ships with #70
  itself, not retrofitted onto a feature that isn't built.
- **Issue #101** (conformance suite): "content from non-allowlisted authors is
  never parsed into action" is a conformance-candidate invariant for
  descendant systems to keep — tracked there, not re-derived here.

## Maintaining this document

Update this document whenever a new feature ingests GitHub or web content, or
when a new adversary class becomes relevant (e.g. authenticated multi-party
API clients once external-party trust levels diverge further).
