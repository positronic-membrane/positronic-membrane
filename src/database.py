import sqlite3
import json
from datetime import datetime
import src.config

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

def get_connection(read_only_constitution=True):
    """
    Returns an SQLite connection. By default, it applies an authorizer
    that blocks writing to the core_constitution table.
    """
    import os
    db_dir = os.path.dirname(os.path.abspath(src.config.DB_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(src.config.DB_PATH)
    # Enable Write-Ahead Logging (WAL)
    conn.execute("PRAGMA journal_mode=WAL;")
    
    if read_only_constitution:
        conn.set_authorizer(constitution_authorizer)
        
    return conn

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

    # Populate drive state if empty
    cursor.execute("SELECT COUNT(*) FROM drive_state;")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO drive_state (boredom_counter, curiosity_vector_json) VALUES (0, '[]');")

    # Populate default system configurations if empty
    default_configs = [
        ("setup_complete", "0", 0),  # Strictly human-only modifiable
        ("boredom_threshold", "5", 1),
        ("n_loop_limit", "5", 0)
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
        ("archivist", "Archivist Agent", "You are the Archivist. Consolidate logs, extract memories, and index knowledge.", os.getenv("ARCHIVIST_MODEL"))
    ]
    for agent_id, name, prompt, model in default_agents:
        cursor.execute("""
        INSERT OR IGNORE INTO agent_registry (agent_id, agent_name, system_prompt, target_model)
        VALUES (?, ?, ?, ?);
        """, (agent_id, name, prompt, model))

    conn.commit()
    conn.close()

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
    conn = get_connection(read_only_constitution=False)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO core_constitution (rule_key, rule_text)
    VALUES (?, ?);
    """, (rule_key, rule_text))
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

def log_episodic_memory(speaker: str, message_content: str, context_type: str = "user_visible"):
    """Inserts a record into the episodic memory log."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO episodic_memory (speaker, message_content, context_type)
    VALUES (?, ?, ?);
    """, (speaker, message_content, context_type))
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

def get_recent_episodic_memories(limit: int = 10) -> list:
    """Retrieves the most recent episodic memories."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
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
def send_swarm_message(sender_id: str, recipient_id: str, message_type: str, content: str):
    """Inserts a message into the swarm message bus."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO swarm_messages (sender_id, recipient_id, message_type, content)
    VALUES (?, ?, ?, ?);
    """, (sender_id, recipient_id, message_type, content))
    conn.commit()
    conn.close()

def get_pending_swarm_messages(recipient_id: str) -> list:
    """Retrieves all pending messages for a given recipient."""
    conn = get_connection(read_only_constitution=True)
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
    conn = get_connection(read_only_constitution=True)
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
def save_sandbox_session(path: str, branch: str, status: str):
    """Saves active sandbox session metadata in system_config."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    configs = [
        ("active_sandbox_path", path),
        ("active_sandbox_branch", branch),
        ("active_sandbox_status", status)
    ]
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
    keys = ["active_sandbox_path", "active_sandbox_branch", "active_sandbox_status"]
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
    WHERE config_key IN ('active_sandbox_path', 'active_sandbox_branch', 'active_sandbox_status');
    """)
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return {}
        
    data = {k: v for k, v in rows}
    if "active_sandbox_path" in data and data["active_sandbox_path"]:
        return data
    return {}




