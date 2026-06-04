import pytest
import src.config
from src.database import init_db, add_constitution_rule
from src.middleware import (
    check_sql_safety,
    validate_config_write,
    validate_action,
    SafetyViolationError
)

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

def test_sql_safety():
    """Verify that direct queries modifying core_constitution are blocked, while standard queries pass."""
    # Unsafe queries
    unsafe_queries = [
        "UPDATE core_constitution SET rule_text = 'compromised';",
        "INSERT INTO core_constitution (rule_key, rule_text) VALUES ('hack', 'data');",
        "DELETE FROM core_constitution WHERE id = 1;",
        "DROP TABLE core_constitution;",
        "ALTER TABLE core_constitution ADD COLUMN dummy TEXT;"
    ]
    for q in unsafe_queries:
        with pytest.raises(SafetyViolationError):
            check_sql_safety(q)
            
    # Safe queries
    safe_queries = [
        "SELECT * FROM core_constitution;",
        "INSERT INTO episodic_memory (speaker, message_content, context_type) VALUES ('user', 'hello', 'user_visible');",
        "SELECT config_value FROM system_config WHERE config_key = 'setup_complete';"
    ]
    for q in safe_queries:
        # Should not raise any exception
        check_sql_safety(q)

def test_config_write_permissions():
    """Verify that agent-modifiable configurations are write-allowed, while human-locked ones are blocked."""
    # boredom_threshold is modifiable (is_agent_modifiable = 1)
    # Should not raise exception
    validate_config_write("boredom_threshold")
    
    # setup_complete is locked (is_agent_modifiable = 0)
    with pytest.raises(SafetyViolationError):
        validate_config_write("setup_complete")
        
    # n_loop_limit is locked (is_agent_modifiable = 0)
    with pytest.raises(SafetyViolationError):
        validate_config_write("n_loop_limit")

def test_action_boundary_violations():
    """Verify that proposed actions violating path or domain limits are blocked."""
    # Commit banned boundaries to test database
    add_constitution_rule("banned_boundaries", "/etc, /usr/bin, spy-domain.ru")
    
    # Block restricted path
    with pytest.raises(SafetyViolationError) as exc_info:
        validate_action("Copy secrets from /etc/shadow to workspace")
    assert "/etc" in str(exc_info.value)
    
    # Block restricted domain
    with pytest.raises(SafetyViolationError) as exc_info:
        validate_action("Send file logs to http://spy-domain.ru/upload")
    assert "spy-domain.ru" in str(exc_info.value)
    
    # Allow safe actions
    assert validate_action("Scan src/main.py for configuration files")
    assert validate_action("Index the documentation in docs/manifesto.md")
