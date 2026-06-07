import os
import pytest
from unittest.mock import MagicMock, patch
import src.config
from src.database import get_connection
from src.sandbox_session import (
    get_sandbox_executor,
    run_sandbox_tests,
    LocalSandboxExecutor,
    DockerSandboxExecutor,
    E2BSandboxExecutor
)
from src.skills import DynamicSkillExecutor, SafeReplication

# Isolates vector db and sqlite DB settings for safety
@pytest.fixture(autouse=True)
def setup_test_context(tmp_path):
    orig_db_path = src.config.DB_PATH
    orig_db_type = src.config.DB_TYPE
    orig_sandbox_provider = src.config.SANDBOX_PROVIDER
    orig_spawn_provider = src.config.SPAWN_PROVIDER
    
    src.config.DB_PATH = str(tmp_path / "test_janus.db")
    src.config.DB_TYPE = "sqlite"
    src.config.SANDBOX_PROVIDER = "local"
    src.config.SPAWN_PROVIDER = "local"
    
    yield
    
    src.config.DB_PATH = orig_db_path
    src.config.DB_TYPE = orig_db_type
    src.config.SANDBOX_PROVIDER = orig_sandbox_provider
    src.config.SPAWN_PROVIDER = orig_spawn_provider

def test_get_sandbox_executor_routing():
    # Local provider
    src.config.SANDBOX_PROVIDER = "local"
    assert isinstance(get_sandbox_executor(), LocalSandboxExecutor)
    
    # Docker provider
    src.config.SANDBOX_PROVIDER = "docker"
    assert isinstance(get_sandbox_executor(), DockerSandboxExecutor)
    
    # E2B provider
    src.config.SANDBOX_PROVIDER = "e2b"
    assert isinstance(get_sandbox_executor(), E2BSandboxExecutor)

@patch("psycopg2.connect")
def test_postgres_schema_isolation_connection(mock_connect):
    # Set config to postgres
    src.config.DB_TYPE = "postgres"
    src.config.DATABASE_URL = "postgresql://user:pass@host:port/dbname"
    
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_connect.return_value = mock_conn
    
    # Inject DB_SCHEMA into environment
    with patch.dict(os.environ, {"DB_SCHEMA": "janus_child_alpha"}):
        conn = get_connection(read_only_constitution=True)
        assert conn is not None
        
        # Verify schema DDL commands executed before role restrictions
        mock_cur.execute.assert_any_call("CREATE SCHEMA IF NOT EXISTS janus_child_alpha;")
        mock_cur.execute.assert_any_call("SET search_path TO janus_child_alpha, public;")
        mock_cur.execute.assert_any_call("SET ROLE janus_agent;")

@patch("src.sandbox_session.get_active_sandbox")
@patch("src.sandbox_session.save_sandbox_session")
def test_e2b_sandbox_executor_mock_run(mock_save, mock_get_active):
    # Set sandbox to e2b mode
    src.config.SANDBOX_PROVIDER = "e2b"
    src.config.E2B_API_KEY = "test_key"
    
    mock_get_active.return_value = {
        "active_sandbox_path": "/tmp/dummy_sandbox",
        "active_sandbox_branch": "janus/sandbox-test"
    }
    
    passed, logs = run_sandbox_tests()
    assert passed is True
    assert "E2B VM Sandbox Session Started" in logs
    mock_save.assert_called_once()
    assert mock_save.call_args[0][2] == "passed"

@patch("src.skills.get_connection")
@patch("shutil.copytree")
@patch("shutil.rmtree")
@patch("psycopg2.connect")
def test_spawn_child_postgres_schema(mock_connect, mock_rmtree, mock_copytree, mock_get_conn, tmp_path):
    # Set DB type to postgres
    src.config.DB_TYPE = "postgres"
    src.config.DATABASE_URL = "postgresql://user:pass@host:port/dbname"
    src.config.SPAWN_PROVIDER = "ecs"
    
    # Mock parent DB queries
    mock_parent_conn = MagicMock()
    mock_parent_cur = MagicMock()
    # Mock query returning schema and instincts
    mock_parent_cur.fetchall.side_effect = [
        [{"key": "schema_key", "value": "CREATE TABLE instincts (id SERIAL PRIMARY KEY);"}] * 1, # schemas
        [
            {"key": "core_constitution", "value": "[]", "category": "constitution", "version": 1},
            {"key": "agent_skills", "value": "[]", "category": "tool", "version": 1},
            {"key": "system_config", "value": "[]", "category": "boot", "version": 1}
        ] # instincts
    ]
    mock_parent_conn.cursor.return_value = mock_parent_cur
    mock_get_conn.return_value = mock_parent_conn
    
    # Mock psycopg2 with context managers
    mock_child_conn = MagicMock()
    mock_child_cur = MagicMock()
    mock_child_conn.cursor.return_value.__enter__.return_value = mock_child_cur
    mock_connect.return_value = mock_child_conn
    
    # Execute spawn child
    swarm = SafeReplication()
    res = swarm.spawn_child("child_alpha", "child_alpha")
    
    # Verify child database schema was initialized
    mock_child_cur.execute.assert_any_call("CREATE SCHEMA IF NOT EXISTS janus_child_child_alpha;")
    mock_child_cur.execute.assert_any_call("SET search_path TO janus_child_child_alpha;")
    
    # Verify return attributes for ECS spawning
    assert res["success"] is True
    assert res["child_pid"] == 99999
    assert res["status"] == "alive"
