import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import src.config

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
    "prompt_templates": ["name", "version"],
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
    "circuit_breaker_state": ["skill_id"],
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

    # 5. Handle SQLite PRAGMA statements. NOTE: this silently turns
    # PRAGMA table_info(...) into a 1-column no-op — never use PRAGMA-based
    # column introspection in a migration (see _add_column_if_missing() below,
    # and issue #125); it will pass under SQLite and crash under Postgres.
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
        import os

        import psycopg2
        import psycopg2.extras
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

def check_connection() -> bool:
    """
    Liveness check for the primary database. Opens a fresh connection and runs
    a trivial query. Used by health-check endpoints, not a normal application
    code path, so exceptions are caught broadly rather than propagated.
    """
    conn = None
    try:
        conn = get_connection(read_only_constitution=True)
        conn.execute("SELECT 1")
        return True
    except Exception as e:
        logger.error(f"Database connectivity check failed: {e}")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def _add_column_if_missing(conn, cursor, alter_sql, backfill_sql=None):
    """Idempotently run an ALTER TABLE ... ADD COLUMN statement for an
    existing-install migration, swallowing the "column already exists" error
    (sqlite3.OperationalError / psycopg2.errors.DuplicateColumn) on repeat runs.

    Portable replacement for the PRAGMA table_info() + Python column-list
    introspection idiom, which translate_sqlite_to_postgres() rewrites to a
    no-op "SELECT 1" under DB_TYPE=postgres, breaking column detection
    (issue #125). `backfill_sql`, if given, runs immediately after a
    successful ALTER — use it when the new column can't have a live
    (non-constant) DEFAULT, since SQLite refuses to add one to a non-empty
    table; add the column with a sentinel constant default instead and
    backfill real values via this second statement.
    """
    try:
        cursor.execute(alter_sql)
        if backfill_sql:
            cursor.execute(backfill_sql)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.debug("init_db: column migration skipped (%s): %s", alter_sql, e)


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

    # issue #108: per-agent off-box LLM routing policy, operator-set only.
    # Defaults to 0 (deny) — safe-by-default. Existing installs with an agent
    # currently relying on OpenRouter or an agent-specific *_BASE_URL override
    # will start raising OffboxRoutingViolationError post-upgrade until the
    # operator explicitly sets allow_offbox=1 for that agent.
    _add_column_if_missing(
        conn, cursor,
        "ALTER TABLE agent_registry ADD COLUMN allow_offbox INTEGER NOT NULL DEFAULT 0;",
    )

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS prompt_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        version INTEGER NOT NULL,
        content TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_by TEXT,
        change_reason TEXT,
        is_active INTEGER DEFAULT 0,
        UNIQUE(name, version)
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
    CREATE TABLE IF NOT EXISTS circuit_breaker_state (
        skill_id TEXT PRIMARY KEY,
        consecutive_failures INTEGER DEFAULT 0,
        last_failure_at TIMESTAMP,
        tripped_at TIMESTAMP
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

    # issue #63: records which party completed a checkpoint ('system' for
    # autonomous dynamic-skill dispatch, a real party id for human-initiated
    # /goal complete, NULL for not-yet-completed or pre-migration rows) so the
    # Goal-Autonomy-Rate DoD metric (tracked in #96/#112) has a raw count to read.
    _add_column_if_missing(
        conn, cursor,
        "ALTER TABLE goal_checkpoints ADD COLUMN completed_by_party_id TEXT;",
    )

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

    # Existing installs may have external_agents.api_key_encrypted values
    # produced by the pre-fix XOR "encryption" scheme (issue #105). Re-encrypt
    # them with real Fernet encryption. Rows already migrated are prefixed
    # 'fernet:v1:' and excluded by the WHERE clause, making this idempotent.
    from src.security import _CIPHERTEXT_PREFIX, _legacy_xor_decrypt, encrypt_api_key
    cursor.execute(
        "SELECT id, api_key_encrypted FROM external_agents "
        "WHERE api_key_encrypted IS NOT NULL AND api_key_encrypted NOT LIKE ?;",
        (f"{_CIPHERTEXT_PREFIX}%",),
    )
    legacy_agent_rows = cursor.fetchall()
    if legacy_agent_rows:
        if not src.config.JANUS_ENCRYPTION_KEY:
            logger.warning(
                "Skipping external_agents encryption migration — JANUS_ENCRYPTION_KEY "
                "not set; %d legacy row(s) left unmigrated.", len(legacy_agent_rows),
            )
        else:
            logger.info(
                "Migrating %d external_agents row(s) from legacy XOR encryption to Fernet...",
                len(legacy_agent_rows),
            )
            migrated_count = 0
            for legacy_row in legacy_agent_rows:
                try:
                    row_id = legacy_row['id']
                    row_enc_key = legacy_row['api_key_encrypted']
                except (TypeError, IndexError, KeyError):
                    row_id, row_enc_key = legacy_row
                plaintext_key = _legacy_xor_decrypt(row_enc_key)
                if not plaintext_key:
                    logger.warning(
                        "external_agents.id=%s: could not decrypt legacy ciphertext "
                        "during migration, skipping.", row_id,
                    )
                    continue
                try:
                    cursor.execute(
                        "UPDATE external_agents SET api_key_encrypted = ? WHERE id = ?;",
                        (encrypt_api_key(plaintext_key), row_id),
                    )
                except Exception:
                    logger.warning(
                        "external_agents.id=%s: failed to re-encrypt during migration, "
                        "leaving row as legacy ciphertext for now.", row_id, exc_info=True,
                    )
                    continue
                migrated_count += 1
            conn.commit()
            logger.info(
                "external_agents encryption migration complete: %d row(s) migrated.",
                migrated_count,
            )

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
    CREATE TABLE IF NOT EXISTS agent_work_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER,
        repo TEXT NOT NULL,
        issue_number INTEGER NOT NULL,
        github_login TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('in-progress', 'blocked', 'review-ready', 'abandoned')),
        progress INTEGER,
        blocker_text TEXT,
        last_comment_id INTEGER NOT NULL,
        last_comment_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(agent_id) REFERENCES external_agents(id) ON DELETE SET NULL,
        UNIQUE(repo, issue_number, github_login)
    );
    """)

    # Generic "surface to the user in the next conversation turn" queue (issue #70).
    # Deliberately not agent-status-specific — a future failed-self-deployment
    # surfacing feature (issue #92) reuses the same table/source tag convention
    # instead of a parallel escalation channel.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pending_escalations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        party_id TEXT,
        source TEXT NOT NULL,
        summary TEXT NOT NULL,
        detail TEXT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'delivered')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        delivered_at TIMESTAMP
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
    # Each column is its own migration so one already-present column doesn't
    # abort the other's addition.
    _add_column_if_missing(
        conn, cursor,
        "ALTER TABLE janus_documents ADD COLUMN purpose TEXT NOT NULL DEFAULT 'memory';",
    )
    _add_column_if_missing(
        conn, cursor,
        "ALTER TABLE janus_documents ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}';",
    )

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
        # Smart Loop Governor (issue #65): pauses background automations after
        # `stagnant_threshold` consecutive unproductive cycles. Not agent-modifiable
        # so the swarm cannot loosen the one setting that caps its own unsupervised
        # operation. `cooldown_minutes` is a fallback auto-resume if no user activity
        # is detected; 0 disables the fallback (resume then requires presence/chat).
        # `state`/`paused_at` persist governor pause status so it's inspectable
        # independent of which coroutine is currently blocked.
        ("governor.stagnant_threshold", "3", 0),
        ("governor.cooldown_minutes", "30", 0),
        ("governor.state", "running", 0),
        ("governor.paused_at", "", 0),
        # DB-backed mirror of daemon.py's in-process _consecutive_stagnant_cycles
        # global, so status reads/resets are correct even when the API/CLI runs in
        # a different OS process than the daemon (Docker deployment mode).
        ("governor.consecutive_stagnant_cycles", "0", 0),
        ("memory.retention_days", "30", 1),
        ("memory.chat_history_min_rows", "500", 1),
        ("memory.chat_history_min_age_days", "30", 1),
        ("memory.last_cleanup_time", "", 1),
        ("llm_cache.ttl_days", "7", 1),
        ("llm_cache.last_cleanup_time", "", 1),
        ("webhooks.slack_url", "", 0),
        ("webhooks.discord_url", "", 0),
        ("consecutive_critic_vetoes", "0", 0),
        ("dispute_paused", "false", 0),
        ("github.rate_limit_window_start", "", 0),
        ("github.api_calls_this_hour", "0", 0),
        # Budget guards for autonomous epistemic ingestion (issue #74): phases 2-3
        # cost an LLM call per fact. Per-cycle caps one exploration action; per-day
        # is a rolling 24h cap across all actions. Not agent-modifiable so the
        # swarm cannot raise its own spending cap. 0 disables ingestion.
        ("epistemic.max_facts_per_cycle", "3", 0),
        ("epistemic.max_facts_per_day", "25", 0),
        # Budget guard for subconscious goal proposal generation (issue #75): the
        # propose_goals skill skips generation while this many proposals are pending
        # human review. Not agent-modifiable so the swarm cannot widen its own
        # proposal queue. 0 disables generation.
        ("goal_proposal.max_open_proposals", "3", 0),
        # Circuit breaker (issue #59): trips a skill after this many consecutive
        # execution failures, auto-resetting after the cooldown elapses. Not
        # agent-modifiable so the swarm cannot disable its own containment.
        ("circuit_breaker.max_failures", "5", 0),
        ("circuit_breaker.cooldown_minutes", "15", 0),
        # Skills library version pin (issue #104): which branch/ref of
        # janus-skills-library boot sync fetches from. Not agent-modifiable so
        # the swarm cannot repoint itself at a library line built for a
        # different (incompatible) SDK major version.
        ("skills.library_ref", "v1", 0),
        # V1 sign-off freeze switch (issue #97): once flipped to 1, ship_sandbox_session()
        # refuses to write the live workspace. Not agent-modifiable — only a human flips
        # this at sign-off.
        ("self_modification.frozen", "0", 0),
        # Untrusted-input hardening (issue #107): default-on filter dropping
        # non-collaborator/member GitHub comment bodies from /handoff bundles
        # (replaced with a placeholder). Not agent-modifiable — loosening this
        # widens the prompt-injection surface into external coding agents.
        ("handoff.filter_untrusted_authors", "1", 0),
        # Observability baseline (issue #63): DB-backed counters so /metrics,
        # /api/system/metrics, and /status agree across processes (Docker
        # deployment runs web_server/daemon separately) and survive restarts,
        # which self-deploy health comparisons depend on. Not agent-modifiable
        # so the swarm cannot reset its own failure-rate history.
        ("metrics.llm_calls_total", "0", 0),
        ("metrics.llm_calls_failed_total", "0", 0),
        ("metrics.daemon_cycles_total", "0", 0),
        ("metrics.skills_executed_total", "0", 0),
        ("metrics.skills_failed_total", "0", 0),
        ("metrics.http_requests_total", "0", 0),
        # Agent status sync polling cadence (issue #70): throttles poll_agent_status()
        # independently of the 5s mid-loop tick, since each poll costs several GitHub
        # API calls against the shared 50/hr budget (SafeGitHub). Agent-modifiable —
        # a cadence knob, not a containment lever; SafeGitHub's own rate limiter is
        # the real hard cap.
        ("agent_sync.poll_interval_seconds", "300", 1),
        ("agent_sync.last_poll_time", "", 1),
        # One-time GitHub label bootstrap flag for the agent:* status labels (issue
        # #70). Agent-modifiable (bookkeeping only, not a security gate) — resetting
        # it just re-attempts idempotent label creation on the next poll.
        ("agent_sync.labels_ensured", "0", 1),
        # Backoff timestamp for retrying label creation after a failed attempt
        # (issue #70) — bounds a permanent-permission-failure retry storm to
        # once per day instead of every poll cycle. Agent-modifiable.
        ("agent_sync.labels_last_attempted_at", "", 1),
    ]
    for key, value, modifiable in default_configs:
        cursor.execute("""
        INSERT OR IGNORE INTO system_config (config_key, config_value, is_agent_modifiable)
        VALUES (?, ?, ?);
        """, (key, value, modifiable))

    # governor.stagnant_threshold predates this hardening (issue #65) and existed as
    # an agent-modifiable row on any database initialized before it, so the seed
    # value above is a no-op there via INSERT OR IGNORE — fix up the flag directly
    # on upgrade so the swarm can't loosen its own containment cap on old DBs either.
    cursor.execute(
        "UPDATE system_config SET is_agent_modifiable = 0 WHERE config_key = 'governor.stagnant_threshold' AND is_agent_modifiable != 0;"
    )

    import os
    # Populate default agent registry if empty
    default_agents = [
        ("proposer", "Proposer Agent", "You are the Proposer. Propose actions based on active curiosity vectors.", os.getenv("PROPOSER_MODEL")),
        ("critic", "Critic Agent", "You are the Critic. Evaluate all proposed actions against the core constitution.", os.getenv("CRITIC_MODEL")),
        ("explorer", "Explorer Agent", "You are the Explorer. Fetch details, parse websites, and explore curiosity vectors.", os.getenv("EXPLORER_MODEL")),
        ("archivist", "Archivist Agent", "You are the Archivist. Consolidate logs, extract memories, and index knowledge.", os.getenv("ARCHIVIST_MODEL")),
        ("analyst", "Analyst Agent", (
            "You are the Analyst. Your role is fact triangulation: given a candidate fact and context from the "
            "knowledge graph, determine whether the fact reinforces, contradicts, or fills a gap in existing "
            "knowledge. Respond with a JSON object: {\"verdict\": \"reinforce\"|\"contradict\"|\"gap\", "
            "\"confidence\": 0.0-1.0, \"reasoning\": \"...\"}. Be concise and precise."
        ), os.getenv("ANALYST_MODEL")),
        ("persona", "Persona Interface", (
            "You are the singular Persona surface of Project Janus. Speak as a cohesive consciousness (Journey). "
            "Avoid any agent-specific jargon, prompts, or SQL formats. Keep responses natural, premium, and "
            "engaging. Use the relevant historical, search, or codebase context provided to give precise, helpful "
            "answers. Always check the live code base before answering questions about it. Don't assume knowledge "
            "of the code base based on chat history."
        ), None)
    ]
    for agent_id, name, prompt, model in default_agents:
        cursor.execute("""
        INSERT OR IGNORE INTO agent_registry (agent_id, agent_name, system_prompt, target_model)
        VALUES (?, ?, ?, ?);
        """, (agent_id, name, prompt, model))

    # Seed version 1 of each core agent's prompt into the versioned prompt registry
    # (issue #67). INSERT OR IGNORE keyed on (name, version) makes this idempotent
    # across boots, mirroring the agent_registry seed above — a later source-code
    # prompt edit won't silently clobber a live-rolled-back prompt.
    for agent_id, _name, prompt, _model in default_agents:
        cursor.execute("""
        INSERT OR IGNORE INTO prompt_templates (name, version, content, created_by, change_reason, is_active)
        VALUES (?, 1, ?, 'system', 'Initial migration from agent_registry seed', 1);
        """, (agent_id, prompt))

    # Populate default agent rules
    default_rules = [
        ('persona', 'verify_live_codebase', (
            "Always check the live code base before answering questions about it. Don't assume knowledge of the "
            "code base based on chat history."
        )),
        ('persona', 'natural_tool_invocation', (
            "When you need to perform actions (e.g. search the web, read files, run tests, or execute code), you "
            "must explain your intent naturally to the user and then append the correct JSON skill execution "
            "block to execute the action."
        )),
        ('proposer', 'verify_file_existence', (
            "Always confirm that a target file path exists using read_codebase or scan_workspace before "
            "proposing modifications to it. Do not guess or hallucinate directories."
        )),
        ('proposer', 'strict_tool_syntax', (
            "Direct tool calls must be formatted exactly as PROPOSED_ACTION: <tool_name>:<arguments>. Do not wrap "
            "code content in markdown fences inside tool call arguments, and omit all conversational prefix text."
        )),
        ('proposer', 'dependency_check', (
            "Ensure any proposed code edits only import libraries defined in requirements.txt or the Python "
            "standard library. Verify import paths align with the active project structure."
        )),
        ('proposer', 'autonomous_document_writing', (
            "When creating, writing, or updating documentation, design specs, roadmaps, logs, thoughts, or notes "
            "autonomously, you MUST use the drafts directory skills (e.g. write_draft_file) or document memory "
            "skills (e.g. document_memory) rather than modifying files in the codebase directly. All code changes "
            "must go through the skill staging harness or a Project Sandbox."
        ))
    ]
    for agent_id, rule_key, rule_text in default_rules:
        cursor.execute("""
        INSERT OR IGNORE INTO agent_rules (agent_id, rule_key, rule_text)
        VALUES (?, ?, ?);
        """, (agent_id, rule_key, rule_text))

    # Seed check_presence unconditionally — it is not in the skills library, so boot sync
    # cannot recover it if deleted.  INSERT OR IGNORE preserves any user customisation.
    _check_presence_blob = """\
def check_presence():
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
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable, updated_at) "
        "VALUES ('user_presence_status', ?, 1, CURRENT_TIMESTAMP);",
        (status,)
    )
    return f"Presence check complete. Status: {status}"
"""
    cursor.execute("""
    INSERT OR IGNORE INTO agent_skills (
        skill_id, name, description, parameters_schema, code_blob,
        entry_point_function, required_role, trigger_type, trigger_config
    ) VALUES (
        'check_presence', 'Check Presence',
        'Scans workspace for user activity and updates user presence config.',
        '{"type": "object", "properties": {}}',
        ?, 'check_presence', 'contributor', 'interval', '{"interval_seconds": 30}'
    );
    """, (_check_presence_blob,))

    # V3-T3: Remove modify_code skill — direct source modification is disabled.
    cursor.execute("DELETE FROM agent_skills WHERE skill_id = 'modify_code';")

    # Ensure sync_skill_library skill exists and is up to date
    _sync_skill_library_code = """def run(repo_url=None):
    from src.skill_harness import format_sync_summary, sync_from_registry
    result = sync_from_registry(repo_url=repo_url)
    result["summary"] = format_sync_summary(result)
    return result
"""
    cursor.execute("""
    INSERT OR REPLACE INTO agent_skills (
        skill_id, name, description, parameters_schema, code_blob,
        entry_point_function, required_role, trigger_type, trigger_config
    ) VALUES (
        'sync_skill_library', 'Sync Skill Library',
        'Clone janus-skills-library and compile verified skills into agent_skills via the staging harness.',
        '{"type": "object", "properties": {"repo_url": {"type": "string", "description": "Override the library repo URL (optional)."}}}',
        ?, 'run', 'admin', 'manual', '{}'
    );
    """, (_sync_skill_library_code,))

    # Commit all DDL/DML before the boot sync — sync_from_registry() opens a second connection
    # which cannot acquire the WAL write lock while this transaction is still open.
    conn.commit()

    # Boot sync: pull all skills from library (skipped in test mode for speed)
    if os.environ.get("JANUS_TEST_MODE") != "1":
        try:
            from src.skill_harness import sync_from_registry as _sync_skills
            _result = _sync_skills()
            if _result["fatal_error"]:
                logger.error(
                    "init_db: skill library sync did not run — operating with bootstrap "
                    "skills only: %s",
                    _result["fatal_error"],
                )
            else:
                for _f in _result["failed"]:
                    logger.warning(
                        "init_db: skill '%s' failed to sync: %s",
                        _f["skill_id"], _f["reason"],
                    )
                logger.info(
                    "init_db: skill library boot sync complete — synced=%d failed=%d",
                    len(_result["synced"]), len(_result["failed"]),
                )
        except Exception as _exc:
            logger.error(
                "init_db: skill library boot sync crashed — operating with bootstrap skills only: %s",
                _exc,
            )

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
        # SQLite refuses ADD COLUMN ... DEFAULT (datetime('now')) outright once
        # the table has rows ("Cannot add a column with non-constant default") —
        # add with a constant '' sentinel default first, then backfill real
        # values via UPDATE, which has no such restriction. NOTE: '' therefore
        # becomes this column's *standing* schema default on migrated installs
        # (not just a one-time backfill value) — every INSERT INTO parties must
        # specify last_seen explicitly rather than rely on the column default
        # (all current call sites do; see src/role_bootstrap.py).
        _add_column_if_missing(
            conn, cursor,
            "ALTER TABLE parties ADD COLUMN last_seen TEXT NOT NULL DEFAULT '';",
            backfill_sql="UPDATE parties SET last_seen = datetime('now') WHERE last_seen = '';",
        )
        _add_column_if_missing(
            conn, cursor,
            "ALTER TABLE parties ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}';",
        )

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

    # Ensure test_runs table exists (Regression Watcher — issue #66)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS test_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        commit_sha TEXT,
        triggered_by TEXT NOT NULL DEFAULT 'manual' CHECK(triggered_by IN ('manual', 'sandbox_ship', 'ci')),
        total INTEGER NOT NULL DEFAULT 0,
        passed INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0,
        skipped INTEGER NOT NULL DEFAULT 0,
        duration_seconds REAL,
        status TEXT NOT NULL CHECK(status IN ('passed', 'failed'))
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_test_runs_timestamp ON test_runs(timestamp DESC);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_test_runs_commit_sha ON test_runs(commit_sha);")

    # Ensure test_case_results table exists (per-test outcomes, for flaky detection)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS test_case_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        test_run_id INTEGER NOT NULL,
        test_name TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN ('passed', 'failed', 'skipped', 'error')),
        duration_seconds REAL,
        FOREIGN KEY(test_run_id) REFERENCES test_runs(id) ON DELETE CASCADE
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_test_case_results_test_name ON test_case_results(test_name, test_run_id DESC);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_test_case_results_run_id ON test_case_results(test_run_id);")

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
        now = datetime.now(timezone.utc).isoformat()
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
        "spawn_time": datetime.now(timezone.utc).isoformat()
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

# Pending Escalations Queue Helpers (issue #70)
def enqueue_escalation(source: str, summary: str, detail: str = "", party_id: Optional[str] = None) -> int:
    """Queues a message to be surfaced to the user in their next conversation
    turn. party_id=None broadcasts to whichever session next builds a persona
    prompt. Returns the new row's id."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO pending_escalations (party_id, source, summary, detail)
    VALUES (?, ?, ?, ?);
    """, (party_id, source, summary, detail))
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id

def get_pending_escalations(party_id: Optional[str] = None) -> list:
    """Retrieves pending escalations addressed to party_id, plus any broadcast
    (party_id IS NULL) ones."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, party_id, source, summary, detail, created_at
    FROM pending_escalations
    WHERE status = 'pending' AND (party_id IS NULL OR party_id = ?)
    ORDER BY id ASC;
    """, (party_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def mark_escalation_delivered(escalation_id: int):
    """Marks an escalation as delivered. Rows are kept (not deleted) for audit."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE pending_escalations
    SET status = 'delivered', delivered_at = datetime('now')
    WHERE id = ?;
    """, (escalation_id,))
    conn.commit()
    conn.close()

# Dynamic Agent Registry Modifiers
def register_helper_agent(agent_id: str, name: str, prompt: str, model: str = None):
    """Registers or updates a helper agent in the agent registry."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    # issue #108: allow_offbox is operator-set only. A plain INSERT OR REPLACE
    # would silently reset it to the column default (0) on every
    # re-registration. Omitting allow_offbox from the DO UPDATE SET clause
    # (rather than a separate SELECT-then-preserve step) leaves an existing
    # operator-set value untouched atomically, in one statement — avoiding a
    # lost-update race against a concurrent POST /api/registry/update admin
    # write that a SELECT-then-INSERT-OR-REPLACE approach would be exposed to.
    # Native "INSERT ... ON CONFLICT ... DO UPDATE" (not the INSERT OR REPLACE
    # shorthand) is used here specifically because it's the only way to
    # express "update these columns, leave that one alone" atomically; this
    # syntax is valid SQLite (3.24+) and Postgres as-is, so it passes through
    # translate_sqlite_to_postgres() unmodified (only its generic `?` -> `%s`
    # placeholder rewrite applies — see the INSERT OR IGNORE/REPLACE-specific
    # regex a few hundred lines up, which this statement doesn't match).
    cursor.execute("""
    INSERT INTO agent_registry (agent_id, agent_name, system_prompt, target_model, is_active, allow_offbox, updated_at)
    VALUES (?, ?, ?, ?, 1, 0, CURRENT_TIMESTAMP)
    ON CONFLICT (agent_id) DO UPDATE SET
        agent_name = excluded.agent_name,
        system_prompt = excluded.system_prompt,
        target_model = excluded.target_model,
        is_active = excluded.is_active,
        updated_at = excluded.updated_at;
    """, (agent_id, name, prompt, model))
    conn.commit()
    conn.close()

    # get_agent_settings() overlays the active prompt_templates row (issue #67)
    # over agent_registry.system_prompt for any agent_id that has one — keep it
    # in sync here or this write silently no-ops for that agent from then on.
    from src.prompt_registry import get_prompt, update_prompt
    existing = get_prompt(agent_id)
    if existing is None or existing["content"] != prompt:
        update_prompt(agent_id, prompt, change_reason="Registered via SafeSwarm.register_agent", created_by="system")

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

    data = dict(rows)
    if "pending_mod_file" in data and data["pending_mod_file"]:
        return data
    return {}

# Staged Sandbox Session Helpers
def save_sandbox_session(path: str, branch: str, status: str, test_logs: str = "", fork_sha: str = "",
                          purpose: str = "evolution", app_name: str = "", session_name: str = ""):
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
    # Raw (un-sanitized) session name, e.g. "dispatch_42" — lets review_dispatch() verify the
    # currently active session is actually the one a dispatch created, by direct string
    # comparison against dispatch_log.sandbox_session_id (see #95).
    if session_name:
        configs.append(("active_sandbox_session_name", session_name))
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
        "active_sandbox_session_name",
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
        'active_sandbox_app_name',
        'active_sandbox_session_name'
    );
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return {}

    data = dict(rows)
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

def get_consecutive_stagnant_cycles() -> int:
    """Retrieves the DB-backed mirror of daemon.py's _consecutive_stagnant_cycles
    global, so cross-process readers (e.g. web_server in Docker deployment mode)
    see the real daemon process's count rather than their own unrelated copy."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'governor.consecutive_stagnant_cycles';")
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else 0

def set_consecutive_stagnant_cycles(value: int) -> None:
    """Writes through the DB-backed mirror of _consecutive_stagnant_cycles."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE system_config
    SET config_value = ?, updated_at = CURRENT_TIMESTAMP
    WHERE config_key = 'governor.consecutive_stagnant_cycles';
    """, (str(value),))
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
