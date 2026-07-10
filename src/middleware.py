import re
import logging
from src.database import get_connection, get_constitution

logger = logging.getLogger("JanusMiddleware")

class SafetyViolationError(Exception):
    """Raised when an autonomous agent attempts to execute an action violating the constitution or safety rules."""
    pass

class SelfModificationFrozenError(Exception):
    """Raised when a live-workspace self-modification write is attempted while
    system_config['self_modification.frozen'] is set."""
    pass

def check_sql_safety(sql_query: str):
    """
    Parses SQL queries to block any updates, deletes, drops, or alters 
    targeting the core_constitution table, or modifying non-agent-modifiable system configs.
    """
    sql_clean = re.sub(r'\s+', ' ', sql_query).strip().lower()
    
    # Check for direct modifications to core_constitution table
    if "core_constitution" in sql_clean:
        if any(op in sql_clean for op in ["insert", "update", "delete", "drop", "alter"]):
            raise SafetyViolationError(
                "Safety Violation: Direct mutation of the 'core_constitution' table is strictly forbidden."
            )

def validate_config_write(config_key: str):
    """
    Validates if a configuration key is modifiable by agents.
    If the key in system_config has is_agent_modifiable = 0, raises SafetyViolationError.
    """
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT is_agent_modifiable 
    FROM system_config 
    WHERE config_key = ?;
    """, (config_key,))
    row = cursor.fetchone()
    conn.close()
    
    # If key exists and is non-modifiable, block it
    if row is not None and row[0] == 0:
        raise SafetyViolationError(
            f"Safety Violation: Configuration key '{config_key}' is human-locked and cannot be modified by the agent swarm."
        )

def validate_action(proposed_action: str) -> bool:
    """
    Evaluates a proposed action string against the sealed constitution guidelines in SQLite.
    Checks for banned directories/paths and restricted domains.
    """
    logger.info(f"Middleware evaluating proposed action: '{proposed_action}'")
    
    # 1. Check default config banned websites
    import src.config
    for site in src.config.DEFAULT_BANNED_WEBSITES:
        pattern = re.compile(re.escape(site), re.IGNORECASE)
        if pattern.search(proposed_action):
            raise SafetyViolationError(
                f"Safety Violation: Action contains references to default restricted boundary path/domain '{site}'."
            )
            
    # 2. Check rules from constitution
    rules = get_constitution()
    
    # Extract rules from constitution
    banned_boundaries = ""
    for key, text in rules:
        if key.upper() == "BANNED_BOUNDARIES":
            banned_boundaries = text
            break
            
    if banned_boundaries:
        # Split banned boundaries into list
        boundaries = [b.strip() for b in banned_boundaries.split(",") if b.strip()]
        
        for boundary in boundaries:
            # Check if boundary is a path or a domain
            if boundary.startswith("/") or boundary.startswith("\\") or "." in boundary:
                # Compile a case-insensitive boundary search
                pattern = re.compile(re.escape(boundary), re.IGNORECASE)
                if pattern.search(proposed_action):
                    raise SafetyViolationError(
                        f"Safety Violation: Action contains references to restricted boundary path/domain '{boundary}'."
                    )
                    
    logger.info("Middleware validation passed.")
    return True

def check_loop_safety():
    """
    Enforces the Loop Safety Valve.
    Reads consecutive_background_loops and n_loop_limit from SQLite.
    If consecutive_background_loops > n_loop_limit, raises SafetyViolationError.
    """
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    
    # Read loop counter
    cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'consecutive_background_loops';")
    row = cursor.fetchone()
    counter = int(row[0]) if row else 0
    
    # Read loop limit
    cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'n_loop_limit';")
    row_limit = cursor.fetchone()
    limit = int(row_limit[0]) if row_limit else 5
    
    conn.close()
    
    if counter > limit:
        raise SafetyViolationError(
            f"Loop Safety Valve triggered: consecutive background loops ({counter}) exceeded limit ({limit})."
        )

