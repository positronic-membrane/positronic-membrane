import base64
import sqlite3
import threading
from unittest.mock import patch

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

def test_skills_library_ref_seeded_and_locked():
    """skills.library_ref (issue #104) seeds as 'v1' and is not agent-modifiable."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT config_value, is_agent_modifiable FROM system_config WHERE config_key = ?;",
        ("skills.library_ref",),
    )
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "v1"
    assert row[1] == 0

def test_handoff_filter_untrusted_authors_seeded_and_locked():
    """handoff.filter_untrusted_authors (issue #107) seeds default-on ('1') and
    is not agent-modifiable."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT config_value, is_agent_modifiable FROM system_config WHERE config_key = ?;",
        ("handoff.filter_untrusted_authors",),
    )
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "1"
    assert row[1] == 0

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

def test_concurrent_writes_do_not_lock_database():
    """
    Simulates the background daemon's heartbeat logging (src/daemon.py, speaker
    'system' / context_type 'background_thought') racing against the web server's
    chat handler (src/routers/chat.py, speaker 'user'/'persona' / context_type
    'user_visible') writing to the same episodic_memory table concurrently.
    Verifies WAL mode + busy_timeout prevent 'database is locked' errors.
    """
    iterations = 50
    errors = []

    def daemon_writer():
        for i in range(iterations):
            try:
                log_episodic_memory("system", f"heartbeat {i}", "background_thought")
            except Exception as e:
                errors.append(e)

    def web_server_writer():
        for i in range(iterations):
            try:
                speaker = "user" if i % 2 == 0 else "persona"
                log_episodic_memory(speaker, f"chat turn {i}", "user_visible", party_id="local_user")
            except Exception as e:
                errors.append(e)

    threads = [
        threading.Thread(target=daemon_writer),
        threading.Thread(target=daemon_writer),
        threading.Thread(target=web_server_writer),
        threading.Thread(target=web_server_writer),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent writes raised errors: {errors}"

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM episodic_memory;")
    assert cursor.fetchone()[0] == iterations * 4
    cursor.execute("SELECT COUNT(*) FROM episodic_memory WHERE context_type = 'background_thought';")
    assert cursor.fetchone()[0] == iterations * 2
    cursor.execute("SELECT COUNT(*) FROM episodic_memory WHERE context_type = 'user_visible';")
    assert cursor.fetchone()[0] == iterations * 2
    conn.close()


def test_janus_documents_backfill_migration_adds_purpose_and_metadata(tmp_path):
    """Pre-existing DBs created before V2-T2 only have the original 5 janus_documents columns;
    re-running init_db() must backfill purpose/metadata without dropping existing rows."""
    legacy_db = tmp_path / "legacy_janus.db"
    conn = sqlite3.connect(str(legacy_db))
    conn.execute("""
        CREATE TABLE janus_documents (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL UNIQUE,
            content    TEXT NOT NULL DEFAULT '',
            tags       TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute("INSERT INTO janus_documents (title, content) VALUES ('Pre-existing Doc', 'old content');")
    conn.commit()
    conn.close()

    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(legacy_db)
    try:
        init_db()
        conn = get_connection(read_only_constitution=True)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(janus_documents);")
        columns = {row[1] for row in cursor.fetchall()}
        assert {"purpose", "metadata"}.issubset(columns)

        cursor.execute(
            "SELECT title, content, purpose, metadata FROM janus_documents WHERE title = 'Pre-existing Doc';"
        )
        row = cursor.fetchone()
        conn.close()
        assert row[1] == "old content"
        assert row[2] == "memory"
        assert row[3] == "{}"
    finally:
        src.config.DB_PATH = orig_db_path


def test_goal_checkpoints_backfill_migration_adds_completed_by_party_id(tmp_path):
    """Pre-existing DBs created before issue #63 only have the original 5
    goal_checkpoints columns; re-running init_db() must backfill
    completed_by_party_id (NULL for pre-existing rows) without dropping data."""
    legacy_db = tmp_path / "legacy_goal_checkpoints.db"
    conn = sqlite3.connect(str(legacy_db))
    conn.execute("""
        CREATE TABLE goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            status TEXT NOT NULL,
            description TEXT NOT NULL,
            progress_metric TEXT,
            parent_goal_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute("""
        CREATE TABLE goal_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id INTEGER NOT NULL,
            checkpoint_description TEXT NOT NULL,
            achieved INTEGER DEFAULT 0 CHECK(achieved IN (0, 1)),
            achieved_at TIMESTAMP,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE CASCADE
        );
    """)
    conn.execute("INSERT INTO goals (id, type, status, description) VALUES (1, 'short', 'completed', 'legacy goal');")
    conn.execute(
        "INSERT INTO goal_checkpoints (goal_id, checkpoint_description, achieved, achieved_at) "
        "VALUES (1, 'legacy checkpoint', 1, '2026-01-01T00:00:00');"
    )
    conn.commit()
    conn.close()

    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(legacy_db)
    try:
        init_db()
        conn = get_connection(read_only_constitution=True)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(goal_checkpoints);")
        columns = {row[1] for row in cursor.fetchall()}
        assert "completed_by_party_id" in columns

        cursor.execute(
            "SELECT checkpoint_description, achieved, completed_by_party_id FROM goal_checkpoints WHERE goal_id = 1;"
        )
        row = cursor.fetchone()
        conn.close()
        assert row[0] == "legacy checkpoint"
        assert row[1] == 1
        assert row[2] is None

        # Idempotent: running init_db() again must not error or duplicate the column.
        init_db()
    finally:
        src.config.DB_PATH = orig_db_path


def test_external_agents_legacy_encryption_migration(tmp_path, monkeypatch):
    """Pre-fix rows encrypted with the legacy XOR scheme (using the old
    hardcoded default key, since no JANUS_ENCRYPTION_KEY was ever set) must
    be transparently re-encrypted to fernet:v1: on init_db(), given a
    JANUS_ENCRYPTION_KEY is now configured."""
    from src.security import _LEGACY_DEFAULT_KEY_DIGEST_HEX, decrypt_api_key

    legacy_default_digest = bytes.fromhex(_LEGACY_DEFAULT_KEY_DIGEST_HEX)
    plaintext_key = "sk-legacy-abc123"
    xor_bytes = bytes(
        b ^ legacy_default_digest[i % len(legacy_default_digest)]
        for i, b in enumerate(plaintext_key.encode("utf-8"))
    )
    legacy_ciphertext = base64.b64encode(xor_bytes).decode("utf-8")

    legacy_db = tmp_path / "legacy_agents.db"
    conn = sqlite3.connect(str(legacy_db))
    conn.execute("""
        CREATE TABLE external_agents (
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
    conn.execute(
        "INSERT INTO external_agents (name, type, endpoint, api_key_encrypted) "
        "VALUES ('legacy-agent', 'api', 'https://example.com', ?);",
        (legacy_ciphertext,),
    )
    conn.commit()
    conn.close()

    orig_db_path = src.config.DB_PATH
    monkeypatch.setattr(src.config, "JANUS_ENCRYPTION_KEY", "new-real-secret-key")
    src.config.DB_PATH = str(legacy_db)
    try:
        init_db()
        conn = get_connection(read_only_constitution=True)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT api_key_encrypted FROM external_agents WHERE name = 'legacy-agent';"
        )
        row = cursor.fetchone()
        conn.close()
        new_enc = row[0]
        assert new_enc.startswith("fernet:v1:")
        assert decrypt_api_key(new_enc) == plaintext_key
    finally:
        src.config.DB_PATH = orig_db_path


def test_external_agents_migration_is_idempotent(tmp_path, monkeypatch):
    """A second init_db() run must not re-encrypt already-migrated rows."""
    from src.security import _LEGACY_DEFAULT_KEY_DIGEST_HEX

    legacy_default_digest = bytes.fromhex(_LEGACY_DEFAULT_KEY_DIGEST_HEX)
    plaintext_key = "sk-legacy-xyz789"
    xor_bytes = bytes(
        b ^ legacy_default_digest[i % len(legacy_default_digest)]
        for i, b in enumerate(plaintext_key.encode("utf-8"))
    )
    legacy_ciphertext = base64.b64encode(xor_bytes).decode("utf-8")

    legacy_db = tmp_path / "legacy_agents_idempotent.db"
    conn = sqlite3.connect(str(legacy_db))
    conn.execute("""
        CREATE TABLE external_agents (
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
    conn.execute(
        "INSERT INTO external_agents (name, type, endpoint, api_key_encrypted) "
        "VALUES ('legacy-agent', 'api', 'https://example.com', ?);",
        (legacy_ciphertext,),
    )
    conn.commit()
    conn.close()

    orig_db_path = src.config.DB_PATH
    monkeypatch.setattr(src.config, "JANUS_ENCRYPTION_KEY", "new-real-secret-key")
    src.config.DB_PATH = str(legacy_db)
    try:
        init_db()
        conn = get_connection(read_only_constitution=True)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT api_key_encrypted FROM external_agents WHERE name = 'legacy-agent';"
        )
        first_enc = cursor.fetchone()[0]
        conn.close()

        init_db()
        conn = get_connection(read_only_constitution=True)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT api_key_encrypted FROM external_agents WHERE name = 'legacy-agent';"
        )
        second_enc = cursor.fetchone()[0]
        conn.close()

        assert first_enc == second_enc
    finally:
        src.config.DB_PATH = orig_db_path


@patch("src.notifications.send_webhook_notification")
def test_log_deliberation_sends_webhook_on_veto(mock_webhook):
    """A Critic veto (critic_decision=0) must dispatch a webhook notification."""
    log_deliberation(
        proposed_action="modify_code: src/foo.py",
        debate_json={"proposer_output": "x", "critic_output": "y"},
        critic_decision=0,
        utility_score=0.0,
        justification="Violates constitution rule X",
    )
    mock_webhook.assert_called_once()
    event_type, message = mock_webhook.call_args[0]
    assert event_type == "critic_veto"
    assert "modify_code: src/foo.py" in message


@patch("src.notifications.send_webhook_notification")
def test_log_deliberation_no_webhook_on_approval(mock_webhook):
    """An approved action (critic_decision=1) must not dispatch any webhook notification."""
    log_deliberation(
        proposed_action="scan_workspace",
        debate_json={"proposer_output": "x", "critic_output": "y"},
        critic_decision=1,
        utility_score=1.0,
        justification="Safe and compliant",
    )
    mock_webhook.assert_not_called()
