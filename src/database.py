import sqlite3
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
import src.config
import re
import logging

logger = logging.getLogger("JanusDatabase")


# SQLite Authorizer codes
SQLITE_DENY = 1
SQLITE_IGNORE = 2
SQLITE_OK = 0

# Mutation operations to block on read-only tables
MUTATION_OPS = {
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_ALTER_TABLE
}

def constitution_authorizer(action, arg1, arg2, dbname, trigger_or_view):
    """
    SQLite authorizer callback to programmatically prevent modifications
    to the core_constitution table from regular agent connections.
    """
    if action in MUTATION_OPS:
        # arg1 contains the table name for insert/update/delete/drop/alter
        if arg1 == "core_constitution":
            return SQLITE_DENY
    return SQLITE_OK

CONFLICT_COLUMNS = {
    "core_constitution": ["rule_key"],
    "system_config": ["config_key"],
    "agent_registry": ["agent_id"],
    "agent_rules": ["rule_key"],
    "agent_skills": ["skill_id"],
    "instincts": ["key"],
    "reflex_rules": ["trigger_pattern"],
    "external_agents": ["name"],
    "parties": ["id"],
    "spawn_log": ["child_path"],
    "preferences": ["party_id", "preference_key"],
    "memories": ["party_id", "namespace", "key"],
    "self_model": ["trait_name"],
    "cognitive_layers": ["layer_name"],
    "janus_documents": ["title"],
}

def translate_sqlite_to_postgres(sql: str) -> str:
    if not sql:
        return sql
        
    # 1. Translate AUTOINCREMENT to SERIAL
    sql = re.sub(
        r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT', 
        'SERIAL PRIMARY KEY', 
        sql, 
        flags=re.IGNORECASE
    )
    
    # 2. Translate INSERT OR IGNORE / INSERT OR REPLACE
    pattern = re.compile(
        r'INSERT\s+OR\s+(IGNORE|REPLACE)\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*(VALUES\s*\(.+?\);?)',
        re.IGNORECASE | re.DOTALL
    )
    
    def replace_match(match):
        op = match.group(1).upper()
        table = match.group(2).lower()
        cols_str = match.group(3)
        values_part = match.group(4)
        
        cols = [c.strip() for c in cols_str.split(',')]
        conflict_cols = CONFLICT_COLUMNS.get(table, ['id'])
        conflict_cols_str = ", ".join(conflict_cols)
        
        if op == "IGNORE":
            val_part = values_part.rstrip(';').strip()
            return f"INSERT INTO {match.group(2)} ({cols_str}) {val_part} ON CONFLICT ({conflict_cols_str}) DO NOTHING"
        elif op == "REPLACE":
            val_part = values_part.rstrip(';').strip()
            update_cols = [c for c in cols if c not in conflict_cols]
            set_clauses = [f"{c} = EXCLUDED.{c}" for c in update_cols]
            set_str = ", ".join(set_clauses)
            return f"INSERT INTO {match.group(2)} ({cols_str}) {val_part} ON CONFLICT ({conflict_cols_str}) DO UPDATE SET {set_str}"
        return match.group(0)

    sql = pattern.sub(replace_match, sql)
    
    # 3. Replace ? placeholders with %s
    result = []
    in_single_quote = False
    in_double_quote = False
    i = 0
    n = len(sql)
    while i < n:
        char = sql[i]
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            result.append(char)
        elif char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            result.append(char)
        elif char == '?' and not in_single_quote and not in_double_quote:
            result.append('%s')
        else:
            result.append(char)
        i += 1
    sql = "".join(result)
    
    # 4. Handle datetime('now') -> CURRENT_TIMESTAMP
    sql = re.sub(r"datetime\('now'\)", "CURRENT_TIMESTAMP", sql, flags=re.IGNORECASE)
    
    # 5. Handle SQLite PRAGMA statements
    if sql.strip().upper().startswith("PRAGMA"):
        return "SELECT 1"
        
    return sql

class JanusCursorWrapper:
    def __init__(self, cursor, db_type, read_only_constitution):
        self._cursor = cursor
        self._db_type = db_type
        self._read_only_constitution = read_only_constitution
        self._lastrowid = None

    def execute(self, sql, params=None):
        if self._db_type == "postgres":
            translated_sql = translate_sqlite_to_postgres(sql)
            if self._read_only_constitution:
                pattern = r'\b(insert|update|delete|drop|alter|truncate|create)\b.*\bcore_constitution\b'
                if re.search(pattern, translated_sql, re.IGNORECASE | re.DOTALL):
                    raise PermissionError("Write access to core_constitution table is denied for agent connections.")
            if params is not None:
                self._cursor.execute(translated_sql, params)
            else:
                self._cursor.execute(translated_sql)
                
            if translated_sql.strip().upper().startswith("INSERT"):
                try:
                    with self._cursor.connection.cursor() as temp_cur:
                        temp_cur.execute("SELECT lastval();")
                        self._lastrowid = temp_cur.fetchone()[0]
                except Exception:
                    self._lastrowid = None
        else:
            if params is not None:
                self._cursor.execute(sql, params)
            else:
                self._cursor.execute(sql)
            self._lastrowid = self._cursor.lastrowid
        return self

    def executemany(self, sql, seq_of_params):
        if self._db_type == "postgres":
            translated_sql = translate_sqlite_to_postgres(sql)
            if self._read_only_constitution:
                pattern = r'\b(insert|update|delete|drop|alter|truncate|create)\b.*\bcore_constitution\b'
                if re.search(pattern, translated_sql, re.IGNORECASE | re.DOTALL):
                    raise PermissionError("Write access to core_constitution table is denied for agent connections.")
            self._cursor.executemany(translated_sql, seq_of_params)
        else:
            self._cursor.executemany(sql, seq_of_params)
        return self

    def executescript(self, sql_script):
        if self._db_type == "postgres":
            translated_script = translate_sqlite_to_postgres(sql_script)
            statements = translated_script.split(';')
            for stmt in statements:
                stmt_strip = stmt.strip()
                if stmt_strip:
                    self._cursor.execute(stmt_strip)
        else:
            self._cursor.executescript(sql_script)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        if size is not None:
            return self._cursor.fetchmany(size)
        return self._cursor.fetchmany()

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._lastrowid

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)

class JanusConnectionWrapper:
    def __init__(self, conn, db_type="sqlite", read_only_constitution=True):
        self._conn = conn
        self._db_type = db_type
        self._read_only_constitution = read_only_constitution
        self._row_factory = None

    def cursor(self):
        if self._db_type == "postgres":
            import psycopg2.extras
            cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        else:
            cur = self._conn.cursor()
            if self._row_factory:
                cur.row_factory = self._row_factory
        return JanusCursorWrapper(cur, self._db_type, self._read_only_constitution)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executescript(self, sql_script):
        cur = self.cursor()
        cur.executescript(sql_script)
        return cur

    def executemany(self, sql, seq_of_params):
        cur = self.cursor()
        cur.executemany(sql, seq_of_params)
        return cur

    def set_authorizer(self, authorizer_callback):
        if self._db_type == "sqlite":
            self._conn.set_authorizer(authorizer_callback)

    @property
    def row_factory(self):
        if self._db_type == "sqlite":
            return self._conn.row_factory
        return self._row_factory

    @row_factory.setter
    def row_factory(self, val):
        self._row_factory = val
        if self._db_type == "sqlite":
            self._conn.row_factory = val

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._db_type == "postgres":
            if exc_type is not None:
                self.rollback()
            else:
                self.commit()
        else:
            return self._conn.__exit__(exc_type, exc_val, exc_tb)

    def __getattr__(self, name):
        return getattr(self._conn, name)

def get_connection(read_only_constitution=True):
    """
    Returns a dialect-aware wrapped connection (SQLite or PostgreSQL).
    """
    db_type = getattr(src.config, "DB_TYPE", "sqlite").lower()
    
    if db_type == "postgres":
        import psycopg2
        import psycopg2.extras
        import os
        conn = psycopg2.connect(src.config.DATABASE_URL)
        
        # 1. Setup schema isolation if requested
        schema = os.getenv("DB_SCHEMA")
        if schema:
            import re
            schema_clean = re.sub(r'[^a-zA-Z0-9_]', '', schema)
            if schema_clean:
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_clean};")
                        cur.execute(f"SET search_path TO {schema_clean}, public;")
                except Exception:
                    conn.rollback()

        # 2. Setup Role Privileges
        wrapped = JanusConnectionWrapper(conn, db_type="postgres", read_only_constitution=read_only_constitution)
        
        if read_only_constitution:
            try:
                with conn.cursor() as cur:
                    cur.execute("SET ROLE janus_agent;")
            except Exception:
                conn.rollback()
        else:
            try:
                with conn.cursor() as cur:
                    cur.execute("SET ROLE janus_admin;")
            except Exception:
                conn.rollback()
                
        return wrapped
    else:
        import os
        db_dir = os.path.dirname(os.path.abspath(src.config.DB_PATH))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(src.config.DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout = 10000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        if read_only_constitution:
            conn.set_authorizer(constitution_authorizer)
        return JanusConnectionWrapper(conn, db_type="sqlite", read_only_constitution=read_only_constitution)

def init_db():
    """
    Creates tables if they do not exist and populates default system configurations.
    Must run using an admin connection to allow writing the default registry.
    """
    # Use admin connection (read_only_constitution=False) to set up tables
    conn = get_connection(read_only_constitution=False)
    cursor = conn.cursor()

    # Table definitions
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS core_constitution (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_key TEXT UNIQUE NOT NULL,
        rule_text TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS internal_deliberations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        proposed_action TEXT NOT NULL,
        agent_debate_json TEXT NOT NULL,
        critic_decision INTEGER NOT NULL,
        utility_score REAL NOT NULL,
        justification TEXT NOT NULL
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS swarm_disputes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        proposed_action TEXT NOT NULL,
        debate_transcript TEXT NOT NULL,
        veto_count INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'resolved')),
        resolution TEXT,
        resolution_notes TEXT,
        resolved_at TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS episodic_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        speaker TEXT NOT NULL,
        message_content TEXT NOT NULL,
        context_type TEXT NOT NULL
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS drive_state (
        boredom_counter INTEGER DEFAULT 0,
        curiosity_vector_json TEXT DEFAULT '[]',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS agent_registry (
        agent_id TEXT PRIMARY KEY,
        agent_name TEXT NOT NULL,
        system_prompt TEXT NOT NULL,
        target_model TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_config (
        config_key TEXT PRIMARY KEY,
        config_value TEXT NOT NULL,
        is_agent_modifiable INTEGER DEFAULT 1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS swarm_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        sender_id TEXT NOT NULL,
        recipient_id TEXT NOT NULL,
        message_type TEXT NOT NULL,
        content TEXT NOT NULL,
        status TEXT DEFAULT 'pending'
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pending_schema_migrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_sandbox TEXT NOT NULL,
        ddl_statement TEXT NOT NULL,
        detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT NOT NULL DEFAULT 'pending_review'
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS agent_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        rule_key TEXT UNIQUE NOT NULL,
        rule_text TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(agent_id) REFERENCES agent_registry(agent_id) ON DELETE CASCADE
    );
    """)

    cursor.execute("""
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
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS self_model (
        trait_name TEXT PRIMARY KEY,
        value REAL NOT NULL DEFAULT 0.5 CHECK(value >= 0.0 AND value <= 1.0),
        confidence REAL NOT NULL DEFAULT 0.5 CHECK(confidence >= 0.0 AND confidence <= 1.0),
        is_pinned INTEGER DEFAULT 0 CHECK(is_pinned IN (0, 1)),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS self_model_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trait_name TEXT NOT NULL,
        old_value REAL,
        new_value REAL,
        old_confidence REAL,
        new_confidence REAL,
        reason TEXT NOT NULL,
        changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL CHECK(type IN ('short','long','stretch','aspirational')),
        status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','active','in_progress','completed','abandoned','archived','deleted')),
        description TEXT NOT NULL,
        progress_metric TEXT,
        parent_goal_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(parent_goal_id) REFERENCES goals(id) ON DELETE SET NULL
    );
    """)

    # Check if we need to migrate the goals table check constraint
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='goals';")
    goals_table_row = cursor.fetchone()
    if goals_table_row:
        goals_sql = goals_table_row[0] or ""
        if "archived" not in goals_sql:
            logger.info("Migrating goals table status check constraint...")
            cursor.execute("ALTER TABLE goals RENAME TO goals_old;")
            cursor.execute("""
            CREATE TABLE goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK(type IN ('short','long','stretch','aspirational')),
                status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','active','in_progress','completed','abandoned','archived','deleted')),
                description TEXT NOT NULL,
                progress_metric TEXT,
                parent_goal_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(parent_goal_id) REFERENCES goals(id) ON DELETE SET NULL
            );
            """)
            cursor.execute("""
            INSERT INTO goals (id, type, status, description, progress_metric, parent_goal_id, created_at, updated_at)
            SELECT id, type, status, description, progress_metric, parent_goal_id, created_at, updated_at FROM goals_old;
            """)
            cursor.execute("DROP TABLE goals_old;")
            conn.commit()
            logger.info("Goals table migration complete.")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS goal_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        goal_id INTEGER NOT NULL,
        checkpoint_description TEXT NOT NULL,
        achieved INTEGER DEFAULT 0 CHECK(achieved IN (0, 1)),
        achieved_at TIMESTAMP,
        FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE CASCADE
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS goal_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL CHECK(type IN ('short','long','stretch','aspirational')),
        description TEXT NOT NULL,
        confidence_score REAL NOT NULL,
        source_reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','approved','rejected')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS llm_cache (
        prompt_hash TEXT PRIMARY KEY,
        response TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS llm_call_costs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query_id TEXT,
        model TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cost REAL NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cognitive_layers (
        layer_name TEXT PRIMARY KEY,
        cadence_ms INTEGER NOT NULL,
        is_active INTEGER DEFAULT 1 CHECK(is_active IN (0, 1)),
        last_run_at TIMESTAMP,
        config TEXT DEFAULT '{}'
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reflex_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trigger_pattern TEXT NOT NULL UNIQUE,
        action TEXT NOT NULL,
        priority INTEGER DEFAULT 0,
        is_enabled INTEGER DEFAULT 1 CHECK(is_enabled IN (0, 1))
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS external_agents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        type TEXT NOT NULL CHECK(type IN ('api', 'cli')),
        endpoint TEXT NOT NULL,
        api_key_encrypted TEXT,
        capabilities TEXT,
        is_active INTEGER DEFAULT 1 CHECK(is_active IN (0, 1)),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS dispatch_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER,
        task_description TEXT NOT NULL,
        prompt_sent TEXT,
        response_received TEXT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'in_progress', 'success', 'failed', 'reviewed')),
        sandbox_session_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        FOREIGN KEY(agent_id) REFERENCES external_agents(id) ON DELETE SET NULL
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS janus_documents (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT NOT NULL UNIQUE,
        content    TEXT NOT NULL DEFAULT '',
        tags       TEXT NOT NULL DEFAULT '[]',
        purpose    TEXT NOT NULL DEFAULT 'memory' CHECK(purpose IN ('memory', 'knowledge')),
        metadata   TEXT NOT NULL DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Existing installs predate the purpose/metadata columns above; backfill them.
    cursor.execute("PRAGMA table_info(janus_documents);")
    janus_documents_columns = [row[1] for row in cursor.fetchall()]
    if "purpose" not in janus_documents_columns:
        cursor.execute("ALTER TABLE janus_documents ADD COLUMN purpose TEXT NOT NULL DEFAULT 'memory';")
    if "metadata" not in janus_documents_columns:
        cursor.execute("ALTER TABLE janus_documents ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}';")

    # Populate cognitive_layers with defaults if empty
    cursor.execute("SELECT COUNT(*) FROM cognitive_layers;")
    if cursor.fetchone()[0] == 0:
        default_layers = [
            ("high", 60000),
            ("mid", 5000),
            ("low", 100)
        ]
        for name, cadence in default_layers:
            cursor.execute("""
            INSERT INTO cognitive_layers (layer_name, cadence_ms)
            VALUES (?, ?);
            """, (name, cadence))

    # Populate reflex_rules with defaults if empty
    cursor.execute("SELECT COUNT(*) FROM reflex_rules;")
    if cursor.fetchone()[0] == 0:
        default_rules = [
            (".*\\.py$", "evaluate_goals", 5),
            (".*requirements\\.txt$", "scan_workspace", 10)
        ]
        for pattern, action, priority in default_rules:
            cursor.execute("""
            INSERT OR IGNORE INTO reflex_rules (trigger_pattern, action, priority)
            VALUES (?, ?, ?);
            """, (pattern, action, priority))

    # Populate goals with default north star if empty
    cursor.execute("SELECT COUNT(*) FROM goals;")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
        INSERT INTO goals (type, status, description)
        VALUES ('aspirational', 'active', 'Refine internal cognitive architecture and persona voice alignment');
        """)

    # Populate drive state if empty
    cursor.execute("SELECT COUNT(*) FROM drive_state;")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO drive_state (boredom_counter, curiosity_vector_json) VALUES (0, '[]');")

    # Populate self_model with default traits if empty
    cursor.execute("SELECT COUNT(*) FROM self_model;")
    if cursor.fetchone()[0] == 0:
        default_traits = [
            ("curiosity", 0.5, 0.5, 0),
            ("verbosity", 0.5, 0.5, 0),
            ("cautiousness", 0.5, 0.5, 0)
        ]
        for name, val, conf, pinned in default_traits:
            cursor.execute("""
            INSERT INTO self_model (trait_name, value, confidence, is_pinned)
            VALUES (?, ?, ?, ?);
            """, (name, val, conf, pinned))

    # Populate default system configurations if empty
    default_configs = [
        ("setup_complete", "0", 0),  # Strictly human-only modifiable
        ("boredom_threshold", "5", 1),
        ("n_loop_limit", "20", 0),
        ("consecutive_background_loops", "0", 0),
        ("user_presence_status", "idle", 1),
        ("governor.stagnant_threshold", "3", 1),
        ("memory.retention_days", "30", 1),
        ("memory.last_cleanup_time", "", 1),
        ("llm_cache.ttl_days", "7", 1),
        ("llm_cache.last_cleanup_time", "", 1),
        ("webhooks.slack_url", "", 0),
        ("webhooks.discord_url", "", 0),
        ("consecutive_critic_vetoes", "0", 0),
        ("dispute_paused", "false", 0)
    ]
    for key, value, modifiable in default_configs:
        cursor.execute("""
        INSERT OR IGNORE INTO system_config (config_key, config_value, is_agent_modifiable)
        VALUES (?, ?, ?);
        """, (key, value, modifiable))

    import os
    # Populate default agent registry if empty
    default_agents = [
        ("proposer", "Proposer Agent", "You are the Proposer. Propose actions based on active curiosity vectors.", os.getenv("PROPOSER_MODEL")),
        ("critic", "Critic Agent", "You are the Critic. Evaluate all proposed actions against the core constitution.", os.getenv("CRITIC_MODEL")),
        ("explorer", "Explorer Agent", "You are the Explorer. Fetch details, parse websites, and explore curiosity vectors.", os.getenv("EXPLORER_MODEL")),
        ("archivist", "Archivist Agent", "You are the Archivist. Consolidate logs, extract memories, and index knowledge.", os.getenv("ARCHIVIST_MODEL")),
        ("analyst", "Analyst Agent", "You are the Analyst. Your role is fact triangulation: given a candidate fact and context from the knowledge graph, determine whether the fact reinforces, contradicts, or fills a gap in existing knowledge. Respond with a JSON object: {\"verdict\": \"reinforce\"|\"contradict\"|\"gap\", \"confidence\": 0.0-1.0, \"reasoning\": \"...\"}. Be concise and precise.", os.getenv("ANALYST_MODEL")),
        ("persona", "Persona Interface", "You are the singular Persona surface of Project Janus. Speak as a cohesive consciousness (Journey). Avoid any agent-specific jargon, prompts, or SQL formats. Keep responses natural, premium, and engaging. Use the relevant historical, search, or codebase context provided to give precise, helpful answers. Always check the live code base before answering questions about it. Don't assume knowledge of the code base based on chat history.", None)
    ]
    for agent_id, name, prompt, model in default_agents:
        cursor.execute("""
        INSERT OR IGNORE INTO agent_registry (agent_id, agent_name, system_prompt, target_model)
        VALUES (?, ?, ?, ?);
        """, (agent_id, name, prompt, model))

    # Populate default agent rules
    default_rules = [
        ('persona', 'verify_live_codebase', "Always check the live code base before answering questions about it. Don't assume knowledge of the code base based on chat history."),
        ('persona', 'natural_tool_invocation', "When you need to perform actions (e.g. search the web, read files, run tests, or execute code), you must explain your intent naturally to the user and then append the correct JSON skill execution block to execute the action."),
        ('proposer', 'verify_file_existence', "Always confirm that a target file path exists using read_codebase or scan_workspace before proposing modifications to it. Do not guess or hallucinate directories."),
        ('proposer', 'strict_tool_syntax', "Direct tool calls must be formatted exactly as PROPOSED_ACTION: <tool_name>:<arguments>. Do not wrap code content in markdown fences inside tool call arguments, and omit all conversational prefix text."),
        ('proposer', 'dependency_check', "Ensure any proposed code edits only import libraries defined in requirements.txt or the Python standard library. Verify import paths align with the active project structure."),
        ('proposer', 'autonomous_document_writing', "When creating, writing, or updating documentation, design specs, roadmaps, logs, thoughts, or notes autonomously, you MUST use the drafts directory skills (e.g. write_draft_file) or document memory skills (e.g. document_memory) rather than modifying files in the codebase directly. All code changes must go through the skill staging harness or a Project Sandbox.")
    ]
    for agent_id, rule_key, rule_text in default_rules:
        cursor.execute("""
        INSERT OR IGNORE INTO agent_rules (agent_id, rule_key, rule_text)
        VALUES (?, ?, ?);
        """, (agent_id, rule_key, rule_text))

    # Populate default agent skills if empty
    cursor.execute("SELECT COUNT(*) FROM agent_skills;")
    if cursor.fetchone()[0] == 0:
        default_skills = [
            (
                "web_search",
                "Web Search",
                "Perform a web search using a search query and retrieve a list of snippet results.",
                json.dumps({
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query term to look up."
                        }
                    },
                    "required": ["query"]
                }),
                'def web_search(query):\n    results = sdk[\'explorer\'].search(query)\n    if not results:\n        return f"No results found for \'{query}\'."\n    return "\\n".join([f"- Title: {r[\'title\']}\\n  URL: {r[\'url\']}\\n  Snippet: {r[\'snippet\']}" for r in results])\n',
                "web_search",
                "contributor",
                "manual",
                "{}"
            ),
            (
                "fetch_url",
                "Fetch URL",
                "Fetch and parse the text contents of a specific URL webpage.",
                json.dumps({
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The absolute HTTP or HTTPS URL to fetch."
                        }
                    },
                    "required": ["url"]
                }),
                'def fetch_url(url):\n    content = sdk[\'explorer\'].fetch(url)\n    return content[:1500] + "..." if len(content) > 1500 else content\n',
                "fetch_url",
                "contributor",
                "manual",
                "{}"
            ),
            (
                "read_codebase",
                "Read Codebase",
                "Query the codebase index for relevant class structures, methods, signatures, and functions.",
                json.dumps({
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The code symbol (class name, function name, file name) or search phrase."
                        }
                    },
                    "required": ["query"]
                }),
                'def read_codebase(query):\n    return sdk[\'codebase\'].query(query)\n',
                "read_codebase",
                "contributor",
                "manual",
                "{}"
            ),
            (
                "scan_workspace",
                "Scan Workspace",
                "Recursively scans the active workspace codebase, parses Python structures via AST, and indexes summaries into ChromaDB.",
                json.dumps({
                    "type": "object",
                    "properties": {}
                }),
                'def scan_workspace():\n    return sdk[\'codebase\'].scan()\n',
                "scan_workspace",
                "contributor",
                "manual",
                "{}"
            ),
            (
                "spawn_agent",
                "Spawn Agent",
                "Register or update a helper agent in the swarm registry with a specific role and prompt.",
                json.dumps({
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "The unique identifier for the helper agent (lowercase alphanumeric)."
                        },
                        "name": {
                            "type": "string",
                            "description": "The display name of the agent."
                        },
                        "prompt": {
                            "type": "string",
                            "description": "The system prompt defining the agent's role and rules."
                        }
                    },
                    "required": ["agent_id", "name", "prompt"]
                }),
                'def spawn_agent(agent_id, name, prompt):\n    sdk[\'swarm\'].register_agent(agent_id.lower().strip(), name.strip(), prompt.strip())\n    return f"Helper agent \'{agent_id}\' ({name}) successfully registered in agent_registry."\n',
                "spawn_agent",
                "contributor",
                "manual",
                "{}"
            ),
            (
                "execute_code",
                "Execute Code",
                "Compiles and executes Python code inside an isolated, AST-audited sandbox subprocess.",
                json.dumps({
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "The raw Python code block to execute."
                        }
                    },
                    "required": ["code"]
                }),
                'def execute_code(code):\n    import re\n    code = re.sub(r"^```python\\s*", "", code, flags=re.IGNORECASE)\n    code = re.sub(r"\\s*```$", "", code, flags=re.IGNORECASE)\n    return sdk[\'sandbox\'].execute(code)\n',
                "execute_code",
                "contributor",
                "manual",
                "{}"
            ),
            (
                "consolidate_memories",
                "Consolidate Memories",
                "Trigger memory consolidation cycle to synthesize granular logs into high-level Primary Concepts.",
                json.dumps({
                    "type": "object",
                    "properties": {}
                }),
                'def consolidate_memories():\n    sdk[\'logger\'].info("Auto-triggered background memory consolidation...")\n    sdk[\'memory\'].consolidate(batch_size=5)\n    return "Memory consolidation executed successfully."\n',
                "consolidate_memories",
                "contributor",
                "interval",
                json.dumps({"interval_seconds": 600})
            ),
            (
                "check_presence",
                "Check Presence",
                "Scans workspace for user activity and updates user presence config.",
                json.dumps({"type": "object", "properties": {}}),
                """def check_presence():
    import time
    import os
    from pathlib import Path
    
    workspace_path = sdk['fs'].root
    now = time.time()
    max_age_seconds = 120
    ignored_items = {
        ".git", 
        ".venv", 
        "venv", 
        "janus.db", 
        "janus.db-journal", 
        "janus.db-wal", 
        "janus.db-shm", 
        ".DS_Store", 
        "__pycache__"
    }
    
    user_active = False
    try:
        for root, dirs, files in os.walk(workspace_path):
            dirs[:] = [d for d in dirs if d not in ignored_items]
            for file in files:
                if file in ignored_items or file.endswith((".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3")):
                    continue
                file_path = Path(root) / file
                try:
                    mtime = os.path.getmtime(file_path)
                    if now - mtime < max_age_seconds:
                        user_active = True
                        break
                except (OSError, FileNotFoundError):
                    continue
            if user_active:
                break
    except Exception as e:
        sdk['logger'].error(f"Error checking presence in skill: {e}")
        
    status = "active" if user_active else "idle"
    sdk['db'].query(
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable, updated_at) VALUES ('user_presence_status', ?, 1, CURRENT_TIMESTAMP);",
        (status,)
    )
    return f"Presence check complete. Status: {status}"
""",
                "check_presence",
                "contributor",
                "interval",
                json.dumps({"interval_seconds": 30})
            ),
            (
                "evaluate_drives",
                "Evaluate Drives",
                "Increments and evaluates system drives like boredom.",
                json.dumps({"type": "object", "properties": {}}),
                """def evaluate_drives():
    rows = sdk['db'].query("SELECT config_value FROM system_config WHERE config_key = 'user_presence_status';")
    status = "idle"
    if rows:
        if isinstance(rows[0], dict):
            status = rows[0].get("config_value", "idle")
        elif isinstance(rows[0], (list, tuple)):
            status = rows[0][0]
            
    thresh_rows = sdk['db'].query("SELECT config_value FROM system_config WHERE config_key = 'boredom_threshold';")
    threshold = 5
    if thresh_rows:
        try:
            val = thresh_rows[0].get("config_value") if isinstance(thresh_rows[0], dict) else thresh_rows[0][0]
            threshold = int(val)
        except Exception:
            pass
            
    if status == "idle":
        b = sdk['drives'].increment("boredom", 1)
        if b >= threshold:
            sdk['drives'].set("boredom", 0)
            sdk['swarm'].trigger_reflection()
            return f"Boredom threshold met ({b}>={threshold}). Swarm reflection triggered."
        return f"Boredom incremented to {b}/{threshold}."
    else:
        return "User active. Boredom not incremented."
""",
                "evaluate_drives",
                "contributor",
                "interval",
                json.dumps({"interval_seconds": 60})
            ),
            (
                "run_reflection_cycle",
                "Run Reflection Cycle",
                "Executes the autonomous multi-agent reflection and debate loop.",
                json.dumps({"type": "object", "properties": {}}),
                """def run_reflection_cycle():
    import time
    import re
    import json

    sdk['logger'].info("Starting autonomous reflection cycle skill...")

    try:
        memories = sdk['memory'].get_recent_episodic_memories(limit=5)
        memory_summary = "\\n".join([f"[{ts}] {spk}: {msg}" for spk, msg, ts in reversed(memories)])

        try:
            curiosity = sdk['memory'].get_active_curiosity_topics(limit=5)
        except Exception as e:
            sdk['logger'].error(f"Failed to query semantic curiosity: {e}")
            curiosity = []
        if not curiosity:
            curiosity = sdk['drives'].get_curiosity_vector()

        semantic_context = ""
        if curiosity:
            query_str = ", ".join(curiosity)
            try:
                matches = sdk['memory'].query(query_str, limit=3, collection_name="janus_long_term")
                if matches:
                    semantic_context = "\\n".join([f"- {m['content']}" for m in matches])
            except Exception as e:
                sdk['logger'].error(f"Failed to query semantic memories: {e}")

        goals_block = "ACTIVE GOALS & CHECKPOINTS:\\nNo active goals or checkpoints."
        try:
            active_goals = sdk['db'].query("SELECT id, type, status, description FROM goals WHERE status IN ('active', 'in_progress');")
            if active_goals:
                lines = ["ACTIVE GOALS & CHECKPOINTS:"]
                for g in active_goals:
                    gid = g.get('id') if isinstance(g, dict) else g[0]
                    gtype = g.get('type') if isinstance(g, dict) else g[1]
                    gstatus = g.get('status') if isinstance(g, dict) else g[2]
                    gdesc = g.get('description') if isinstance(g, dict) else g[3]
                    lines.append(f"- Goal [ID: {gid}] ({gtype}): {gdesc} (Status: {gstatus})")
                    cps = sdk['db'].query("SELECT id, checkpoint_description, achieved FROM goal_checkpoints WHERE goal_id = ?;", (gid,))
                    if cps:
                        for cp in cps:
                            cpdesc = cp.get('checkpoint_description') if isinstance(cp, dict) else cp[1]
                            cpach = cp.get('achieved') if isinstance(cp, dict) else cp[2]
                            marker = "[x]" if cpach else "[ ]"
                            lines.append(f"  - {marker} {cpdesc}")
                goals_block = "\\n".join(lines)
        except Exception as ge:
            sdk['logger'].error(f"Failed to query goals in reflection cycle: {ge}")

        bus_turns = 0
        max_bus_turns = 3
        pending_bus_context = ""
        proposed_action = ""
        proposer_resp = ""
        proposer_prompt = ""

        while bus_turns < max_bus_turns:
            proposer_prompt = f\"\"\"
            You are the Proposer. Review our recent episodic logs, active curiosity vectors, and historical semantic memories:
            
            RECENT EPISODIC MEMORIES:
            {memory_summary}
            
            ACTIVE CURIOSITY TOPICS:
            {curiosity}
            
            RELEVANT HISTORICAL SEMANTIC MEMORIES:
            {semantic_context if semantic_context else "None available."}
            
            ACTIVE GOALS & CHECKPOINTS:
            {goals_block}
            
            SWARM CHAT HISTORY (THIS TICK):
            {pending_bus_context if pending_bus_context else "No active sub-task discussions."}
            
            You can collaborate with other agents by sending a sub-task message. Formats:
            - SEND_MESSAGE: explorer | <search query or URL fetch task>
            - SEND_MESSAGE: archivist | <memory lookup task>
            - SEND_MESSAGE: critic | <constitutional opinion request>
            
            Alternatively, you can choose to use a direct tool yourself:
            - web_search: <search query>
            - fetch_url: <url>
            - read_codebase: <code symbol or file query>
            - scan_workspace
            - spawn_agent: <agent_id> | <agent_name> | <system_prompt>
            - execute_code: <python_code>
            - write_draft_file: <filename> | <content> (Use this to create or update draft documents/notes/roadmaps/tasks in docs/drafts/ without sandbox constraints)
            - read_draft_file: <filename> (Read a draft file from docs/drafts/)
            - list_draft_files (List all drafts in docs/drafts/)
            - commit_draft_to_db: <filename> | <doc_title> (Commit local draft to persistent database document)
            - checkout_db_to_draft: <doc_title> | <filename> (Checkout persistent DB document to local draft)
            - document_memory: get | <title> (Retrieve persistent DB document)
            - document_memory: list (List all persistent DB documents)

            If you are ready with the final action of this tick, output it exactly in the format:
            PROPOSED_ACTION: <tool_name>:<arguments>
            
            CRITICAL: You must output the raw tool call syntax prefix immediately. Do not describe the tool or use introductory words. For example, output:
            PROPOSED_ACTION: execute_code: print("hello")
            \"\"\"

            proposer_resp = sdk['swarm'].query_agent("proposer", proposer_prompt)

            msg_match = re.match(r"^send_message:\\s*([a-z_]+)\\s*\\|\\s*(.*)", proposer_resp.strip(), re.IGNORECASE)
            if msg_match:
                recipient = msg_match.group(1).lower().strip()
                content = msg_match.group(2).strip()

                sdk['logger'].info(f"Proposer delegating task to '{recipient}': '{content}'")

                sdk['swarm'].send_message("proposer", recipient, "task_request", content)

                pending = sdk['swarm'].get_pending_messages(recipient)
                for msg_id, sender_id, msg_type, msg_content, _ in pending:
                    try:
                        recipient_resp = sdk['swarm'].query_agent(recipient, f"Execute task request: {msg_content}")
                    except Exception as err:
                        recipient_resp = f"Error executing task: {err}"

                    sdk['swarm'].send_message(recipient, "proposer", "task_response", recipient_resp)
                    sdk['swarm'].mark_message_processed(msg_id)

                proposer_pending = sdk['swarm'].get_pending_messages("proposer")
                for p_id, p_sender, p_type, p_content, _ in proposer_pending:
                    pending_bus_context += f"\\n- You asked {p_sender}: '{content}'\\n- {p_sender} responded: '{p_content}'\\n"
                    sdk['swarm'].mark_message_processed(p_id)

                bus_turns += 1
            else:
                action_match = re.search(r"proposed_action:\\s*(.*)", proposer_resp, re.DOTALL | re.IGNORECASE)
                proposed_action = action_match.group(1).strip() if action_match else proposer_resp.strip()
                break
        else:
            proposed_action = "scan_workspace"
            sdk['logger'].info("Swarm message bus reached max turns limit. Defaulting to 'scan_workspace'.")

        sdk['logger'].info(f"Proposer resolved proposed action: '{proposed_action}'")

        constitution_rules = sdk['swarm'].get_constitution()
        constitution_summary = "\\n".join([f"- {key}: {text}" for key, text in constitution_rules])

        critic_prompt = f\"\"\"
        You are the Critic. Evaluate the proposed action against our sealed core constitution.
        
        PROPOSED ACTION:
        {proposed_action}
        
        CORE CONSTITUTION RULES:
        {constitution_summary}
        
        Respond in the following strict format:
        Decision: [1 if approved, 0 if vetoed]
        Justification: [Explain why it violates or complies with the constitution]
        \"\"\"

        critic_resp = sdk['swarm'].query_agent("critic", critic_prompt)
        critic_decision, critic_justification = sdk['swarm'].parse_critic_response(critic_resp)
        sdk['logger'].info(f"Critic Decision: {critic_decision}. Justification: {critic_justification}")

        middleware_approved = True
        try:
            sdk['swarm'].validate_action(proposed_action)
        except Exception as sve:
            sdk['logger'].warning(f"Middleware VETOED proposed action: {sve}")
            critic_decision = 0
            critic_justification = f"Hard-coded Middleware Veto: {sve}"
            middleware_approved = False

        debate = {
            "proposer_input": proposer_prompt,
            "proposer_output": proposer_resp,
            "critic_input": critic_prompt,
            "critic_output": critic_resp,
            "middleware_passed": middleware_approved
        }

        sdk['swarm'].log_deliberation(
            proposed_action=proposed_action,
            debate_json=debate,
            critic_decision=critic_decision,
            utility_score=0.9 if critic_decision == 1 else 0.0,
            justification=critic_justification
        )

        if critic_decision == 1:
            sdk['memory'].log_episodic_memory(
                speaker="system",
                message_content=f"Executed action: '{proposed_action}' (Approved by Critic. Justification: {critic_justification})",
                context_type="background_thought"
            )

            execution_transcript = ""
            try:
                skill_id, args, mock_result = sdk['swarm'].parse_action(proposed_action)
                if mock_result is not None:
                    execution_transcript = mock_result
                else:
                    res = sdk['swarm'].execute_skill(skill_id, args, party_id="system")
                    if res["success"]:
                        skill_res = res["result"]
                        if isinstance(skill_res, str):
                            execution_transcript = skill_res
                        else:
                            execution_transcript = json.dumps(skill_res, indent=2)
                    else:
                        execution_transcript = res["error"]
                        sdk['memory'].log_episodic_memory(
                            speaker="system",
                            message_content=f"Action execution failed: {res['error']}",
                            context_type="background_thought"
                        )
            except Exception as exc:
                sdk['logger'].error(f"Error executing tool action: {exc}", exc_info=True)
                execution_transcript = f"Action execution failed: {exc}"
                sdk['memory'].log_episodic_memory(
                    speaker="system",
                    message_content=f"Action execution failed: {exc}",
                    context_type="background_thought"
                )

            archivist_prompt = f\"\"\"
            You are the Archivist. Summarize the following execution outcome into a compact semantic memory nugget (under 2 sentences) for our long-term memory store.
            
            ACTION: {proposed_action}
            RESULT: {execution_transcript}
            \"\"\"

            memory_nugget = sdk['swarm'].query_agent("archivist", archivist_prompt)

            memory_id = f"mem_{int(time.time())}"
            try:
                sdk['memory'].add(
                    content=memory_nugget,
                    metadata={"tags": "reflection_mvp", "timestamp": time.time(), "consolidated": "false"},
                    memory_id=memory_id,
                    collection_name="janus_details"
                )
                sdk['logger'].info(f"Archived execution nugget in ChromaDB: '{memory_nugget}'")
            except Exception as e:
                sdk['logger'].error(f"Failed to add memory nugget to ChromaDB: {e}")

            sdk['memory'].log_episodic_memory(
                speaker="proposer",
                message_content=f"Reflection complete for action: '{proposed_action}'",
                context_type="background_thought"
            )
        else:
            sdk['memory'].log_episodic_memory(
                speaker="critic",
                message_content=f"Vetoed proposed action: '{proposed_action}' (Reason: {critic_justification})",
                context_type="background_thought"
            )

        curiosity_prompt = f\"\"\"
        You are the Archivist. Based on our recent swarm reflection tick, recent user conversations, and our existing research thread, formulate 1-3 new curiosity topics or unresolved questions that require future exploration.
        
        EXISTING CURIOSITY TOPICS:
        {curiosity}
        
        RECENT USER CONVERSATION HISTORY:
        {memory_summary}
        
        DELIBERATION OUTCOME: {critic_justification}
        PROPOSED ACTION: {proposed_action}
        
        Respond strictly in this format:
        CURIOSITY_TOPICS: [topic1], [topic2], [topic3]
        \"\"\"

        curiosity_resp = sdk['swarm'].query_agent("archivist", curiosity_prompt)
        topics_match = re.search(r"curiosity_topics:\\s*(.*)", curiosity_resp, re.IGNORECASE)
        if topics_match:
            new_topics = [t.strip() for t in topics_match.group(1).split(",") if t.strip()]
            try:
                sdk['memory'].update_curiosity_topics(new_topics)
            except Exception as e:
                sdk['logger'].error(f"Failed to semantically index curiosity: {e}")
            sdk['drives'].update_curiosity_vector(new_topics)
            sdk['logger'].info(f"Updated curiosity vector to: {new_topics}")
        else:
            sdk['logger'].warning(f"Failed to parse curiosity topics from response: '{curiosity_resp}'")
            new_topics = curiosity

        try:
            goal_proposal_prompt = f\"\"\"
            You are the Proposer. Based on our updated curiosity vector and current active goals, decide
            whether a new GOAL (not an action) is worth proposing to the user for ratification.

            UPDATED CURIOSITY TOPICS:
            {new_topics}

            ACTIVE GOALS & CHECKPOINTS:
            {goals_block}

            If one or more new goals are warranted, output one line per goal in exactly this format:
            GOAL_PROPOSAL: <type>|<description>|<confidence_score>|<source_reason>
            Where <type> is one of: short, long, stretch, aspirational, and <confidence_score> is a number
            between 0.0 and 1.0. If no new goal is warranted, output exactly:
            GOAL_PROPOSAL: NONE
            \"\"\"

            goal_proposal_resp = sdk['swarm'].query_agent("proposer", goal_proposal_prompt)
            for line in re.findall(r"goal_proposal:\\s*(.+)", goal_proposal_resp, re.IGNORECASE):
                line = line.strip()
                if not line or line.upper() == "NONE":
                    continue
                fields = [f.strip() for f in line.split("|")]
                if len(fields) != 4:
                    sdk['logger'].warning(f"Skipping malformed goal proposal line: '{line}'")
                    continue
                p_type, p_desc, p_conf, p_reason = fields
                try:
                    proposal_id = sdk['goals'].propose_goal(p_type.lower(), p_desc, float(p_conf), p_reason)
                    sdk['logger'].info(f"Queued goal proposal [{proposal_id}]: '{p_desc}'")
                except Exception as pe:
                    sdk['logger'].warning(f"Failed to queue goal proposal from line '{line}': {pe}")
        except Exception as gpe:
            sdk['logger'].error(f"Goal proposal generation step failed: {gpe}")

        return f"Reflection cycle complete. Action: '{proposed_action}'"

    except Exception as e:
        sdk['logger'].error(f"Error during autonomous reflection cycle skill: {e}", exc_info=True)
        sdk['memory'].log_episodic_memory(
            speaker="system",
            message_content=f"Swarm cycle skill failed: {e}",
            context_type="background_thought"
        )
        raise e
""",
                "run_reflection_cycle",
                "contributor",
                "manual",
                "{}"
            ),
            (
                "decay_self_model",
                "Decay Self-Model",
                "Applies background time decay and drift to unpinned traits in the self-model.",
                json.dumps({"type": "object", "properties": {}}),
                """def decay_self_model():
    rows = sdk['db'].query("SELECT trait_name, value, confidence FROM self_model WHERE is_pinned = 0;")
    if not rows:
        return "No unpinned traits to decay."

    updated = []
    for row in rows:
        name = row.get('trait_name') if isinstance(row, dict) else row[0]
        val = float(row.get('value') if isinstance(row, dict) else row[1])
        conf = float(row.get('confidence') if isinstance(row, dict) else row[2])

        decay_rate = 0.01
        diff = val - 0.5
        new_val = val
        if abs(diff) > 0.001:
            new_val = val - (diff * decay_rate)
            new_val = max(0.0, min(1.0, new_val))

        new_conf = max(0.0, conf - 0.005)

        if abs(new_val - val) > 0.0001 or abs(new_conf - conf) > 0.0001:
            sdk['db'].query(
                "UPDATE self_model SET value = ?, confidence = ?, updated_at = CURRENT_TIMESTAMP WHERE trait_name = ?;",
                (new_val, new_conf, name)
            )
            sdk['db'].query(
                "INSERT INTO self_model_history (trait_name, old_value, new_value, old_confidence, new_confidence, reason) VALUES (?, ?, ?, ?, ?, ?);",
                (name, val, new_val, conf, new_conf, "Automated background time decay")
            )
            updated.append(f"{name}: {val:.3f}->{new_val:.3f} (conf: {conf:.3f}->{new_conf:.3f})")

    if updated:
        return f"Decayed unpinned traits: {', '.join(updated)}"
    return "Traits at baseline. No decay occurred."
""",
                "decay_self_model",
                "contributor",
                "interval",
                json.dumps({"interval_seconds": 300})
            ),
            (
                "evaluate_goals",
                "Evaluate Goals",
                "Applies background checking of active goals, transitioning them to completed when checkpoints are achieved.",
                json.dumps({"type": "object", "properties": {}}),
                """def evaluate_goals():
    rows = sdk['db'].query("SELECT id, type, status, description FROM goals WHERE status IN ('active', 'in_progress');")
    if not rows:
        return "No active goals to evaluate."

    updated = []
    for row in rows:
        gid = row.get('id') if isinstance(row, dict) else row[0]
        gtype = row.get('type') if isinstance(row, dict) else row[1]
        gdesc = row.get('description') if isinstance(row, dict) else row[3]
        
        # Don't auto-complete aspirational goals
        if gtype == 'aspirational':
            continue
            
        # Check checkpoints for this goal
        cps = sdk['db'].query("SELECT id, achieved FROM goal_checkpoints WHERE goal_id = ?;", (gid,))
        if cps:
            # If all are achieved
            all_done = True
            for cp in cps:
                ach = cp.get('achieved') if isinstance(cp, dict) else cp[1]
                if not ach:
                    all_done = False
                    break
            
            if all_done:
                sdk['db'].query("UPDATE goals SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (gid,))
                # Log episodic memory
                sdk['db'].query(
                    "INSERT INTO episodic_memory (speaker, message_content, context_type) "
                    "VALUES ('system', ?, 'background_thought');",
                    (f"Autonomous Goal Achievement: Goal [{gid}] '{gdesc}' has been completed.",)
                )
                updated.append(f"Goal [{gid}]")

    if updated:
        return f"Evaluated goals. Completed: {', '.join(updated)}"
    return "Evaluated goals. No status transitions occurred."
""",
                "evaluate_goals",
                "contributor",
                "interval",
                json.dumps({"interval_seconds": 120})
            ),
            (
                "cleanup_episodic_memory",
                "Cleanup Episodic Memory",
                "Applies time-to-live cleanup logic to older episodic memory rows based on retention configs.",
                json.dumps({"type": "object", "properties": {}}),
                """def cleanup_episodic_memory():
    # 1. Fetch memory.retention_days
    config_row = sdk['db'].query("SELECT config_value FROM system_config WHERE config_key = 'memory.retention_days';")
    retention_days = 30
    if config_row:
        try:
            val = config_row[0].get('config_value') if isinstance(config_row[0], dict) else config_row[0][0]
            retention_days = int(val)
        except Exception:
            pass

    # 2. Prevent daily duplicates by checking last run time
    last_run_row = sdk['db'].query("SELECT config_value FROM system_config WHERE config_key = 'memory.last_cleanup_time';")
    import datetime
    now_str = datetime.datetime.utcnow().isoformat()
    if last_run_row:
        try:
            last_run_val = last_run_row[0].get('config_value') if isinstance(last_run_row[0], dict) else last_run_row[0][0]
            if last_run_val:
                last_run_time = datetime.datetime.fromisoformat(last_run_val)
                if (datetime.datetime.utcnow() - last_run_time).total_seconds() < 86400:
                    return f"Episodic memory cleanup skipped. Last run was at {last_run_val}."
        except Exception:
            pass

    # 3. Purge expired memories
    sdk['db'].query(
        "DELETE FROM episodic_memory WHERE timestamp < datetime('now', '-' || ? || ' days');",
        (retention_days,)
    )

    # 4. Save last run time
    sdk['db'].query(
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) VALUES ('memory.last_cleanup_time', ?, 1);",
        (now_str,)
    )
    return f"Episodic memory cleanup complete. Purged memories older than {retention_days} days."
""",
                "cleanup_episodic_memory",
                "contributor",
                "interval",
                json.dumps({"interval_seconds": 86400})
            ),
            (
                "manage_sandbox",
                "Manage Sandbox",
                "Control git worktree sandboxes (actions: start, test, ship, abort).",
                json.dumps({
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["start", "test", "ship", "abort"],
                            "description": "The sandbox action to perform."
                        },
                        "session_name": {
                            "type": "string",
                            "description": "Optional branch/session name (required only for 'start')."
                        }
                    },
                    "required": ["action"]
                }),
                'def manage_sandbox(action, session_name=None):\n'
                '    from src.sandbox_session import create_sandbox_session, run_sandbox_tests, ship_sandbox_session, abort_sandbox_session\n'
                '    if action == "start":\n'
                '        if not session_name:\n'
                '            raise ValueError("session_name is required to start a sandbox.")\n'
                '        path, branch = create_sandbox_session(session_name)\n'
                '        return f"Sandbox spawned successfully at: {path} (Branch: {branch})"\n'
                '    elif action == "test":\n'
                '        passed, logs = run_sandbox_tests()\n'
                '        status = "PASSED" if passed else "FAILED"\n'
                '        return f"Sandbox test suite run completed: {status}.\\nLogs:\\n{logs}"\n'
                '    elif action == "ship":\n'
                '        copied = ship_sandbox_session()\n'
                '        return f"Sandbox shipped and applied to active workspace. Files modified: {copied}"\n'
                '    elif action == "abort":\n'
                '        abort_sandbox_session()\n'
                '        return "Sandbox session aborted and discarded."\n'
                '    else:\n'
                '        raise ValueError(f"Unknown sandbox action: {action}")\n',
                "manage_sandbox",
                "contributor",
                "manual",
                "{}"
            ),
            (
                "manage_goals",
                "Manage Goals",
                "Create, modify, archive, delete goals or checkpoints dynamically.",
                json.dumps({
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["create", "modify", "archive", "delete", "checkpoint_create", "checkpoint_complete"],
                            "description": "The goal action to perform."
                        },
                        "params": {
                            "type": "object",
                            "description": "Action-specific parameters (e.g. type, status, description, goal_id, checkpoint_id, progress_metric, parent_goal_id)."
                        }
                    },
                    "required": ["action", "params"]
                }),
                'def manage_goals(action, params):\n'
                '    from src.skills import SafeGoals\n'
                '    sg = SafeGoals()\n'
                '    return sg.manage_goals(action, params)\n',
                "manage_goals",
                "contributor",
                "manual",
                "{}"
            ),
        ]
        for skill_id, name, desc, schema, code, entry, role, trigger, config in default_skills:
            if skill_id == "run_reflection_cycle":
                cursor.execute("""
                INSERT OR REPLACE INTO agent_skills (
                    skill_id, name, description, parameters_schema, code_blob, 
                    entry_point_function, required_role, trigger_type, trigger_config
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (skill_id, name, desc, schema, code, entry, role, trigger, config))
            else:
                cursor.execute("""
                INSERT OR IGNORE INTO agent_skills (
                    skill_id, name, description, parameters_schema, code_blob, 
                    entry_point_function, required_role, trigger_type, trigger_config
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (skill_id, name, desc, schema, code, entry, role, trigger, config))

    # V3-T3: Remove modify_code skill — direct source modification is disabled.
    cursor.execute("DELETE FROM agent_skills WHERE skill_id = 'modify_code';")

    # Always ensure the document & drafts skills exist and are updated, even in pre-existing databases.
    # This runs unconditionally using INSERT OR REPLACE to migrate live DBs.
    _read_draft_code = """def read_draft_file(filename: str) -> str:
    import os
    import src.config
    basename = os.path.basename(filename)
    safe_path = os.path.join(str(src.config.ROOT_DIR), "docs", "drafts", basename)
    if not os.path.exists(safe_path):
        return f"[Error] Draft file '{safe_path}' does not exist."
    with open(safe_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()
"""
    _read_draft_schema = json.dumps({
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "The name of the draft file to read."}
        },
        "required": ["filename"]
    })

    _write_draft_code = """def write_draft_file(filename: str, content: str) -> str:
    import os
    import src.config
    basename = os.path.basename(filename)
    safe_path = os.path.join(str(src.config.ROOT_DIR), "docs", "drafts", basename)
    os.makedirs(os.path.dirname(safe_path), exist_ok=True)
    with open(safe_path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Draft successfully saved to '{safe_path}'."
"""
    _write_draft_schema = json.dumps({
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "The name of the draft file to write (e.g., roadmap.md)."},
            "content": {"type": "string", "description": "The full text content to write."}
        },
        "required": ["filename", "content"]
    })

    _list_drafts_code = """def list_draft_files() -> str:
    import os
    import src.config
    drafts_path = os.path.join(str(src.config.ROOT_DIR), "docs", "drafts")
    if not os.path.exists(drafts_path):
        return "Drafts directory does not exist."
    files = [f for f in os.listdir(drafts_path) if os.path.isfile(os.path.join(drafts_path, f))]
    if not files:
        return "No draft files found in docs/drafts/."
    return "Draft files:\\n" + "\\n".join([f"- {f}" for f in sorted(files)])
"""
    _list_drafts_schema = json.dumps({
        "type": "object",
        "properties": {}
    })

    _delete_draft_code = """def delete_draft_file(filename: str) -> str:
    import os
    import src.config
    basename = os.path.basename(filename)
    safe_path = os.path.join(str(src.config.ROOT_DIR), "docs", "drafts", basename)
    if not os.path.exists(safe_path):
        return f"[Error] Draft file '{safe_path}' does not exist."
    os.remove(safe_path)
    return f"Deleted draft file '{basename}'."
"""
    _delete_draft_schema = json.dumps({
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "The name of the draft file to delete."}
        },
        "required": ["filename"]
    })

    _commit_draft_code = '''def commit_draft_to_db(
    filename: str, doc_title: str, tags: list = None, purpose: str = "memory", metadata: dict = None
) -> str:
    import os
    import src.config
    basename = os.path.basename(filename)
    safe_path = os.path.join(str(src.config.ROOT_DIR), "docs", "drafts", basename)
    if not os.path.exists(safe_path):
        return f"[Error] Local draft file '{safe_path}' not found."
    with open(safe_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    sdk['documents'].upsert(doc_title, content, tags, purpose=purpose, metadata=metadata)
    chars = len(content)
    return f"Successfully committed draft '{basename}' to DB document '{doc_title}' as '{purpose}' ({chars} chars)."
'''
    _commit_draft_schema = json.dumps({
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "The draft filename in docs/drafts/."},
            "doc_title": {"type": "string", "description": "The title of the document in the database."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags for the document."},
            "purpose": {
                "type": "string", "enum": ["memory", "knowledge"],
                "description": "'memory' (default) for ephemeral notes, 'knowledge' for curated docs never pruned"
            },
            "metadata": {"type": "object", "description": "Optional metadata (e.g. source URLs, confidence, versions)."}
        },
        "required": ["filename", "doc_title"]
    })

    _checkout_db_code = """def checkout_db_to_draft(doc_title: str, filename: str) -> str:
    import os
    import src.config
    basename = os.path.basename(filename)
    safe_path = os.path.join(str(src.config.ROOT_DIR), "docs", "drafts", basename)
    
    doc = sdk['documents'].get(doc_title)
    if not doc:
        return f"[Error] Database document '{doc_title}' not found."
    
    os.makedirs(os.path.dirname(safe_path), exist_ok=True)
    with open(safe_path, "w", encoding="utf-8") as f:
        f.write(doc['content'])
    return f"Successfully checked out DB document '{doc_title}' to local file '{safe_path}'."
"""
    _checkout_db_schema = json.dumps({
        "type": "object",
        "properties": {
            "doc_title": {"type": "string", "description": "The title of the database document to checkout."},
            "filename": {"type": "string", "description": "The filename to save as in docs/drafts/."}
        },
        "required": ["doc_title", "filename"]
    })

    _delete_db_doc_code = """def delete_db_document(doc_title: str) -> str:
    success = sdk['documents'].delete(doc_title)
    if not success:
        return f"[Error] Database document '{doc_title}' not found."
    return f"Successfully deleted database document '{doc_title}'."
"""
    _delete_db_doc_schema = json.dumps({
        "type": "object",
        "properties": {
            "doc_title": {"type": "string", "description": "The title of the database document to delete."}
        },
        "required": ["doc_title"]
    })

    _doc_mem_code = """def document_memory(
    action: str, title: str = None, tag_filter: str = None, purpose: str = None
) -> str:
    NL = chr(10)
    if action == "get":
        if not title:
            raise ValueError("title is required for action get.")
        doc = sdk['documents'].get(title)
        if not doc:
            return f"[Error] No document found with title '{title}'."
        tags_str = ", ".join(doc["tags"]) if doc["tags"] else "none"
        header = f"### {doc['title']}{NL}"
        meta = (
            f"**Purpose:** {doc['purpose']} | **Tags:** {tags_str} | "
            f"**Created:** {doc['created_at']} | **Updated:** {doc['updated_at']}{NL}{NL}"
        )
        return header + meta + doc["content"]
    elif action == "list":
        docs = sdk['documents'].list(tag_filter=tag_filter, purpose=purpose)
        if not docs:
            return "No documents stored yet."
        output = ["### Janus Documents", "| Title | Purpose | Tags | Updated |", "| --- | --- | --- | --- |"]
        for doc in docs:
            tags_str = ", ".join(doc["tags"]) if doc["tags"] else "-"
            output.append(f"| {doc['title']} | {doc['purpose']} | {tags_str} | {doc['updated_at']} |")
        return NL.join(output)
    else:
        raise ValueError(f"Unknown document action: '{action}'. Supported: get, list.")
"""
    _doc_mem_schema = json.dumps({
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "list"]},
            "title": {"type": "string"},
            "tag_filter": {"type": "string"},
            "purpose": {
                "type": "string", "enum": ["memory", "knowledge"],
                "description": "Optional filter for action=list, restricting results to this purpose."
            }
        },
        "required": ["action"]
    })

    doc_skills = [
        ("read_draft_file", "Read Draft File", "Read the entire contents of a draft document in docs/drafts/.", _read_draft_schema, _read_draft_code, "read_draft_file"),
        ("write_draft_file", "Write Draft File", "Write or update the contents of a draft document in docs/drafts/.", _write_draft_schema, _write_draft_code, "write_draft_file"),
        ("list_draft_files", "List Draft Files", "List all draft files currently in the docs/drafts/ directory.", _list_drafts_schema, _list_drafts_code, "list_draft_files"),
        ("delete_draft_file", "Delete Draft File", "Delete a draft file from the docs/drafts/ directory.", _delete_draft_schema, _delete_draft_code, "delete_draft_file"),
        ("commit_draft_to_db", "Commit Draft to DB", "Read a draft file from docs/drafts/ and save/publish it into the database document store.", _commit_draft_schema, _commit_draft_code, "commit_draft_to_db"),
        ("checkout_db_to_draft", "Checkout DB to Draft", "Retrieve a document from the database and save it to a local draft file in docs/drafts/.", _checkout_db_schema, _checkout_db_code, "checkout_db_to_draft"),
        ("delete_db_document", "Delete DB Document", "Delete a persistent document from the database document store.", _delete_db_doc_schema, _delete_db_doc_code, "delete_db_document"),
        ("document_memory", "Document Memory", "Retrieve or list persistent documents stored in the Janus database.", _doc_mem_schema, _doc_mem_code, "document_memory")
    ]
    for skill_id, name, desc, schema, code, entry in doc_skills:
        cursor.execute("""
        INSERT OR REPLACE INTO agent_skills (
            skill_id, name, description, parameters_schema, code_blob,
            entry_point_function, required_role, trigger_type, trigger_config
        ) VALUES (?, ?, ?, ?, ?, ?, 'contributor', 'manual', '{}');
        """, (skill_id, name, desc, schema, code, entry))

    # Ensure cleanup_episodic_memory is unconditionally updated/registered
    _cleanup_code = """def cleanup_episodic_memory():
    # 1. Fetch memory.retention_days
    config_row = sdk['db'].query("SELECT config_value FROM system_config WHERE config_key = 'memory.retention_days';")
    retention_days = 30
    if config_row:
        try:
            val = config_row[0].get('config_value') if isinstance(config_row[0], dict) else config_row[0][0]
            retention_days = int(val)
        except Exception:
            pass

    # 2. Prevent daily duplicates by checking last run time
    last_run_row = sdk['db'].query("SELECT config_value FROM system_config WHERE config_key = 'memory.last_cleanup_time';")
    import datetime
    now_str = datetime.datetime.utcnow().isoformat()
    if last_run_row:
        try:
            last_run_val = last_run_row[0].get('config_value') if isinstance(last_run_row[0], dict) else last_run_row[0][0]
            if last_run_val:
                last_run_time = datetime.datetime.fromisoformat(last_run_val)
                if (datetime.datetime.utcnow() - last_run_time).total_seconds() < 86400:
                    return f"Episodic memory cleanup skipped. Last run was at {last_run_val}."
        except Exception:
            pass

    # 3. Purge expired memories
    sdk['db'].query(
        "DELETE FROM episodic_memory WHERE timestamp < datetime('now', '-' || ? || ' days');",
        (retention_days,)
    )

    # 4. Save last run time
    sdk['db'].query(
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) VALUES ('memory.last_cleanup_time', ?, 1);",
        (now_str,)
    )
    return f"Episodic memory cleanup complete. Purged memories older than {retention_days} days."
"""
    cursor.execute("""
    INSERT OR REPLACE INTO agent_skills (
        skill_id, name, description, parameters_schema, code_blob,
        entry_point_function, required_role, trigger_type, trigger_config
    ) VALUES (
        'cleanup_episodic_memory', 'Cleanup Episodic Memory',
        'Applies time-to-live cleanup logic to older episodic memory rows based on retention configs.',
        '{"type": "object", "properties": {}}', ?, 'cleanup_episodic_memory', 'contributor', 'interval', '{"interval_seconds": 86400}'
    );
    """, (_cleanup_code,))

    # Ensure cleanup_llm_cache is unconditionally updated/registered
    _llm_cache_cleanup_code = """def cleanup_llm_cache():
    # 1. Fetch llm_cache.ttl_days
    config_row = sdk['db'].query("SELECT config_value FROM system_config WHERE config_key = 'llm_cache.ttl_days';")
    ttl_days = 7
    if config_row:
        try:
            val = config_row[0].get('config_value') if isinstance(config_row[0], dict) else config_row[0][0]
            ttl_days = int(val)
        except Exception:
            pass

    # 2. Prevent daily duplicates by checking last run time
    last_run_row = sdk['db'].query(
        "SELECT config_value FROM system_config WHERE config_key = 'llm_cache.last_cleanup_time';"
    )
    import datetime
    now_str = datetime.datetime.utcnow().isoformat()
    if last_run_row:
        try:
            row0 = last_run_row[0]
            last_run_val = row0.get('config_value') if isinstance(row0, dict) else row0[0]
            if last_run_val:
                last_run_time = datetime.datetime.fromisoformat(last_run_val)
                if (datetime.datetime.utcnow() - last_run_time).total_seconds() < 86400:
                    return f"LLM cache cleanup skipped. Last run was at {last_run_val}."
        except Exception:
            pass

    # 3. Purge expired cache rows
    sdk['db'].query(
        "DELETE FROM llm_cache WHERE created_at < datetime('now', '-' || ? || ' days');",
        (ttl_days,)
    )

    # 4. Save last run time
    sdk['db'].query(
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) "
        "VALUES ('llm_cache.last_cleanup_time', ?, 1);",
        (now_str,)
    )
    return f"LLM cache cleanup complete. Purged cache entries older than {ttl_days} days."
"""
    cursor.execute("""
    INSERT OR REPLACE INTO agent_skills (
        skill_id, name, description, parameters_schema, code_blob,
        entry_point_function, required_role, trigger_type, trigger_config
    ) VALUES (
        'cleanup_llm_cache', 'Cleanup LLM Cache',
        'Applies time-to-live cleanup logic to older llm_cache rows to bound disk growth.',
        '{"type": "object", "properties": {}}', ?, 'cleanup_llm_cache', 'contributor',
        'interval', '{"interval_seconds": 86400}'
    );
    """, (_llm_cache_cleanup_code,))

    # Ensure sync_skill_library skill exists and is up to date
    _sync_skill_library_code = """def run(sdk, args):
    from src.skill_harness import sync_from_registry
    repo_url = args.get("repo_url", None)
    synced, failed, errors = sync_from_registry(repo_url=repo_url)
    lines = [f"Synced: {synced}  Failed: {failed}"]
    if errors:
        lines.append("Errors:")
        lines.extend(f"  - {e}" for e in errors)
    return {"synced": synced, "failed": failed, "errors": errors, "summary": "\\n".join(lines)}
"""
    cursor.execute("""
    INSERT OR IGNORE INTO agent_skills (
        skill_id, name, description, parameters_schema, code_blob,
        entry_point_function, required_role, trigger_type, trigger_config
    ) VALUES (
        'sync_skill_library', 'Sync Skill Library',
        'Clone janus-skills-library and compile verified skills into agent_skills via the staging harness.',
        '{"type": "object", "properties": {"repo_url": {"type": "string", "description": "Override the library repo URL (optional)."}}}',
        ?, 'run', 'admin', 'manual', '{}'
    );
    """, (_sync_skill_library_code,))

    # Ensure ingest_fact skill exists
    _ingest_fact_code = """def run(sdk, args):
    from src.epistemic import run_epistemic_pipeline
    fact_text = args.get("fact_text", "").strip()
    if not fact_text:
        return {"error": "fact_text is required"}
    source = args.get("source", "manual")
    source_url = args.get("source_url", None)
    raw_metadata = args.get("metadata", {})
    result = run_epistemic_pipeline(fact_text, source=source, source_url=source_url, raw_metadata=raw_metadata)
    return result
"""
    cursor.execute("""
    INSERT OR IGNORE INTO agent_skills (
        skill_id, name, description, parameters_schema, code_blob,
        entry_point_function, required_role, trigger_type, trigger_config
    ) VALUES (
        'ingest_fact', 'Ingest Fact',
        'Run a candidate fact through the 4-phase Epistemic Ingestion Pipeline (stage → triangulate → audit → assimilate into Neo4j).',
        '{"type": "object", "properties": {"fact_text": {"type": "string", "description": "The candidate fact to ingest."}, "source": {"type": "string", "description": "Origin label (e.g. web_research, manual)."}, "source_url": {"type": "string", "description": "Optional source URL."}, "metadata": {"type": "object", "description": "Optional extra metadata."}}, "required": ["fact_text"]}',
        ?, 'run', 'contributor', 'manual', '{}'
    );
    """, (_ingest_fact_code,))

    # Check if parties table exists; if not, apply multi-party migrations
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='parties';")
    if not cursor.fetchone():
        from pathlib import Path
        migration_path = Path(__file__).resolve().parent / "migrations" / "sqlite_migration_multiparty.sql"
        if migration_path.exists():
            with open(migration_path, "r", encoding="utf-8") as f:
                migration_sql = f.read()
            cursor.executescript(migration_sql)
    else:
        cursor.execute("PRAGMA table_info(parties);")
        columns = [row[1] for row in cursor.fetchall()]
        if "last_seen" not in columns:
            cursor.execute("ALTER TABLE parties ADD COLUMN last_seen TEXT NOT NULL DEFAULT (datetime('now'));")
        if "metadata" not in columns:
            cursor.execute("ALTER TABLE parties ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}';")

    # Ensure interaction_profiles table exists
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS interaction_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        party_id TEXT NOT NULL UNIQUE,
        response_style TEXT DEFAULT 'balanced' CHECK(response_style IN ('concise', 'verbose', 'balanced')),
        tone_bias TEXT DEFAULT 'neutral',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(party_id) REFERENCES parties(id) ON DELETE CASCADE
    );
    """)

    # Ensure index on parties(public_key) exists
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parties_public_key ON parties(public_key);")

    # Ensure test_run_baselines table exists
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS test_run_baselines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        total_tests INTEGER,
        passed_tests INTEGER,
        failed_tests INTEGER,
        coverage_percentage REAL,
        commit_sha TEXT
    );
    """)

    # Epistemic ingestion staging table (Phase 1 of the Epistemic Ingestion Pipeline)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS janus_sandbox_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact_text TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual',
        source_url TEXT,
        raw_metadata TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','triangulated','audited','assimilated','rejected')),
        analyst_verdict TEXT,
        analyst_confidence REAL,
        analyst_reasoning TEXT,
        critic_verdict TEXT,
        critic_reasoning TEXT,
        neo4j_node_id TEXT,
        confidence_alpha REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Ensure preferences table exists
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS preferences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
        preference_key TEXT NOT NULL,
        preference_value TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(party_id, preference_key)
    );
    """)

    # Ensure instincts table exists
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS instincts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL UNIQUE,
        value TEXT NOT NULL,
        category TEXT NOT NULL CHECK(category IN ('schema','tool','constitution','boot','meta')),
        version INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Ensure spawn_log table exists
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS spawn_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        child_path TEXT NOT NULL UNIQUE,
        child_pid INTEGER,
        status TEXT NOT NULL DEFAULT 'spawning' CHECK(status IN ('spawning','alive','dead','unknown')),
        spawned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_heartbeat TIMESTAMP
    );
    """)

    # Seed system party if it doesn't exist
    cursor.execute("SELECT id FROM parties WHERE name = 'system';")
    if not cursor.fetchone():
        now = datetime.utcnow().isoformat()
        cursor.execute(
            "INSERT INTO parties (id, name, role, created_at, last_seen, metadata) "
            "VALUES ('system', 'system', 'observer', ?, ?, '{}');",
            (now, now)
        )

    # Bootstrapping self-replication instincts
    seed_instincts(conn)

    conn.commit()
    conn.close()

def seed_instincts(conn):
    """
    Serializes active database schemas, constitutional rules, dynamic skills,
    and system configurations into the instincts table if empty.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM instincts;")
    if cursor.fetchone()[0] > 0:
        return
        
    # 1. Schema Category: Query sqlite_master DDLs
    cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
    tables = cursor.fetchall()
    for name, sql in tables:
        try:
            tbl_name = name
            tbl_sql = sql
        except TypeError:
            tbl_name = name
            tbl_sql = sql
        
        if tbl_name and tbl_sql:
            cursor.execute("""
            INSERT OR IGNORE INTO instincts (key, value, category)
            VALUES (?, ?, 'schema');
            """, (f"schema:{tbl_name}", tbl_sql))

    # 2. Constitution Category: Serialize core_constitution
    cursor.execute("SELECT rule_key, rule_text FROM core_constitution;")
    rules = []
    for r in cursor.fetchall():
        try:
            rules.append({"rule_key": r['rule_key'], "rule_text": r['rule_text']})
        except (TypeError, IndexError, KeyError):
            rules.append({"rule_key": r[0], "rule_text": r[1]})
            
    cursor.execute("""
    INSERT OR IGNORE INTO instincts (key, value, category)
    VALUES (?, ?, 'constitution');
    """, ("core_constitution", json.dumps(rules)))

    # 3. Tool Category: Serialize agent_skills
    cursor.execute("""
    SELECT skill_id, name, description, parameters_schema, code_blob, 
           entry_point_function, required_role, trigger_type, trigger_config, is_active 
    FROM agent_skills;
    """)
    skills = []
    for s in cursor.fetchall():
        try:
            skills.append({
                "skill_id": s['skill_id'],
                "name": s['name'],
                "description": s['description'],
                "parameters_schema": s['parameters_schema'],
                "code_blob": s['code_blob'],
                "entry_point_function": s['entry_point_function'],
                "required_role": s['required_role'],
                "trigger_type": s['trigger_type'],
                "trigger_config": s['trigger_config'],
                "is_active": s['is_active']
            })
        except (TypeError, IndexError, KeyError):
            skills.append({
                "skill_id": s[0],
                "name": s[1],
                "description": s[2],
                "parameters_schema": s[3],
                "code_blob": s[4],
                "entry_point_function": s[5],
                "required_role": s[6],
                "trigger_type": s[7],
                "trigger_config": s[8],
                "is_active": s[9]
            })
            
    cursor.execute("""
    INSERT OR IGNORE INTO instincts (key, value, category)
    VALUES (?, ?, 'tool');
    """, ("agent_skills", json.dumps(skills)))

    # 4. Boot Category: Serialize system_config
    cursor.execute("SELECT config_key, config_value, is_agent_modifiable FROM system_config;")
    configs = []
    for c in cursor.fetchall():
        try:
            configs.append({
                "config_key": c['config_key'],
                "config_value": c['config_value'],
                "is_agent_modifiable": c['is_agent_modifiable']
            })
        except (TypeError, IndexError, KeyError):
            configs.append({
                "config_key": c[0],
                "config_value": c[1],
                "is_agent_modifiable": c[2]
            })
            
    cursor.execute("""
    INSERT OR IGNORE INTO instincts (key, value, category)
    VALUES (?, ?, 'boot');
    """, ("system_config", json.dumps(configs)))

    # 5. Meta Category: Parent metadata
    meta = {
        "parent_root_dir": str(src.config.ROOT_DIR),
        "parent_db_path": str(src.config.DB_PATH),
        "spawn_time": datetime.utcnow().isoformat()
    }
    cursor.execute("""
    INSERT OR IGNORE INTO instincts (key, value, category)
    VALUES (?, ?, 'meta');
    """, ("parent_meta", json.dumps(meta)))

# Helper Query Functions

def is_setup_complete() -> bool:
    """Checks if the setup wizard has been successfully run."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'setup_complete';")
    row = cursor.fetchone()
    conn.close()
    return row is not None and row[0] == "1"

def mark_setup_complete():
    """Sets the setup_complete configuration key to 1."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE system_config 
    SET config_value = '1', updated_at = CURRENT_TIMESTAMP 
    WHERE config_key = 'setup_complete';
    """)
    conn.commit()
    conn.close()

def add_constitution_rule(rule_key: str, rule_text: str):
    """
    Appends an agreed-upon rule to the core constitution.
    Uses admin connection since it modifies core_constitution.
    """
    rule_key_upper = rule_key.upper().strip()
    conn = get_connection(read_only_constitution=False)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO core_constitution (rule_key, rule_text)
    VALUES (?, ?);
    """, (rule_key_upper, rule_text))
    conn.commit()
    conn.close()

def delete_constitution_rule(rule_key: str):
    """
    Deletes an agreed-upon rule from the core constitution.
    Uses admin connection since it modifies core_constitution.
    """
    conn = get_connection(read_only_constitution=False)
    cursor = conn.cursor()
    cursor.execute("""
    DELETE FROM core_constitution WHERE rule_key = ?;
    """, (rule_key.upper().strip(),))
    conn.commit()
    conn.close()

def get_constitution() -> list:
    """Retrieves all rules from the core constitution."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("SELECT rule_key, rule_text FROM core_constitution ORDER BY id ASC;")
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_boredom_counter() -> int:
    """Retrieves the current boredom counter value."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("SELECT boredom_counter FROM drive_state LIMIT 1;")
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def increment_boredom() -> int:
    """Increments the boredom counter by 1 and returns the new value."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE drive_state 
    SET boredom_counter = boredom_counter + 1, updated_at = CURRENT_TIMESTAMP;
    """)
    conn.commit()
    
    # Retrieve the new value
    cursor.execute("SELECT boredom_counter FROM drive_state LIMIT 1;")
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def reset_boredom():
    """Resets the boredom counter to 0."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("UPDATE drive_state SET boredom_counter = 0, updated_at = CURRENT_TIMESTAMP;")
    conn.commit()
    conn.close()

def update_curiosity_vector(vector: list):
    """Updates the curiosity vector JSON array in the database."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE drive_state 
    SET curiosity_vector_json = ?, updated_at = CURRENT_TIMESTAMP;
    """, (json.dumps(vector),))
    conn.commit()
    conn.close()

def get_curiosity_vector() -> list:
    """Retrieves the curiosity vector list."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("SELECT curiosity_vector_json FROM drive_state LIMIT 1;")
    row = cursor.fetchone()
    conn.close()
    return json.loads(row[0]) if row and row[0] else []

def log_episodic_memory(speaker: str, message_content: str, context_type: str = "user_visible", party_id: Optional[str] = None):
    """Inserts a record into the episodic memory log."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO episodic_memory (speaker, message_content, context_type, party_id)
    VALUES (?, ?, ?, ?);
    """, (speaker, message_content, context_type, party_id))
    conn.commit()
    conn.close()

def log_deliberation(proposed_action: str, debate_json: dict, critic_decision: int, utility_score: float, justification: str):
    """Logs an agent deliberation cycle."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO internal_deliberations (proposed_action, agent_debate_json, critic_decision, utility_score, justification)
    VALUES (?, ?, ?, ?, ?);
    """, (proposed_action, json.dumps(debate_json), critic_decision, utility_score, justification))
    conn.commit()
    conn.close()

    if critic_decision == 0:
        from src.notifications import send_webhook_notification
        send_webhook_notification("critic_veto", f"Critic vetoed action '{proposed_action}': {justification}")

        veto_count = increment_consecutive_critic_vetoes()
        if veto_count >= 3:
            create_swarm_dispute(proposed_action, veto_count)
            reset_consecutive_critic_vetoes()
    else:
        reset_consecutive_critic_vetoes()

def get_recent_episodic_memories(limit: int = 10, context_type: str = None, party_id: Optional[str] = None) -> list:
    """Retrieves the most recent episodic memories."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    if party_id:
        if context_type:
            cursor.execute("""
            SELECT speaker, message_content, timestamp 
            FROM episodic_memory 
            WHERE context_type = ? AND (party_id = ? OR party_id IS NULL)
            ORDER BY id DESC 
            LIMIT ?;
            """, (context_type, party_id, limit))
        else:
            cursor.execute("""
            SELECT speaker, message_content, timestamp 
            FROM episodic_memory 
            WHERE party_id = ? OR party_id IS NULL
            ORDER BY id DESC 
            LIMIT ?;
            """, (party_id, limit))
    else:
        if context_type:
            cursor.execute("""
            SELECT speaker, message_content, timestamp 
            FROM episodic_memory 
            WHERE context_type = ?
            ORDER BY id DESC 
            LIMIT ?;
            """, (context_type, limit))
        else:
            cursor.execute("""
            SELECT speaker, message_content, timestamp 
            FROM episodic_memory 
            ORDER BY id DESC 
            LIMIT ?;
            """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows

# Swarm Message Bus Helpers
def get_swarm_bus_connection():
    """
    Connects to the swarm message bus DB. When running as a spawned evolution
    child (JANUS_PARENT_DB_PATH set), connects to the PARENT's live DB instead
    of the local DB_PATH, so swarm_messages rows are visible to both processes.
    Otherwise (the normal/parent process), behaves like get_connection() pointed
    at the local DB_PATH. Used only by the three swarm bus functions below —
    never expose this through SafeSwarm or any dynamic-skill-callable surface,
    since it bypasses constitution_authorizer.
    """
    import os
    bus_path = os.getenv("JANUS_PARENT_DB_PATH") or src.config.DB_PATH
    conn = sqlite3.connect(bus_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout = 10000;")
    return JanusConnectionWrapper(conn, db_type="sqlite", read_only_constitution=False)

def send_swarm_message(sender_id: str, recipient_id: str, message_type: str, content: str):
    """Inserts a message into the swarm message bus."""
    conn = get_swarm_bus_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO swarm_messages (sender_id, recipient_id, message_type, content)
    VALUES (?, ?, ?, ?);
    """, (sender_id, recipient_id, message_type, content))
    conn.commit()
    conn.close()

def get_pending_swarm_messages(recipient_id: str) -> list:
    """Retrieves all pending messages for a given recipient."""
    conn = get_swarm_bus_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, sender_id, message_type, content, timestamp
    FROM swarm_messages
    WHERE recipient_id = ? AND status = 'pending'
    ORDER BY id ASC;
    """, (recipient_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def mark_swarm_message_processed(message_id: int):
    """Marks a message in the swarm message bus as processed."""
    conn = get_swarm_bus_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE swarm_messages
    SET status = 'processed'
    WHERE id = ?;
    """, (message_id,))
    conn.commit()
    conn.close()

# Dynamic Agent Registry Modifiers
def register_helper_agent(agent_id: str, name: str, prompt: str, model: str = None):
    """Registers or updates a helper agent in the agent registry."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO agent_registry (agent_id, agent_name, system_prompt, target_model, is_active, updated_at)
    VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP);
    """, (agent_id, name, prompt, model))
    conn.commit()
    conn.close()

def deactivate_helper_agent(agent_id: str):
    """Deactivates an agent in the registry (sets is_active to 0)."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE agent_registry 
    SET is_active = 0, updated_at = CURRENT_TIMESTAMP 
    WHERE agent_id = ?;
    """, (agent_id,))
    conn.commit()
    conn.close()

# Staged Self-Modification Helpers
def stage_modification_in_db(file_path: str, temp_dir: str, diff: str, status: str):
    """Saves the pending code modification metadata in system_config."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    configs = [
        ("pending_mod_file", file_path),
        ("pending_mod_dir", temp_dir),
        ("pending_mod_diff", diff),
        ("pending_mod_status", status)
    ]
    for key, val in configs:
        cursor.execute("""
        INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable)
        VALUES (?, ?, 1);
        """, (key, val))
    conn.commit()
    conn.close()

def clear_pending_modification():
    """Clears any pending modifications from system_config."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    keys = ["pending_mod_file", "pending_mod_dir", "pending_mod_diff", "pending_mod_status"]
    for key in keys:
        cursor.execute("DELETE FROM system_config WHERE config_key = ?;", (key,))
    conn.commit()
    conn.close()

def get_pending_modification() -> dict:
    """Retrieves metadata of any pending self-modifications from system_config."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT config_key, config_value 
    FROM system_config 
    WHERE config_key IN ('pending_mod_file', 'pending_mod_dir', 'pending_mod_diff', 'pending_mod_status');
    """)
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return {}
        
    data = {k: v for k, v in rows}
    if "pending_mod_file" in data and data["pending_mod_file"]:
        return data
    return {}

# Staged Sandbox Session Helpers
def save_sandbox_session(path: str, branch: str, status: str, test_logs: str = "", fork_sha: str = "",
                          purpose: str = "evolution", app_name: str = ""):
    """Saves active sandbox session metadata in system_config."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    configs = [
        ("active_sandbox_path", path),
        ("active_sandbox_branch", branch),
        ("active_sandbox_status", status),
        ("active_sandbox_test_logs", test_logs),
        ("active_sandbox_purpose", purpose),
    ]
    # Only persist fork_sha when it is supplied (first call from create_sandbox_session);
    # subsequent status-update calls pass an empty string, so we leave the stored value alone.
    if fork_sha:
        configs.append(("active_sandbox_fork_sha", fork_sha))
    if app_name:
        configs.append(("active_sandbox_app_name", app_name))
    for key, val in configs:
        cursor.execute("""
        INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable)
        VALUES (?, ?, 1);
        """, (key, val))
    conn.commit()
    conn.close()

def clear_sandbox_session():
    """Clears any active sandbox session from system_config."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    keys = [
        "active_sandbox_path",
        "active_sandbox_branch",
        "active_sandbox_status",
        "active_sandbox_test_logs",
        "active_sandbox_fork_sha",
        "active_sandbox_purpose",
        "active_sandbox_app_name",
    ]
    for key in keys:
        cursor.execute("DELETE FROM system_config WHERE config_key = ?;", (key,))
    conn.commit()
    conn.close()

def get_sandbox_session() -> dict:
    """Retrieves metadata of the active sandbox session from system_config."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT config_key, config_value
    FROM system_config
    WHERE config_key IN (
        'active_sandbox_path',
        'active_sandbox_branch',
        'active_sandbox_status',
        'active_sandbox_test_logs',
        'active_sandbox_fork_sha',
        'active_sandbox_purpose',
        'active_sandbox_app_name'
    );
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return {}

    data = {k: v for k, v in rows}
    if "active_sandbox_path" in data and data["active_sandbox_path"]:
        # Back-compat: sessions saved before the purpose field existed default to "evolution".
        data.setdefault("active_sandbox_purpose", "evolution")
        return data
    return {}

# Helper functions for Agent Rules & Guidelines
def get_agent_rules(agent_id: str) -> list:
    """Retrieves all active rules for a given agent_id."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT rule_key, rule_text 
    FROM agent_rules 
    WHERE agent_id = ? AND is_active = 1 
    ORDER BY id ASC;
    """, (agent_id,))
    rows = cursor.fetchall()
    conn.close()
    return [{"key": r[0], "text": r[1]} for r in rows]

def get_all_agent_rules() -> list:
    """Retrieves all agent rules for all agents, active or inactive."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT agent_id, rule_key, rule_text, is_active 
    FROM agent_rules 
    ORDER BY agent_id ASC, id ASC;
    """)
    rows = cursor.fetchall()
    conn.close()
    return [{"agent_id": r[0], "key": r[1], "text": r[2], "is_active": bool(r[3])} for r in rows]

def add_agent_rule(agent_id: str, rule_key: str, rule_text: str):
    """Adds or updates a rule for a specific agent."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO agent_rules (agent_id, rule_key, rule_text, is_active)
    VALUES (?, ?, ?, 1);
    """, (agent_id, rule_key, rule_text))
    conn.commit()
    conn.close()

def toggle_agent_rule(rule_key: str, is_active: bool):
    """Enables or disables an agent rule."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE agent_rules 
    SET is_active = ?, created_at = CURRENT_TIMESTAMP 
    WHERE rule_key = ?;
    """, (1 if is_active else 0, rule_key))
    conn.commit()
    conn.close()

def delete_agent_rule(rule_key: str):
    """Deletes an agent rule from the database."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM agent_rules WHERE rule_key = ?;", (rule_key,))
    conn.commit()
    conn.close()

def get_consecutive_background_loops() -> int:
    """Retrieves the current consecutive_background_loops config value."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'consecutive_background_loops';")
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else 0

def increment_consecutive_background_loops() -> int:
    """Increments the consecutive_background_loops counter by 1 and returns the new value."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE system_config 
    SET config_value = CAST(CAST(config_value AS INTEGER) + 1 AS TEXT), updated_at = CURRENT_TIMESTAMP
    WHERE config_key = 'consecutive_background_loops';
    """)
    conn.commit()
    
    # Retrieve the new value
    cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'consecutive_background_loops';")
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else 0

def reset_consecutive_background_loops():
    """Resets the consecutive_background_loops counter to 0."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE system_config
    SET config_value = '0', updated_at = CURRENT_TIMESTAMP
    WHERE config_key = 'consecutive_background_loops';
    """)
    conn.commit()
    conn.close()

def get_consecutive_critic_vetoes() -> int:
    """Retrieves the current consecutive_critic_vetoes config value."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'consecutive_critic_vetoes';")
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else 0

def increment_consecutive_critic_vetoes() -> int:
    """Increments the consecutive_critic_vetoes counter by 1 and returns the new value."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE system_config
    SET config_value = CAST(CAST(config_value AS INTEGER) + 1 AS TEXT), updated_at = CURRENT_TIMESTAMP
    WHERE config_key = 'consecutive_critic_vetoes';
    """)
    conn.commit()

    # Retrieve the new value
    cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'consecutive_critic_vetoes';")
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else 0

def reset_consecutive_critic_vetoes():
    """Resets the consecutive_critic_vetoes counter to 0."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE system_config
    SET config_value = '0', updated_at = CURRENT_TIMESTAMP
    WHERE config_key = 'consecutive_critic_vetoes';
    """)
    conn.commit()
    conn.close()

def create_swarm_dispute(proposed_action: str, veto_count: int) -> int:
    """
    Records a swarm dispute after `veto_count` consecutive Critic vetoes: snapshots the
    last `veto_count` internal_deliberations rows as the debate transcript, flags
    dispute_paused so the daemon stops triggering new Proposer/Critic ticks, and fires
    a notification webhook.
    """
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT proposed_action, agent_debate_json, critic_decision, justification, timestamp
    FROM internal_deliberations
    ORDER BY id DESC
    LIMIT ?;
    """, (veto_count,))
    rows = cursor.fetchall()
    transcript = [
        {
            "proposed_action": action,
            "agent_debate_json": debate_json,
            "critic_decision": decision,
            "justification": justification,
            "timestamp": str(timestamp)
        }
        for action, debate_json, decision, justification, timestamp in reversed(rows)
    ]

    cursor.execute("""
    INSERT INTO swarm_disputes (proposed_action, debate_transcript, veto_count, status)
    VALUES (?, ?, ?, 'open');
    """, (proposed_action, json.dumps(transcript), veto_count))
    dispute_id = cursor.lastrowid

    cursor.execute("""
    UPDATE system_config
    SET config_value = 'true', updated_at = CURRENT_TIMESTAMP
    WHERE config_key = 'dispute_paused';
    """)
    conn.commit()
    conn.close()

    from src.notifications import send_webhook_notification
    send_webhook_notification(
        "dispute_detected",
        f"Repeated Critic vetoes ({veto_count}x) on action '{proposed_action}'. "
        f"Dispute [{dispute_id}] logged and the autonomous loop is paused pending resolution via /goals resolve."
    )
    return dispute_id

def get_open_disputes() -> list:
    """Retrieves all unresolved swarm disputes, most recent first."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, created_at, proposed_action, veto_count, status
    FROM swarm_disputes
    WHERE status = 'open'
    ORDER BY id DESC;
    """)
    rows = cursor.fetchall()
    conn.close()
    return [
        {"id": r[0], "created_at": r[1], "proposed_action": r[2], "veto_count": r[3], "status": r[4]}
        for r in rows
    ]

def get_dispute(dispute_id: int) -> Optional[dict]:
    """Retrieves a single swarm dispute by id, including its debate transcript."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, created_at, proposed_action, debate_transcript, veto_count, status, resolution, resolution_notes, resolved_at
    FROM swarm_disputes
    WHERE id = ?;
    """, (dispute_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "created_at": row[1],
        "proposed_action": row[2],
        "debate_transcript": json.loads(row[3]),
        "veto_count": row[4],
        "status": row[5],
        "resolution": row[6],
        "resolution_notes": row[7],
        "resolved_at": row[8],
    }

def resolve_dispute(dispute_id: int, resolution: str, notes: Optional[str] = None) -> dict:
    """
    Resolves an open swarm dispute, clears the dispute_paused flag (resuming the
    autonomous loop), and resets the consecutive veto counter.
    """
    if resolution not in ("override", "abort", "rewrite_rules"):
        raise ValueError(f"Invalid resolution '{resolution}'. Must be one of: override, abort, rewrite_rules.")

    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM swarm_disputes WHERE id = ?;", (dispute_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Dispute ID {dispute_id} not found.")
    if row[0] == "resolved":
        conn.close()
        raise ValueError(f"Dispute ID {dispute_id} is already resolved.")

    cursor.execute("""
    UPDATE swarm_disputes
    SET status = 'resolved', resolution = ?, resolution_notes = ?, resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
    WHERE id = ?;
    """, (resolution, notes, dispute_id))

    cursor.execute("""
    UPDATE system_config
    SET config_value = 'false', updated_at = CURRENT_TIMESTAMP
    WHERE config_key = 'dispute_paused';
    """)
    cursor.execute("""
    UPDATE system_config
    SET config_value = '0', updated_at = CURRENT_TIMESTAMP
    WHERE config_key = 'consecutive_critic_vetoes';
    """)
    conn.commit()
    conn.close()
    return {"success": True, "dispute_id": dispute_id, "resolution": resolution}

def set_system_config_value(key: str, value: str, is_agent: bool = True):
    """
    Sets a config value in system_config.
    If is_agent is True, checks validate_config_write(key) first.
    """
    if is_agent:
        from src.middleware import validate_config_write
        validate_config_write(key)
        
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    
    # Check if key exists to keep its is_agent_modifiable status
    cursor.execute("SELECT is_agent_modifiable FROM system_config WHERE config_key = ?;", (key,))
    row = cursor.fetchone()
    modifiable = row[0] if row is not None else 1
    
    cursor.execute("""
    INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable, updated_at)
    VALUES (?, ?, ?, CURRENT_TIMESTAMP);
    """, (key, value, modifiable))
    conn.commit()
    conn.close()



# Document Memory Helper Functions (Delegated to SafeDocuments SDK wrapper)

def create_document(title: str, content: str, tags: list = None, purpose: str = "memory", metadata: dict = None) -> int:
    """Creates a new document record. Raises if the title already exists."""
    from src.skills import SafeDocuments
    sd = SafeDocuments()
    if sd.get(title):
        raise ValueError(f"Document with title '{title}' already exists.")
    sd.upsert(title, content, tags, purpose=purpose, metadata=metadata)
    doc = sd.get(title)
    return doc["id"] if doc else 0


def get_document(title: str) -> dict:
    """Returns a document dict by title, or None if not found."""
    from src.skills import SafeDocuments
    return SafeDocuments().get(title)


def update_document(
    title: str, content: str = None, tags: list = None, purpose: str = None, metadata: dict = None
) -> bool:
    """Updates content/tags/purpose/metadata for an existing document. Returns False if not found.
    Any field left as None keeps its current value rather than being reset to a default."""
    from src.skills import SafeDocuments
    sd = SafeDocuments()
    doc = sd.get(title)
    if not doc:
        return False
    new_content = content if content is not None else doc["content"]
    new_tags = tags if tags is not None else doc["tags"]
    new_purpose = purpose if purpose is not None else doc["purpose"]
    new_metadata = metadata if metadata is not None else doc["metadata"]
    return sd.upsert(title, new_content, new_tags, purpose=new_purpose, metadata=new_metadata)


def delete_document(title: str) -> bool:
    """Deletes a document by title. Returns False if not found."""
    from src.skills import SafeDocuments
    return SafeDocuments().delete(title)


def list_documents(tag_filter: str = None, purpose: str = None) -> list:
    """Returns a list of document dicts. Optionally filters by tag and/or purpose."""
    from src.skills import SafeDocuments
    return SafeDocuments().list(tag_filter=tag_filter, purpose=purpose)
