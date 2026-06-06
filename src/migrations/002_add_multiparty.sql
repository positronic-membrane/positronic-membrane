-- Migration 002: Multi-Party Continuity Schema
-- Adds tables for parties, sessions, memories, modifications, feedback aggregates,
-- and extends episodic_memory with party_id and session_id for context isolation.

BEGIN TRANSACTION;

-- 1. Create parties table
CREATE TABLE IF NOT EXISTS parties (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'contributor', 'admin', 'observer')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    public_key TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);

-- 2. Create sessions table
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT,
    context TEXT DEFAULT '{}'
);

-- 3. Create memories table
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    namespace TEXT NOT NULL DEFAULT 'global'
);

-- Unique constraint to prevent duplicate key configurations under same party/namespace
CREATE UNIQUE INDEX IF NOT EXISTS idx_party_memory_key ON memories(party_id, namespace, key);
-- Covering index for global lookups (when party_id is irrelevant)
CREATE INDEX IF NOT EXISTS idx_namespace_key ON memories(namespace, key);

-- 3b. Create preferences table
CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    preference_key TEXT NOT NULL,
    preference_value TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(party_id, preference_key)
);

-- 4. Create modifications table (audit trail)
CREATE TABLE IF NOT EXISTS modifications (
    id TEXT PRIMARY KEY,
    initiated_by TEXT NOT NULL REFERENCES parties(id),
    approved_by TEXT REFERENCES parties(id),
    feature TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK(change_type IN ('add', 'modify', 'rollback', 'self_source', 'self_config')),
    change_resource TEXT NOT NULL DEFAULT 'code' CHECK(change_resource IN ('code', 'config_or_memory')),
    diff TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'pending_self_review', 'approved', 'deployed', 'rolled_back')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at TEXT,
    deployed_at TEXT,
    rolled_back_at TEXT
);

-- 5. Create feedback_aggregates table (optional, for pattern detection)
CREATE TABLE IF NOT EXISTS feedback_aggregates (
    id TEXT PRIMARY KEY,
    feature TEXT NOT NULL,
    party_pool TEXT NOT NULL DEFAULT '[]',
    sentiment REAL NOT NULL DEFAULT 0.0,
    keywords TEXT NOT NULL DEFAULT '[]',
    last_updated TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 6. Extend episodic_memory with party_id and session_id
ALTER TABLE episodic_memory ADD COLUMN party_id TEXT REFERENCES parties(id) ON DELETE CASCADE;
ALTER TABLE episodic_memory ADD COLUMN session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL;

-- 7. Create index for efficient party-scoped episodic memory queries
CREATE INDEX IF NOT EXISTS idx_episodic_memory_party ON episodic_memory(party_id);
CREATE INDEX IF NOT EXISTS idx_episodic_memory_session ON episodic_memory(session_id);

-- 8. Seed system party if it doesn't exist
INSERT OR IGNORE INTO parties (id, name, role, created_at, last_seen, metadata)
VALUES ('system', 'system', 'observer', datetime('now'), datetime('now'), '{}');

COMMIT;
