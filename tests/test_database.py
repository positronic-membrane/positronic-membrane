import sqlite3

import pytest

import src.config
from src.database import (
    add_constitution_rule,
    get_boredom_counter,
    get_connection,
    get_constitution,
    get_curiosity_vector,
    increment_boredom,
    init_db,
    is_setup_complete,
    log_deliberation,
    log_episodic_memory,
    mark_setup_complete,
    reset_boredom,
    update_curiosity_vector,
)


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """
    Redirects config.DB_PATH to a temporary file for the duration of each test
    to guarantee isolation and prevent production database pollution.
    """
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)

    # Initialize the database for testing
    init_db()

    yield

    # Cleanup
    src.config.DB_PATH = orig_db_path

def test_database_initialization():
    """Verify that all tables exist after initialization."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()

    # Check tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {row[0] for row in cursor.fetchall()}

    expected_tables = {
        "core_constitution",
        "internal_deliberations",
        "episodic_memory",
        "drive_state",
        "agent_registry",
        "system_config"
    }

    assert expected_tables.issubset(tables)
    conn.close()

def test_default_values():
    """Verify that default config and agents are pre-populated."""
    assert not is_setup_complete()

    # Check default agent registry contains proposer and critic
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("SELECT agent_id FROM agent_registry;")
    agents = {row[0] for row in cursor.fetchall()}
    assert {"proposer", "critic", "explorer", "archivist"}.issubset(agents)
    conn.close()

def test_write_prevention_on_constitution():
    """Verify that writing to core_constitution is blocked on standard connections."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()

    # Writing should be denied by the authorizer callback
    with pytest.raises(sqlite3.DatabaseError) as exc_info:
        cursor.execute("INSERT INTO core_constitution (rule_key, rule_text) VALUES ('test_key', 'test_text');")

    assert "not authorized" in str(exc_info.value).lower()
    conn.close()

def test_write_allowed_on_constitution_with_admin():
    """Verify that writing to core_constitution succeeds with an admin connection."""
    # This helper internally uses get_connection(read_only_constitution=False)
    add_constitution_rule("test_key", "test_value")

    rules = get_constitution()
    assert len(rules) == 1
    assert rules[0] == ("TEST_KEY", "test_value")

def test_boredom_state_queries():
    """Verify boredom incrementing and resetting functionality."""
    assert get_boredom_counter() == 0

    new_val = increment_boredom()
    assert new_val == 1
    assert get_boredom_counter() == 1

    increment_boredom()
    assert get_boredom_counter() == 2

    reset_boredom()
    assert get_boredom_counter() == 0

def test_curiosity_vector():
    """Verify updating and fetching the curiosity vector."""
    assert get_curiosity_vector() == []

    test_vector = ["git_hooks", "sqlite_wal_locks"]
    update_curiosity_vector(test_vector)
    assert get_curiosity_vector() == test_vector

def test_setup_marking():
    """Verify setup_complete key switching."""
    assert not is_setup_complete()
    mark_setup_complete()
    assert is_setup_complete()

def test_logging():
    """Verify logging episodic memory and deliberations doesn't throw errors."""
    # Should execute cleanly
    log_episodic_memory("user", "Hello Janus", "user_visible")
    log_deliberation("Scan workspace", {"proposer": "scan"}, 1, 0.9, "safe")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT speaker, message_content FROM episodic_memory;")
    row = cursor.fetchone()
    assert row == ("user", "Hello Janus")
    conn.close()
