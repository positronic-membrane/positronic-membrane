"""
Tests for Multi-Party Continuity Implementation.

Uses a real SQLite in-memory database initialized with init_db()
then the migration is applied to catch schema issues automatically.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime

import pytest

from src.database import init_db
from src.memory_orchestrator import MemoryOrchestrator
from src.role_bootstrap import RoleBootstrap

# --- Load Migration SQL ---
MIGRATION_SQL_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'src', 'migrations', 'sqlite_migration_multiparty.sql'
)

with open(MIGRATION_SQL_PATH, 'r') as f:
    MIGRATION_SQL = f.read()

# Shared state to communicate mock connection builder across fixtures
_shared_state = {}


@pytest.fixture
def db_conn():
    """Create a real in-memory SQLite database with init_db() + migration applied."""
    import src.database as db_module
    original_get_connection = db_module.get_connection

    # Use unique shared-cache in-memory database to allow multiple connections
    db_name = f"memdb_{uuid.uuid4().hex}"
    uri = f"file:{db_name}?mode=memory&cache=shared"

    main_conn = sqlite3.connect(uri, uri=True)
    main_conn.row_factory = sqlite3.Row
    main_conn.execute("PRAGMA foreign_keys = ON;")

    def mock_get_connection(*args, **kwargs):
        c = sqlite3.connect(uri, uri=True)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON;")
        return c

    db_module.get_connection = mock_get_connection

    # Initialize core schema (creates episodic_memory, etc.) and automatically applies migrations
    init_db()

    # Store mock connection builder for downstream fixtures
    _shared_state['mock_get_connection'] = mock_get_connection

    yield main_conn

    # Restore original
    db_module.get_connection = original_get_connection
    main_conn.close()


@pytest.fixture
def memory_orch(db_conn):
    """Create a MemoryOrchestrator that uses the in-memory database."""
    import src.memory_orchestrator as mo_module
    original_get_connection = mo_module.get_connection

    mo_module.get_connection = _shared_state['mock_get_connection']

    orch = MemoryOrchestrator()
    yield orch

    mo_module.get_connection = original_get_connection


@pytest.fixture
def bootstrap(db_conn):
    """Create a RoleBootstrap that uses the in-memory database."""
    import src.role_bootstrap as rb_module
    original_get_connection = rb_module.get_connection

    rb_module.get_connection = _shared_state['mock_get_connection']

    bs = RoleBootstrap()
    yield bs

    rb_module.get_connection = original_get_connection


@pytest.fixture
def sample_party(db_conn):
    """Create a sample party in the in-memory database for tests."""
    party_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    db_conn.execute(
        'INSERT INTO parties (id, name, role, created_at) VALUES (?, ?, ?, ?)',
        (party_id, 'TestParty', 'admin', now)
    )
    db_conn.commit()
    return party_id


@pytest.fixture
def sample_session(db_conn, sample_party):
    """Create a sample session in the in-memory database."""
    session_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    db_conn.execute(
        'INSERT INTO sessions (id, party_id, started_at) VALUES (?, ?, ?)',
        (session_id, sample_party, now)
    )
    db_conn.commit()
    return session_id


# --- Memory Orchestrator Tests ---

class TestMemoryOrchestrator:
    """Tests for MemoryOrchestrator party-scoped memory operations."""

    def test_set_memory_new(self, memory_orch, db_conn, sample_party):
        """Test setting a new memory record."""
        result = memory_orch.set_memory(
            party_id=sample_party,
            key='test_key',
            value={'data': 'test_value'},
            namespace='test_ns'
        )
        assert result is not None
        assert isinstance(result, str)

        row = db_conn.execute(
            'SELECT * FROM memories WHERE id = ?', (result,)
        ).fetchone()
        assert row is not None
        assert row['key'] == 'test_key'
        assert json.loads(row['value']) == {'data': 'test_value'}
        assert row['namespace'] == 'test_ns'
        assert row['party_id'] == sample_party

    def test_set_memory_update(self, memory_orch, db_conn, sample_party):
        """Test updating an existing memory record."""
        first_id = memory_orch.set_memory(
            party_id=sample_party,
            key='test_key',
            value={'data': 'initial'},
            namespace='test_ns'
        )

        second_id = memory_orch.set_memory(
            party_id=sample_party,
            key='test_key',
            value={'data': 'updated'},
            namespace='test_ns'
        )

        assert first_id == second_id

        row = db_conn.execute(
            'SELECT * FROM memories WHERE id = ?', (first_id,)
        ).fetchone()
        assert json.loads(row['value']) == {'data': 'updated'}

    def test_get_memory_found(self, memory_orch, db_conn, sample_party):
        """Test retrieving an existing memory."""
        memory_orch.set_memory(
            party_id=sample_party,
            key='test_key',
            value={'data': 'test_value'},
            namespace='test_ns'
        )

        result = memory_orch.get_memory(
            party_id=sample_party,
            key='test_key',
            namespace='test_ns'
        )

        assert result == {'data': 'test_value'}

    def test_get_memory_not_found(self, memory_orch):
        """Test retrieving a non-existent memory."""
        result = memory_orch.get_memory(
            party_id='nonexistent-party',
            key='nonexistent_key',
            namespace='test_ns'
        )
        assert result is None

    def test_get_all_keys(self, memory_orch, db_conn, sample_party):
        """Test listing all keys for a party."""
        memory_orch.set_memory(sample_party, 'key1', 'val1', 'ns1')
        memory_orch.set_memory(sample_party, 'key2', 'val2', 'ns1')
        memory_orch.set_memory(sample_party, 'key3', 'val3', 'ns1')

        result = memory_orch.get_all_keys(sample_party, 'ns1')
        assert sorted(result) == sorted(['key1', 'key2', 'key3'])

    def test_get_all_keys_isolated(self, memory_orch, db_conn, sample_party):
        """Test that keys from different parties are isolated."""
        other_party = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        db_conn.execute(
            'INSERT INTO parties (id, name, role, created_at) VALUES (?, ?, ?, ?)',
            (other_party, 'OtherParty', 'user', now)
        )
        db_conn.commit()

        memory_orch.set_memory(sample_party, 'key1', 'val1', 'global')
        memory_orch.set_memory(other_party, 'key2', 'val2', 'global')

        result = memory_orch.get_all_keys(sample_party, 'global')
        assert result == ['key1']
        assert 'key2' not in result

    def test_delete_memory_success(self, memory_orch, db_conn, sample_party):
        """Test successful memory deletion."""
        memory_orch.set_memory(sample_party, 'test_key', 'test_value', 'test_ns')
        assert memory_orch.get_memory(sample_party, 'test_key', 'test_ns') is not None

        result = memory_orch.delete_memory(sample_party, 'test_key', 'test_ns')
        assert result is True

        assert memory_orch.get_memory(sample_party, 'test_key', 'test_ns') is None

    def test_get_party_memories(self, memory_orch, db_conn, sample_party):
        """Test retrieving all memories for a party."""
        memory_orch.set_memory(sample_party, 'key1', 'val1', 'ns1')
        memory_orch.set_memory(sample_party, 'key2', 'val2', 'ns1')
        memory_orch.set_memory(sample_party, 'key3', 'val3', 'ns2')

        result = memory_orch.get_party_memories(sample_party)
        assert 'ns1' in result
        assert 'ns2' in result
        assert result['ns1']['key1'] == 'val1'
        assert result['ns1']['key2'] == 'val2'
        assert result['ns2']['key3'] == 'val3'

    def test_log_episodic_memory(self, memory_orch, db_conn, sample_party, sample_session):
        """Test logging an episodic memory with party scoping."""
        record_id = memory_orch.log_episodic_memory(
            party_id=sample_party,
            session_id=sample_session,
            message_content='Hello, world!',
            speaker='user'
        )
        assert isinstance(record_id, int)
        assert record_id > 0

        row = db_conn.execute(
            'SELECT * FROM episodic_memory WHERE id = ?', (record_id,)
        ).fetchone()
        assert row is not None
        assert row['message_content'] == 'Hello, world!'
        assert row['speaker'] == 'user'
        assert row['party_id'] == sample_party
        assert row['session_id'] == sample_session

    def test_get_episodic_memories(self, memory_orch, db_conn, sample_party, sample_session):
        """Test retrieving episodic memories for a party."""
        id1 = memory_orch.log_episodic_memory(
            party_id=sample_party, session_id=sample_session,
            message_content='First', speaker='user'
        )
        id2 = memory_orch.log_episodic_memory(
            party_id=sample_party, session_id=sample_session,
            message_content='Second', speaker='assistant'
        )

        result = memory_orch.get_episodic_memories(sample_party, limit=10)
        assert len(result) == 2
        assert result[0]['message_content'] in ('Second', 'First')
        assert result[0]['speaker'] in ('assistant', 'user')

    def test_episodic_memory_auto_increment(self, memory_orch, db_conn, sample_party):
        """Test that episodic memory IDs are auto-incremented integers."""
        id1 = memory_orch.log_episodic_memory(
            party_id=sample_party, session_id=None,
            message_content='First', speaker='user'
        )
        id2 = memory_orch.log_episodic_memory(
            party_id=sample_party, session_id=None,
            message_content='Second', speaker='user'
        )
        assert id2 == id1 + 1


# --- Role Bootstrap Tests ---

class TestRoleBootstrap:
    """Tests for RoleBootstrap first-run ceremony."""

    def test_is_bootstrap_required_true(self, bootstrap, db_conn):
        """Test detection of empty parties table."""
        assert bootstrap.is_bootstrap_required() is True

    def test_is_bootstrap_required_false(self, bootstrap, db_conn, sample_party):
        """Test detection of populated parties table."""
        assert bootstrap.is_bootstrap_required() is False

    def test_generate_enrollment_key(self, bootstrap):
        """Test enrollment key generation."""
        key = bootstrap.generate_enrollment_key()
        assert len(key) == 64
        assert isinstance(key, str)

    def test_create_root_admin(self, bootstrap, db_conn):
        """Test root admin creation."""
        party_id, key = bootstrap.create_root_admin(
            name='test_admin',
            enrollment_key='test-key-123'
        )
        assert party_id is not None
        assert key == 'test-key-123'

        row = db_conn.execute(
            'SELECT * FROM parties WHERE id = ?', (party_id,)
        ).fetchone()
        assert row is not None
        assert row['name'] == 'test_admin'
        assert row['role'] == 'admin'
        assert row['public_key'] == 'test-key-123'

    def test_create_root_admin_already_exists(self, bootstrap, db_conn, sample_party):
        """Test that root admin creation fails if parties exist."""
        with pytest.raises(RuntimeError, match='Bootstrap already completed'):
            bootstrap.create_root_admin()

    def test_check_web_ui_bootstrap_required(self, bootstrap, db_conn):
        """Test web UI bootstrap check when required."""
        result = bootstrap.check_web_ui_bootstrap()
        assert result['bootstrap_required'] is True
        assert 'Setup required' in result['message']

    def test_check_web_ui_bootstrap_not_required(self, bootstrap, db_conn, sample_party):
        """Test web UI bootstrap check when not required."""
        result = bootstrap.check_web_ui_bootstrap()
        assert result['bootstrap_required'] is False
        assert 'System is ready' in result['message']


# --- Database Schema Tests ---

class TestDatabaseSchema:
    """Tests to validate the migration schema directly."""

    def test_parties_table_exists(self, db_conn):
        """Verify parties table has correct columns."""
        cursor = db_conn.execute("PRAGMA table_info(parties)")
        columns = {row['name']: row for row in cursor.fetchall()}

        assert 'id' in columns
        assert 'name' in columns
        assert 'role' in columns
        assert 'created_at' in columns
        assert 'public_key' in columns

    def test_parties_role_check(self, db_conn):
        """Verify role CHECK constraint accepts valid roles."""
        for role in ('user', 'contributor', 'admin', 'observer'):
            db_conn.execute(
                "INSERT INTO parties (id, name, role) VALUES (?, ?, ?)",
                (str(uuid.uuid4()), f'party_{role}', role)
            )
        db_conn.commit()

    def test_parties_role_check_invalid(self, db_conn):
        """Verify role CHECK constraint rejects invalid roles."""
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                "INSERT INTO parties (id, name, role) VALUES (?, ?, ?)",
                (str(uuid.uuid4()), 'bad_party', 'superadmin')
            )
            db_conn.commit()

    def test_memories_unique_constraint(self, db_conn, sample_party):
        """Verify unique constraint on (party_id, namespace, key)."""
        db_conn.execute(
            'INSERT INTO memories (id, party_id, key, value, namespace) VALUES (?, ?, ?, ?, ?)',
            (str(uuid.uuid4()), sample_party, 'dup_key', '"val1"', 'global')
        )
        db_conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                'INSERT INTO memories (id, party_id, key, value, namespace) VALUES (?, ?, ?, ?, ?)',
                (str(uuid.uuid4()), sample_party, 'dup_key', '"val2"', 'global')
            )
            db_conn.commit()

    def test_episodic_memory_auto_increment(self, db_conn, sample_party):
        """Verify episodic_memory id is auto-increment integer."""
        now = datetime.utcnow().isoformat()
        db_conn.execute(
            'INSERT INTO episodic_memory (message_content, speaker, timestamp, party_id, context_type) VALUES (?, ?, ?, ?, ?)',
            ('test', 'user', now, sample_party, 'user_visible')
        )
        db_conn.commit()
        row = db_conn.execute('SELECT id FROM episodic_memory').fetchone()
        assert isinstance(row['id'], int)

    def test_foreign_key_cascade(self, db_conn):
        """Verify that deleting a party cascades to memories."""
        party_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        db_conn.execute(
            'INSERT INTO parties (id, name, role, created_at) VALUES (?, ?, ?, ?)',
            (party_id, 'CascadeTest', 'user', now)
        )
        db_conn.execute(
            'INSERT INTO memories (id, party_id, key, value) VALUES (?, ?, ?, ?)',
            (str(uuid.uuid4()), party_id, 'test_key', '"val"')
        )
        db_conn.commit()

        db_conn.execute('DELETE FROM parties WHERE id = ?', (party_id,))
        db_conn.commit()

        row = db_conn.execute(
            'SELECT COUNT(*) as cnt FROM memories WHERE party_id = ?', (party_id,)
        ).fetchone()
        assert row['cnt'] == 0

    def test_preferences_table_exists(self, db_conn):
        """Verify preferences table has correct columns."""
        cursor = db_conn.execute("PRAGMA table_info(preferences)")
        columns = {row['name']: row for row in cursor.fetchall()}

        assert 'id' in columns
        assert 'party_id' in columns
        assert 'preference_key' in columns
        assert 'preference_value' in columns
        assert 'created_at' in columns
        assert 'updated_at' in columns

    def test_preferences_uniqueness(self, db_conn, sample_party):
        """Verify unique constraint on (party_id, preference_key)."""
        db_conn.execute(
            'INSERT INTO preferences (party_id, preference_key, preference_value) VALUES (?, ?, ?)',
            (sample_party, 'theme', 'dark')
        )
        db_conn.commit()

        # Duplicate should fail
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                'INSERT INTO preferences (party_id, preference_key, preference_value) VALUES (?, ?, ?)',
                (sample_party, 'theme', 'light')
            )
            db_conn.commit()


# --- Web Server Integration Tests ---

class TestWebServerEndpoints:
    """Integration tests for the multi-party API via the web server."""

    @pytest.fixture
    def api_client(self, db_conn):
        """Create a TestClient connected to the test database."""
        from fastapi.testclient import TestClient

        import src.web_server as ws_module

        original_get_connection = ws_module.get_connection
        ws_module.get_connection = _shared_state['mock_get_connection']

        original_orch_get_conn = ws_module.memory_orch._get_connection
        original_bs_get_conn = ws_module.bootstrap._get_connection

        ws_module.memory_orch._get_connection = _shared_state['mock_get_connection']
        ws_module.bootstrap._get_connection = _shared_state['mock_get_connection']

        client = TestClient(ws_module.app)
        yield client

        ws_module.get_connection = original_get_connection
        ws_module.memory_orch._get_connection = original_orch_get_conn
        ws_module.bootstrap._get_connection = original_bs_get_conn

    def test_bootstrap_status(self, api_client, db_conn):
        """Test bootstrap status endpoint."""
        resp = api_client.get('/api/v1/bootstrap/status')
        assert resp.status_code == 200
        assert resp.json()['bootstrap_required'] is True

    def test_register_party(self, api_client, db_conn):
        """Test party registration as admin."""
        admin_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        db_conn.execute(
            'INSERT INTO parties (id, name, role, created_at) VALUES (?, ?, ?, ?)',
            (admin_id, 'AdminUser', 'admin', now)
        )
        db_conn.commit()

        resp = api_client.post(
            '/api/v1/party/register',
            json={'name': 'NewParty', 'role': 'contributor'},
            headers={'X-Party-ID': admin_id}
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data['name'] == 'NewParty'
        assert data['role'] == 'contributor'

    def test_write_and_read_memory(self, api_client, db_conn):
        """Test writing and reading memory via API."""
        party_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        db_conn.execute(
            'INSERT INTO parties (id, name, role, created_at) VALUES (?, ?, ?, ?)',
            (party_id, 'TestUser', 'user', now)
        )
        db_conn.commit()

        resp = api_client.post(
            '/api/v1/memory',
            json={'key': 'api_key', 'value': 'api_value', 'namespace': 'api_test'},
            headers={'X-Party-ID': party_id}
        )
        assert resp.status_code == 201
        assert 'memory_id' in resp.json()

        resp = api_client.get(
            '/api/v1/memory/api_key?namespace=api_test',
            headers={'X-Party-ID': party_id}
        )
        assert resp.status_code == 200
        assert resp.json()['value'] == 'api_value'

    def test_register_party_with_metadata(self, api_client, db_conn):
        """Test registering a party with metadata and retrieving it."""
        admin_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        db_conn.execute(
            'INSERT INTO parties (id, name, role, created_at) VALUES (?, ?, ?, ?)',
            (admin_id, 'AdminUser2', 'admin', now)
        )
        db_conn.commit()

        metadata = {'key': 'val', 'nested': {'num': 42}}
        resp = api_client.post(
            '/api/v1/party/register',
            json={'name': 'PartyWithMeta', 'role': 'user', 'metadata': metadata},
            headers={'X-Party-ID': admin_id}
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data['name'] == 'PartyWithMeta'
        assert data['metadata'] == metadata
        assert 'last_seen' in data

        party_id = data['party_id']

        # Now query the party
        resp = api_client.get(
            f'/api/v1/party/{party_id}',
            headers={'X-Party-ID': admin_id}
        )
        assert resp.status_code == 200
        data_get = resp.json()
        assert data_get['metadata'] == metadata
        assert 'last_seen' in data_get

    def test_last_seen_updates_on_auth(self, api_client, db_conn):
        """Test that last_seen updates when requests are authenticated."""
        party_id = str(uuid.uuid4())
        past_time = "2020-01-01T00:00:00"
        db_conn.execute(
            'INSERT INTO parties (id, name, role, created_at, last_seen) VALUES (?, ?, ?, ?, ?)',
            (party_id, 'ActiveUser', 'user', past_time, past_time)
        )
        db_conn.commit()

        # Perform a request that uses require_role
        resp = api_client.get(
            f'/api/v1/party/{party_id}',
            headers={'X-Party-ID': party_id}
        )
        assert resp.status_code == 200

        # Verify last_seen was updated in DB
        row = db_conn.execute('SELECT last_seen FROM parties WHERE id = ?', (party_id,)).fetchone()
        assert row['last_seen'] != past_time
        assert row['last_seen'] > past_time


class TestHeartbeatMaintenance:
    """Tests for low-priority background maintenance tasks."""

    def test_maintenance_updates_system_party(self, db_conn):
        """Verify run_background_maintenance updates last_seen for system party."""
        from src.daemon import run_background_maintenance

        # Ensure system party exists in the test DB
        row = db_conn.execute("SELECT last_seen FROM parties WHERE name = 'system'").fetchone()
        assert row is not None
        initial_seen = row['last_seen']

        import time
        time.sleep(0.1) # small delay to guarantee timestamp progression

        run_background_maintenance()

        row_after = db_conn.execute("SELECT last_seen FROM parties WHERE name = 'system'").fetchone()
        assert row_after['last_seen'] > initial_seen

    def test_maintenance_closes_inactive_sessions(self, db_conn, sample_party):
        """Verify sessions inactive for > 30 minutes are closed."""
        from src.daemon import run_background_maintenance

        session_id = str(uuid.uuid4())

        # Insert a session that has been inactive:
        # Party was last seen 45 minutes ago
        past_time = "2020-01-01T00:00:00"
        db_conn.execute(
            "UPDATE parties SET last_seen = ? WHERE id = ?",
            (past_time, sample_party)
        )
        db_conn.execute(
            "INSERT INTO sessions (id, party_id, started_at, ended_at) VALUES (?, ?, ?, NULL)",
            (session_id, sample_party, past_time)
        )
        db_conn.commit()

        # Run maintenance
        run_background_maintenance()

        # Verify session has been closed with ended_at set to the party's last_seen (past_time)
        row = db_conn.execute("SELECT ended_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
        assert row['ended_at'] == past_time
