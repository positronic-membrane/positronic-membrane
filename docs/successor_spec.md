# Positronic Membrane — Successor Specification (v2)

**Status:** DRAFT — pending operator ratification. Merging the PR that introduces this file constitutes ratification (§10).
**Issue:** #111
**Consumes:** Restructured Roadmap V2–V8+ (document memory doc #14) as advisory input
**Companions:** #112 (evaluation harness — defines "better"), #113 (memory architecture annex), #96 (V1 sign-off), #107 (threat model)

---

## 0. How to read this document

- **MUST / MUST NOT** are hard requirements; a successor build that violates one is not v2.
- **SHOULD** is the default; deviations require a written rationale in the deviating PR.
- Consumers of this document: the operator (ratification and amendments), the roadmap-to-issue decomposer (#93 — issues it drafts trace back to sections here), dispatched coding agents (handoff bundles may quote sections as acceptance context), and the cost model (#110 — §6 and §8 bound the issue count).

---

## 1. Premise

v1 (this codebase) freezes at V1 sign-off (#96, #97) and thereafter acts only as **orchestrator and reviewer**. The successor ("v2") is built in a **separate repository** seeded from this one (#98), by external coding agents dispatched per issue (#99), reviewed against acceptance criteria (#69) and a conformance suite (#101), and deployed as a **separate instance** with its own database, secrets, and host (#92, #103).

**Identity:** a new instance either starts fresh or ingests v1's exported metadata through the explicit import mechanism (#100). There is no implicit continuation. The fresh-vs-import choice is made by the operator at v2's first boot; this spec does not pre-commit it (§9.5 records the current leaning).

---

## 2. What v2 is

> **v2 is v1's capabilities, rebuilt on an architecture designed for autonomous-agent development, plus exactly one capability theme: Proactive Goal Pursuit.**

### 2.1 Rebuild for buildability

The fork's value is not features — it is that v2's architecture is shaped for the agents that will build v3:

- Modules sized for an agent's context window (§4.2), so a dispatched agent can hold an entire unit of work.
- Safety invariants as design inputs, not retrofits (§4.3) — the conformance suite passes from the first commit.
- Exactly one of everything: one migration story, one dependency manifest, one persistence dialect, one memory design (§4.4–4.6).
- The self-development pipeline as a first-class subsystem, not a bolt-on (§2.3).

### 2.2 One capability theme: Proactive Goal Pursuit

Carried from doc #14's V2. In v2, the system formulates its own goals and pursues them autonomously with human ratification: background reflection generates goal proposals without prompting; proposals are ratifiable via CLI and API; approved proposals become active goals with checkpoints that background work demonstrably advances. v1 shipped fragments of this (goal proposals #77, goal wiring); v2 makes it the organizing behavior.

No second theme is in scope. Candidate themes (memory curation depth, interpretability, parallel swarm work) are inputs to the roadmap v2 writes for itself (§7).

### 2.3 The recursive requirement

The project's goal is a version of PM that can build the next version of itself. Therefore **v2 MUST be born with the successor pipeline as a designed-in capability**: version-spec → issue decomposition → handoff/dispatch → PR review with author gating → conformance CI → successor deployment. v1 acquired these as seventeen retrofits; in v2 they are one subsystem with one owner-module and one test suite. Success test: *v2 building v3 must be cheaper and safer than v1 building v2* — measured via #110's cost instrumentation carried forward.

### 2.4 Rejected framings (recorded so they stay rejected)

- **Pure port** ("rewrite v1 exactly, then evolve"): months of spend for zero visible progress and no forcing function on architecture quality.
- **Feature push on inherited layout** ("fork, then keep adding"): forfeits the reason to fork; the warts (§5) metastasize into the successor.

---

## 3. Capability parity (MUST retain)

v2 MUST reach behavioral parity with v1 on every category below, measured by the evaluation harness (#112): conformance and E2E suites green, and benchmark scores ≥ v1's recorded baseline per category (or a written rationale accepted by the operator for any regression).

| Category | v1 capabilities that must survive |
|---|---|
| **Conversational surface** | Single-voice Persona; slash-command families (goals, skills, sandbox, docs, self, agents, amendments); autonomous ReAct-style tool use; multi-session continuity |
| **Cognition & drives** | Layered cadences (high/mid/reflex); boredom/curiosity drives feeding reflection; presence detection; loop safety valve + progress-aware governor |
| **Goals** | Goal/checkpoint registry; background proposal generation; ratification flow; goal-context injection into reflection |
| **Memory** | All three layers — episodic log, semantic vector store, party-scoped KV — per the operator's keep ruling; consolidation into primary concepts; retention lifecycle. Implementation per #113, which is a normative annex to this spec |
| **Self-model** | Traits with confidence, pinning, and decay; self-model history |
| **Governance & safety** | Immutable constitution (connection-layer enforcement); action/content validation; config-write protection; role hierarchy enforced at route *and* skill layer; budget cap with violation handling; circuit breaker |
| **Skills** | DB-registered skills with role gating; skills-library sync pinned to a v2 line (#104); interval/reflex triggers |
| **Sandboxing** | Isolated execution for untrusted snippets; worktree-style isolated dev sessions with test gating — with the redesign constraint in §4.7 |
| **Self-development pipeline** | §2.3 — issue decomposition, handoff, dispatch, PR review with author allowlist, conformance CI, successor deployment |
| **Multi-party & API** | RS256 auth chain; party scoping and isolation; FastAPI surface incl. health endpoints |
| **Operations** | Structured logging + metrics (§4.9); notifications; off-site backups with rehearsed restore (#109 pattern); boot-time config validation |

Parity is **behavioral**, not structural: v2 is free to reorganize any of this internally provided #112's probes can't tell the difference or score it worse.

---

## 4. Architecture requirements

### 4.1 Packaging
A properly named top-level package (working name `membrane/`; final name with repo choice, §9.1) — **MUST NOT** install as a top-level module named `src`. `pyproject.toml` is the single dependency manifest; there is no `requirements.txt`.

### 4.2 Module size
Source modules SHOULD be ≤ 500 lines and MUST be ≤ 1,000 (generated/seed data excluded). The persona splits into a command-dispatch core plus per-domain command modules. Rationale: a dispatched agent should hold any single module plus its tests in context; review quality collapses on god-modules.

### 4.3 Conformance-first
The portable safety-invariant suite (#101) is present and green **from the first commit**, wired as a required CI check with operator-owned CODEOWNERS. New privileged capabilities MUST land with their conformance test in the same PR.

### 4.4 Persistence
SQLite-only, WAL mode, behind a thin repository/connection interface so a second backend can return later as a real decision. The Postgres dual-dialect translator is **not carried** (§5). Exactly one migration story, chosen on day one (a real migration runner — not `init_db()` accretion plus an unused Alembic scaffold), with forward migrations tested in CI.

### 4.5 Memory
Three layers, kept per operator ruling; implementation per **#113** (normative annex): each layer has a written contract (what writes it, what reads it, its lifecycle), the KV layer has a live consumer in the hydration path or does not exist yet, embeddings carry explicit model/version metadata with a re-embed migration story, and forgetting is a relevance lifecycle rather than a bare TTL.

### 4.6 Prompts
All system/agent prompts live in a versioned registry with rollback from day one (v1's #67 is the reference design). No prompt strings inline in source.

### 4.7 Code-change discipline — no live-workspace mutation
**The repository is the only write path to v2's code.** Changes flow issue → PR → CI → review → merge → redeploy (#92 machinery pointed at v2's own repo). v2 MUST NOT carry v1's apply-to-live-workspace paths (`apply_staged_change` local mode, `ship_sandbox_session` copy-back, dispatch-approval shipping). This deletes the entire class of gates that #95/#97 exist to police, rather than re-guarding it. Sandboxes remain for *testing* proposals; shipping is always a PR.

### 4.8 Security posture at birth
Threat model document (#107) carried and kept current; author allowlisting on every path that parses external content into action; secrets at rest encrypted with a real cipher and **no default keys in source** (#105 as the reference); per-agent LLM routing policy explicit (#108 — `allow_offbox` is a schema field from day one); token discipline per #103 (v2's tokens have no write scope on v1's repo).

### 4.9 Observability
Structured logging (JSON option), `LOG_LEVEL` env control, and a metrics endpoint (v1's #63 is the reference design) from day one — #112's benchmark and #110's cost model consume these counters, and v3's deployment health-gating will depend on them.

### 4.10 Testing
Unit, E2E, and conformance suites all run in CI from the first commit — no deselected-by-default suites (the v1 lesson behind #106). A test-mode cadence-scaling mechanism equivalent to `JANUS_TEST_MODE` exists from the start.

---

## 5. Do-not-inherit list

Explicitly **not carried** into v2 (each may return later as a deliberate decision, none by inertia):

1. **Postgres dual-dialect layer** (`JanusCursorWrapper` SQL rewriting, role/regex constitution enforcement, pgvector adapter, child-schema isolation) — untested half of a speculative abstraction; §4.4 keeps the door open properly.
2. **Dead code** — `hydrate_context`, the consumerless `MemoryOrchestrator` path (the *role* returns via §4.5/#113 with real plumbing or not at all).
3. **Unused Alembic scaffold** — replaced by §4.4's single migration story.
4. **Stubs that fabricate success** — the e2b executor (#94), docker/ecs spawn providers. Absent capability MUST raise, never pretend.
5. **Apply-to-live-workspace machinery** — per §4.7.
6. **Legacy XOR crypto path** — post-#105 migration, the legacy decrypt helper dies at the fork.
7. **Packaging warts** — top-level `src` module, `requirements.txt`/pyproject duplication (§4.1).
8. **Hardcoded agent-CLI invocation** (`--message` + 120s timeout) — replaced by #99's invocation templates.

**Explicit KEEP rulings** (recorded so subtraction zeal doesn't overreach): the three-layer memory architecture (operator, 2026-07-07 — critical to growth; redesign per #113); the immutable-constitution model with connection-layer enforcement; the single-voice persona principle; the external skills-library mechanism (on a v2 line, #104); the Safe*-SDK pattern for skill capabilities.

---

## 6. Definition of done (v2.0 release)

All of the following, verified at a v2 sign-off checkpoint mirroring #96:

1. **Parity:** conformance suite green; E2E suite green in CI; #112 behavioral benchmark ≥ v1's recorded baseline in every category, or per-category written rationale accepted by the operator.
2. **Theme:** doc #14's V2 definition-of-done items demonstrably working (proposals without prompting; CLI/API ratification; approved proposals become checkpointed active goals that background cycles advance).
3. **Self-planning:** v2 has authored its own next-version roadmap and it has been operator-ratified (§7).
4. **Pipeline-native:** v2 has executed its own pipeline end-to-end at least once — decomposed a spec item into an issue, dispatched it, reviewed and merged the PR, and redeployed itself — under human ratification at the merge gate.
5. **Architecture:** §4 requirements verified (module-size check automated in CI; no do-not-inherit item present — enforced by a grep-able checklist in v2's CI).
6. **Cost:** actuals recorded against #110's model, and the v2→v3 projection updated with measured data (§2.3's success test).

---

## 7. Roadmap inheritance

v2 inherits doc #14 (Restructured Roadmap V2–V8+) as **advisory input, not obligation**. An early v2 milestone — after parity, before or alongside the theme work — is authoring its own roadmap: reconciling doc #14's arcs with what the fork changed, and proposing its own version cadence informed by #110's cost findings. Self-determined planning is a capability this project exists to demonstrate; it is therefore a deliverable, not a hand-me-down. The operator ratifies v2's roadmap exactly as this spec is ratified.

---

## 8. Build process (how v2 gets built)

1. **This spec is decomposed** (#93) into issues in v2's repo, each in the house format with a parseable Acceptance Criteria section (the #69 review gate fails closed without one), each sized to a single branch/agent session (§4.2 makes that tractable).
2. **Dispatch** per issue via #99 (handoff bundle → registered coding agent → PR), with #107's author gating and quarantine in force from the first dispatched issue.
3. **Review & merge:** automated criteria-vs-diff review (#69) plus conformance CI; merge requires the operator (admin gate) — the human ratification point of the whole system.
4. **Deploy:** #92 machinery; v2 instances redeploy from the repo per §4.7.
5. **Order of construction:** skeleton (packaging, CI, conformance, persistence, config) → memory + cognition core → persona surface → goals/theme → pipeline subsystem → parity closure against #112. The decomposer SHOULD emit issues in this dependency order.
6. **Budget:** every dispatch is metered; #110's ledger is reviewed at each phase boundary.

---

## 9. Operator decisions (recorded 2026-07-07, prior to ratification)

1. **Successor repo name:** `positronic-membrane/positronic-membrane-v2`. The package name (§4.1) follows: working name `membrane`, finalized in v2's first skeleton issue.
2. **Persona identity:** **"Journey" carries over.** The name follows the active conversational surface — which resolves #102's "which instance is Journey" question in advance: the persona name belongs to whichever instance holds the surface, transferring at cutover.
3. **Theme:** **Proactive Goal Pursuit confirmed** as the single capability theme (§2.2). If the V1 sign-off reflection (#96) surfaces reasons to change it, that is an amendment PR to this document, not a silent re-scope.
4. **Version numbering:** v2 releases as `2.0.0` under three-part semver; v1's final tag (#96) establishes `1.x`.
5. **Fresh start vs. import:** **import leaning.** v2 is expected to ingest v1's exported metadata (constitution, documents, self-model, goals) at first boot via #100 — putting #100 on the critical path for #93's issue sequencing. The final decision still executes at first boot per §1; if it flips to fresh-start, only sequencing changes, not this spec.

---

## 10. Ratification & change control

- Merging the PR that introduces this document constitutes operator ratification of the full document, including §9's recorded decisions.
- Post-merge, this document is upserted into `janus_documents` (purpose `knowledge`, tags `successor, spec, v2`) so the system's own memory holds the ratified spec — #93 reads it from there.
- Amendments are PRs against this file; each amendment PR states which downstream issues (#93's output) it invalidates.
