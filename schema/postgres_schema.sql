-- PostgreSQL Schema Setup & Privileges Configuration
-- Creates all Janus tables in PostgreSQL and configures role-based access control.

CREATE EXTENSION IF NOT EXISTS vector;

-- 1. Create parties table first due to foreign key references
CREATE TABLE IF NOT EXISTS parties (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'contributor', 'admin', 'observer')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    public_key TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);

-- 2. Create sessions table referencing parties
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    context TEXT DEFAULT '{}'
);

-- 3. Create core_constitution
CREATE TABLE IF NOT EXISTS core_constitution (
    id SERIAL PRIMARY KEY,
    rule_key TEXT UNIQUE NOT NULL,
    rule_text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. Create internal_deliberations
CREATE TABLE IF NOT EXISTS internal_deliberations (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    proposed_action TEXT NOT NULL,
    agent_debate_json TEXT NOT NULL,
    critic_decision INTEGER NOT NULL,
    utility_score REAL NOT NULL,
    justification TEXT NOT NULL
);

-- 5. Create episodic_memory referencing parties and sessions
CREATE TABLE IF NOT EXISTS episodic_memory (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    speaker TEXT NOT NULL,
    message_content TEXT NOT NULL,
    context_type TEXT NOT NULL,
    party_id TEXT REFERENCES parties(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_episodic_memory_party ON episodic_memory(party_id);
CREATE INDEX IF NOT EXISTS idx_episodic_memory_session ON episodic_memory(session_id);

-- 6. Create drive_state
CREATE TABLE IF NOT EXISTS drive_state (
    boredom_counter INTEGER DEFAULT 0,
    curiosity_vector_json TEXT DEFAULT '[]',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 7. Create agent_registry
CREATE TABLE IF NOT EXISTS agent_registry (
    agent_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    target_model TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 8. Create system_config
CREATE TABLE IF NOT EXISTS system_config (
    config_key TEXT PRIMARY KEY,
    config_value TEXT NOT NULL,
    is_agent_modifiable INTEGER DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 9. Create swarm_messages
CREATE TABLE IF NOT EXISTS swarm_messages (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sender_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    message_type TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT DEFAULT 'pending'
);

-- 10. Create agent_rules referencing agent_registry
CREATE TABLE IF NOT EXISTS agent_rules (
    id SERIAL PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agent_registry(agent_id) ON DELETE CASCADE,
    rule_key TEXT UNIQUE NOT NULL,
    rule_text TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 11. Create agent_skills
CREATE TABLE IF NOT EXISTS agent_skills (
    skill_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    parameters_schema TEXT NOT NULL,
    code_blob TEXT NOT NULL,
    entry_point_function TEXT NOT NULL,
    required_role TEXT NOT NULL DEFAULT 'contributor',
    trigger_type TEXT NOT NULL DEFAULT 'manual' CHECK(trigger_type IN ('manual', 'interval', 'event')),
    trigger_config TEXT DEFAULT '{}',
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 12. Create self_model
CREATE TABLE IF NOT EXISTS self_model (
    trait_name TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 0.5 CHECK(value >= 0.0 AND value <= 1.0),
    confidence REAL NOT NULL DEFAULT 0.5 CHECK(confidence >= 0.0 AND confidence <= 1.0),
    is_pinned INTEGER DEFAULT 0 CHECK(is_pinned IN (0, 1)),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 13. Create self_model_history
CREATE TABLE IF NOT EXISTS self_model_history (
    id SERIAL PRIMARY KEY,
    trait_name TEXT NOT NULL,
    old_value REAL,
    new_value REAL,
    old_confidence REAL,
    new_confidence REAL,
    reason TEXT NOT NULL,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 14. Create goals
CREATE TABLE IF NOT EXISTS goals (
    id SERIAL PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN ('short','long','stretch','aspirational')),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','active','in_progress','completed','abandoned','archived','deleted')),
    description TEXT NOT NULL,
    progress_metric TEXT,
    parent_goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 15. Create goal_checkpoints
CREATE TABLE IF NOT EXISTS goal_checkpoints (
    id SERIAL PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    checkpoint_description TEXT NOT NULL,
    achieved INTEGER DEFAULT 0 CHECK(achieved IN (0, 1)),
    achieved_at TIMESTAMP,
    completed_by_party_id TEXT
);

-- 16. Create cognitive_layers
CREATE TABLE IF NOT EXISTS cognitive_layers (
    layer_name TEXT PRIMARY KEY,
    cadence_ms INTEGER NOT NULL,
    is_active INTEGER DEFAULT 1 CHECK(is_active IN (0, 1)),
    last_run_at TIMESTAMP,
    config TEXT DEFAULT '{}'
);

-- 17. Create reflex_rules
CREATE TABLE IF NOT EXISTS reflex_rules (
    id SERIAL PRIMARY KEY,
    trigger_pattern TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    priority INTEGER DEFAULT 0,
    is_enabled INTEGER DEFAULT 1 CHECK(is_enabled IN (0, 1))
);

-- 18. Create external_agents
CREATE TABLE IF NOT EXISTS external_agents (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL CHECK(type IN ('api', 'cli')),
    endpoint TEXT NOT NULL,
    api_key_encrypted TEXT,
    capabilities TEXT,
    is_active INTEGER DEFAULT 1 CHECK(is_active IN (0, 1)),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 19. Create dispatch_log
CREATE TABLE IF NOT EXISTS dispatch_log (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER REFERENCES external_agents(id) ON DELETE SET NULL,
    task_description TEXT NOT NULL,
    prompt_sent TEXT,
    response_received TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'in_progress', 'success', 'failed', 'reviewed')),
    sandbox_session_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

-- 20. Create instincts
CREATE TABLE IF NOT EXISTS instincts (
    id SERIAL PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('schema','tool','constitution','boot','meta')),
    version INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 21. Create spawn_log
CREATE TABLE IF NOT EXISTS spawn_log (
    id SERIAL PRIMARY KEY,
    child_path TEXT NOT NULL UNIQUE,
    child_pid INTEGER,
    status TEXT NOT NULL DEFAULT 'spawning' CHECK(status IN ('spawning','alive','dead','unknown')),
    spawned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_heartbeat TIMESTAMP
);

-- 22. Create memories table (multiparty)
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    namespace TEXT NOT NULL DEFAULT 'global'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_party_memory_key ON memories(party_id, namespace, key);
CREATE INDEX IF NOT EXISTS idx_namespace_key ON memories(namespace, key);

-- 23. Create preferences table
CREATE TABLE IF NOT EXISTS preferences (
    id SERIAL PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    preference_key TEXT NOT NULL,
    preference_value TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(party_id, preference_key)
);

-- 24. Create modifications table
CREATE TABLE IF NOT EXISTS modifications (
    id TEXT PRIMARY KEY,
    initiated_by TEXT NOT NULL REFERENCES parties(id),
    approved_by TEXT REFERENCES parties(id),
    feature TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK(change_type IN ('add', 'modify', 'rollback', 'self_source', 'self_config')),
    change_resource TEXT NOT NULL DEFAULT 'code' CHECK(change_resource IN ('code', 'config_or_memory')),
    diff TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'pending_self_review', 'approved', 'deployed', 'rolled_back')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TIMESTAMP,
    deployed_at TIMESTAMP,
    rolled_back_at TIMESTAMP
);

-- 25. Create feedback_aggregates table
CREATE TABLE IF NOT EXISTS feedback_aggregates (
    id TEXT PRIMARY KEY,
    feature TEXT NOT NULL,
    party_pool TEXT NOT NULL DEFAULT '[]',
    sentiment REAL NOT NULL DEFAULT 0.0,
    keywords TEXT NOT NULL DEFAULT '[]',
    last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 26. Create vector memory table (pgvector)
CREATE TABLE IF NOT EXISTS janus_embeddings (
    collection_name TEXT NOT NULL,
    id TEXT NOT NULL,
    document TEXT,
    metadata JSONB,
    embedding vector,
    PRIMARY KEY (collection_name, id)
);

-- 27. Create janus_documents table
CREATE TABLE IF NOT EXISTS janus_documents (
    id         SERIAL PRIMARY KEY,
    title      TEXT NOT NULL UNIQUE,
    content    TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '[]',
    purpose    TEXT NOT NULL DEFAULT 'memory' CHECK(purpose IN ('memory', 'knowledge')),
    metadata   TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Seed system party
INSERT INTO parties (id, name, role, created_at, last_seen, metadata)
VALUES ('system', 'system', 'observer', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, '{}')
ON CONFLICT (id) DO NOTHING;

-- 28. Setup Postgres roles and schema permissions
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'janus_admin') THEN
        CREATE ROLE janus_admin;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'janus_agent') THEN
        CREATE ROLE janus_agent;
    END IF;
END
$$;

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO janus_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO janus_admin;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO janus_agent;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON core_constitution FROM janus_agent;
GRANT SELECT ON core_constitution TO janus_agent;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO janus_agent;
