# Pre-Cloud Multi-Party Hardening

## Purpose
Prepare Janus for distinct, persistent relationships with multiple parties **before** any cloud deployment. This document captures the on-premise, local-development tasks that must be completed prior to scaling to cloud environments.

---

## Phase 1: Party Interaction Profiles *(Revised per Design Review)*

**Principle:** Janus maintains a single, unified self-model. Party-specific interaction profiles are a *relational layer* — not a core identity fork.

| Component | Description |
|-----------|-------------|
| `interaction_profile` table | Keyed to `party_id`. Stores surface-level preferences: typical response style (concise vs. verbose), recurring topics, common friction points, interaction frequency. |
| Profile injection | Loaded into the session context at party authentication. Overrides global defaults for response tone, length, and topic bias without altering the self-model. |
| Query interface | `get_profile(party_id)` returns the profile dict; `set_preference(party_id, key, value)` updates a single field. Both route through the `party_manager` module. |

---

## Phase 2: Party Discovery & Identity Resolution

**Principle:** Each returning party must be consistently identified across sessions without ambiguity.

| Component | Description |
|-----------|-------------|
| Identity sources | Device fingerprint (hash of client IP + user-agent), explicit API key, or session token. Priority order configurable per deployment. |
| Resolution logic | Match against existing `party` table. If no match, create a new party record with a generated `party_id`. If partial match (e.g., same fingerprint but different API key), flag for manual review or auto-merge based on policy. |
| Conflict handling | When two identity hints point to different parties, log the collision and default to the most recent `party_id` used by the stronger signal (API key > token > fingerprint). |
| Cache layer | In-memory LRU cache for active party profiles to avoid repeated DB lookups during a single session. |

---

## Phase 3: Per-Party Conversation Log Isolation

**Principle:** Party A must never see Party B's conversation history, even in transient logs or error messages.

| Component | Description |
|-----------|-------------|
| `conversation_log` table | Keyed to `party_id` and `session_id`. Stores full message history per party with a `context_id` for threading. |
| Log retrieval | `get_conversation(party_id, limit=50)` returns only that party's messages, ordered by timestamp. |
| Log injection | At session start, the system loads only the conversation for the authenticated party into the prompt context. |
| Isolation enforcement | All database queries include a `WHERE party_id = ?` clause. Unit tests validate that cross-party queries return zero rows. |
| Pruning | Old sessions beyond a configurable retention window are archived to a separate `conversation_archive` table, still partitioned by `party_id`. |

---

## Phase 4: Modification History Per Party

**Principle:** Every codebase change must be attributable to the requesting party, enabling per-party rollback and audit.

| Component | Description |
|-----------|-------------|
| Sandbox stamp | When a party initiates a modification, the sandbox session records the `party_id` in its metadata. |
| `modification_log` table | Stores `change_id`, `party_id`, `sandbox_branch`, `diff`, `timestamp`, and `status` (proposed, tested, shipped, aborted). |
| Audit UI | (Future) A dashboard showing each party's modification history, with rollback buttons for shipped changes. |
| Rollback | Using the stored diff, the system can reverse a specific party's change without affecting changes from other parties (provided no conflicts). |

---

## Phase 5: Testing Party Boundaries

**Principle:** Automated safeguards ensure parties cannot leak data across isolation boundaries.

| Test scenario | Description |
|---------------|-------------|
| Cross-party read | Attempt to retrieve conversation logs of party B while authenticated as party A. Expect empty result or error. |
| Cross-party write | Attempt to insert a log entry under party B's `party_id`. Expect rejection. |
| SQL injection boundary | Send specially crafted inputs as party A that try to query party B's data. Confirm no data leakage. |
| Profile overlap | Verify that preference updates for party A do not alter the profile returned for party B. |
| Session replay | Simulate party A disconnecting and party B connecting from the same machine. Confirm no stale context contamination. |

---

## Phase 6: Migration Considerations

**Principle:** Existing single-party databases must upgrade to multi-party without data loss or downtime.

| Step | Action |
|------|--------|
| 1. Schema backup | Take a full snapshot of the current `conversation_log` and `interaction_profile` tables. |
| 2. Assign default party | All existing records receive a `party_id = 'legacy'` (or the first authenticated party's ID). |
| 3. Add constraints | Run `ALTER TABLE` to add `NOT NULL` and foreign key constraints for `party_id`. |
| 4. Index creation | Create indexes on `party_id` for all affected tables. |
| 5. Verify integrity | Run validation queries to ensure no orphaned records and that isolation queries behave correctly. |
| 6. Rollback plan | Keep the backup available for at least one release cycle. |

---

## Appendix A: Relationship to Other Documents

- **`multi_party_continuity_plan.md`** — Implementation blueprint with schema DDL and role definitions. This hardening doc lists the pre-cloud tasks; the plan doc contains the detailed technical design.
- **`docs/future_roadmap.md`** — Lists this hardening work as a completed gate. Further multi-party features (cloud scaling, governance UIs) are tracked there.
- **`GEMINI.md`** — Context‑isolation privacy rules that this hardening effort must satisfy.