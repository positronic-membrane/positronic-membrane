---
# Multi‑Party Continuity — Implementation Plan (Revised v2)

## 1. Overview
Enable Janus to maintain distinct, persistent relationships with multiple parties, each with their own memory, preferences, modification history, and **isolated chat logs**. This is the foundation for all subsequent roadmap features, and must comply with the context‑isolation privacy rules defined in GEMINI.md.

## 2. Roles & Governance (MVP: Global per Party)

| Role         | Can suggest changes | Can start modifications | Can approve/deploy | Can rollback |
|--------------|--------------------|-------------------------|-------------------|--------------|
| User         | Yes                | No                      | No                | No           |
| Contributor  | Yes                | Yes                     | No                | No           |
| Administrator| Yes                | Yes                     | Yes               | Yes          |
| Observer     | No                 | No                      | No                | No           |

- Observer is *latent* (no external party assigned initially) – reserved for self‑monitoring and future privacy needs.
- Roles are global per party for MVP. A migration path to domain‑scoped permissions is documented in §8.

### Role Bootstrapping (First‑Run Sequence)
When Janus starts for the first time in single‑user CLI mode:
1. The system checks the `parties` table. If empty:
2. Janus prompts the user to create the first party with a secure enrollment key (randomly generated, printed to stdout, or entered at CLI).
3. That first party is automatically assigned the role `administrator`.
4. The administrator can then register additional parties with appropriate roles.
5. This prevents lock‑out: there is exactly one boot sequence that creates the root admin.

For Web UI users: if the `parties` table is empty on page load, the UI redirects to a **Setup Wizard** screen instructing the user to complete bootstrapping in their terminal (or, in a future enhancement, to generate a first enrollment key in‑browser).

## 3. Database Schema (SQLite‑Compatible)

### `parties`
- `id` (TEXT, PK – UUID stored as text)
- `name` (TEXT, UNIQUE)
- `role` (TEXT – one of "user", "contributor", "admin", "observer")
- `created_at` (TEXT – ISO‑8601 timestamp)
- `public_key` (TEXT, nullable – for future auth)

### `sessions`
- `id` (TEXT, PK – UUID stored as text)
- `party_id` (TEXT, FK → parties, ON DELETE CASCADE)
- `started_at` (TEXT – ISO‑8601 timestamp)
- `ended_at` (TEXT, nullable – ISO‑8601 timestamp)
- `context` (JSON – session‑local metadata)

### `memories`
- `id` (TEXT, PK – UUID stored as text)
- `party_id` (TEXT, FK → parties, ON DELETE CASCADE)
- `key` (TEXT – memory identifier)
- `value` (TEXT – JSON encoded)
- `created_at` (TEXT – ISO‑8601 timestamp)
- `updated_at` (TEXT – ISO‑8601 timestamp)
- `namespace` (TEXT – e.g., "global", "feature_x")

**Uniqueness constraint:** `CREATE UNIQUE INDEX idx_party_memory_key ON memories(party_id, namespace, key);`  
**Covering index for global lookups:** `CREATE INDEX idx_namespace_key ON memories(namespace, key);`

### `modifications` (audit trail for feature changes)
- `id` (TEXT, PK – UUID stored as text)
- `initiated_by` (TEXT, FK → parties)
- `approved_by` (TEXT, FK → parties, nullable)
- `feature` (TEXT – e.g., "memory_layer", "agent_orchestrator")
- `change_type` (TEXT – "add", "modify", "rollback")
- `change_resource` (TEXT – e.g., "code" or "config_or_memory") – determines diff format
- `diff` (TEXT – RFC 6902 JSON Patch **or** unified Git diff, depending on `change_resource`)
- `status` (TEXT – "pending", "pending_self_review", "approved", "deployed", "rolled_back")
- `created_at`, `approved_at`, `deployed_at`, `rolled_back_at` (TEXT – ISO‑8601 timestamps, nullable)

**Autonomous auditing handoff:** When `change_type` is `self_source` or `self_config` (i.e., a self‑modification), the status must transition through `pending_self_review` before reaching `approved` or `deployed`. During `pending_self_review`, the Critic agent is invoked to write a deliberation record into `internal_deliberations`. The modification cannot move to `approved` until that deliberation completes.

### `episodic_memory` (existing table — extended)
- (existing columns for message history, background thoughts)
- `party_id` (TEXT, nullable after migration, FK → parties, ON DELETE CASCADE)
- `session_id` (TEXT, nullable after migration, FK → sessions, ON DELETE SET NULL)

**Migration:**  
```sql
ALTER TABLE episodic_memory ADD COLUMN party_id TEXT REFERENCES parties(id) ON DELETE CASCADE;
ALTER TABLE episodic_memory ADD COLUMN session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL;
```
This ensures every chat log and background thought is scoped to a specific party, preventing cross‑party leakage.

### `feedback_aggregates` (optional – for pattern detection)
- `id` (TEXT, PK – UUID stored as text)
- `feature` (TEXT)
- `party_pool` (TEXT – JSON list of party IDs)
- `sentiment` (REAL – aggregated score)
- `keywords` (TEXT – JSON list of key terms)
- `last_updated` (TEXT – ISO‑8601 timestamp)

## 4. Memory Layer
- Namespace isolation: each party gets a `party:{party_id}:` prefix in memory.
- `MemoryOrchestrator` extended to accept an optional `party_id` parameter.
- Fallback to `global` namespace when no party is specified (for backward compatibility with single‑party usage).
- Retrieval: `get_memory(party_id, key)` returns party‑specific value; `get_all_keys(party_id)` lists all keys.
- The workspace prefix `sandbox:` is reserved for git worktree isolation and is **not** used for party memories.

## 5. API Interface
- `POST /party/register` – create a new party (admin only)
- `GET /party/{id}` – get party details and role
- `PUT /party/{id}/role` – change role (admin only)
- `POST /memory` – write memory (scoped to requester's party)
- `GET /memory/{key}` – read memory (scoped)
- `POST /modification` – initiate a modification (contributor/admin)
- `PUT /modification/{id}/approve` – approve for deployment (admin)
- `PUT /modification/{id}/deploy` – deploy to production (admin)
- `PUT /modification/{id}/rollback` – rollback a deployed change (admin)
- `GET /feedback/aggregate` – retrieve aggregated feature signals (any role)

## 6. Rollback Mechanisms
Two distinct mechanisms depending on the resource type:

- **Codebase files:** Apply a unified Git diff (produced by `git diff --no-color`) stored in `modifications.diff`. Rollback by applying the inverse via `git apply -R` or `git revert`. This is the safe, native way to manage source code changes.
- **Configuration / structured memory / JSON state:** Store an RFC 6902 JSON Patch document in `modifications.diff`. Rollback by computing the inverse patch (using a standard RFC 6902 inversion function) and applying it to the affected resource.

Both methods log a new modification record with `change_type: "rollback"` and the inverse diff. The full audit trail is preserved – no destructive deletes.

## 7. Feedback Aggregation
A background process (or triggered by new modifications) examines the `modifications` table and party‑specific memory for correlated patterns:
- Multiple parties independently requesting the same feature → high priority signal.
- A party that frequently initiates rollbacks → flag for admin review.
- Contributor‑common rejection patterns → design principles.

## 8. Future Migration Path (Domain‑Scoped Permissions)
Once the MVP is stable, introduce a `permissions` table:
- `party_id` (TEXT, FK → parties, ON DELETE CASCADE)
- `domain` (TEXT)
- `role_override` (TEXT)
- Query: effective_role = COALESCE(domain_override, global_role)
- Preserve lock‑in: domain overrides are additive, never subtractive from global rights.

## 9. Implementation Phases
1. **Schema & migrations** – create tables in SQLite with TEXT UUIDs, foreign keys, cascade rules, unique indexes on `memories`, and `ALTER TABLE episodic_memory ADD COLUMN party_id/session_id`.
2. **Memory layer** – extend MemoryOrchestrator with party‑scoped methods using `party:` prefix and scoped `episodic_memory` queries.
3. **Role bootstrap** – first‑run ceremony that auto‑creates the root admin party, with web UI fallback for empty‑database detection.
4. **API endpoints** – build REST interface with role middleware.
5. **Modification/rollback system** – dual‑format diff (Git vs. JSON Patch) with autonomous Critic deliberation handoff for self‑modifications.
6. **Feedback aggregation** – pattern detection engine.
7. **Testing & sandbox validation** – run inside the Janus sandbox for safe staging.