from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import src.config
import src.database
import src.memory
from src.database import get_connection, init_db
from src.web_server import app


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for health-check tests."""
    temp_db = tmp_path / "test_janus_health.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path


@pytest.fixture(autouse=True)
def setup_test_vector_db(tmp_path):
    """Isolate the ChromaDB persistent directory for health-check tests."""
    orig_path = src.config.VECTOR_DB_PATH
    src.config.VECTOR_DB_PATH = str(tmp_path / "test_chromadb")
    src.memory._chroma_client = None
    src.memory._collections = {}
    yield
    src.config.VECTOR_DB_PATH = orig_path


def _mark_daemon_fresh():
    conn = get_connection()
    conn.execute("UPDATE cognitive_layers SET last_run_at = CURRENT_TIMESTAMP WHERE layer_name = 'mid';")
    conn.commit()
    conn.close()


def _mark_daemon_stale():
    conn = get_connection()
    stale = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE cognitive_layers SET last_run_at = ? WHERE layer_name = 'mid';", (stale,))
    conn.commit()
    conn.close()


def test_healthz_always_returns_200(monkeypatch):
    monkeypatch.setattr("src.routers.health.check_connection", lambda: False)
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_returns_200_when_all_healthy():
    _mark_daemon_fresh()
    client = TestClient(app)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readyz_returns_503_when_db_down(monkeypatch):
    _mark_daemon_fresh()
    monkeypatch.setattr("src.routers.health.check_connection", lambda: False)
    client = TestClient(app)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"


def test_readyz_returns_503_when_vector_db_down(monkeypatch):
    _mark_daemon_fresh()
    monkeypatch.setattr("src.routers.health.check_vector_connection", lambda: False)
    client = TestClient(app)
    resp = client.get("/readyz")
    assert resp.status_code == 503


def test_readyz_returns_503_when_daemon_stale():
    _mark_daemon_stale()
    client = TestClient(app)
    resp = client.get("/readyz")
    assert resp.status_code == 503


def test_readyz_returns_503_when_daemon_never_ran():
    # Fresh init_db() leaves cognitive_layers.last_run_at NULL until the daemon ticks.
    client = TestClient(app)
    resp = client.get("/readyz")
    assert resp.status_code == 503


def test_health_returns_200_ok_when_all_healthy():
    _mark_daemon_fresh()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["vector_db"] == "ok"
    assert body["daemon"]["running"] is True
    assert body["daemon"]["last_heartbeat"] is not None
    assert body["llm_api"]["configured"] is True
    assert isinstance(body["uptime_seconds"], float)
    assert body["uptime_seconds"] >= 0


def test_health_returns_503_degraded_when_daemon_stale_but_db_ok():
    _mark_daemon_stale()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["database"] == "ok"
    assert body["daemon"]["running"] is False


def test_health_returns_503_down_when_db_unreachable(monkeypatch):
    _mark_daemon_fresh()
    monkeypatch.setattr("src.routers.health.check_connection", lambda: False)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "down"
    assert body["database"] == "down"


def test_health_llm_api_configured_false_when_key_explicitly_empty(monkeypatch):
    _mark_daemon_fresh()
    monkeypatch.setattr(src.config, "LLM_API_KEY", "")
    client = TestClient(app)
    resp = client.get("/health")
    body = resp.json()
    assert body["llm_api"]["configured"] is False
    assert "secret-llm-key-value" not in resp.text


def test_health_never_exposes_llm_api_key_value(monkeypatch):
    _mark_daemon_fresh()
    monkeypatch.setattr(src.config, "LLM_API_KEY", "secret-llm-key-value")
    client = TestClient(app)
    resp = client.get("/health")
    assert "secret-llm-key-value" not in resp.text


def test_health_vector_db_down_reports_degraded(monkeypatch):
    _mark_daemon_fresh()
    monkeypatch.setattr("src.routers.health.check_vector_connection", lambda: False)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["vector_db"] == "down"
    assert body["status"] == "degraded"


def test_readyz_ok_when_daemon_fresh_relative_to_slower_configured_cadence():
    """A daemon reconfigured to a slower mid cadence shouldn't be reported stale
    just because its heartbeat age exceeds the old fixed 60s default."""
    conn = get_connection()
    conn.execute("UPDATE cognitive_layers SET cadence_ms = 120000 WHERE layer_name = 'mid';")
    ninety_seconds_ago = (datetime.now(UTC) - timedelta(seconds=90)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE cognitive_layers SET last_run_at = ? WHERE layer_name = 'mid';", (ninety_seconds_ago,))
    conn.commit()
    conn.close()

    client = TestClient(app)
    resp = client.get("/readyz")
    assert resp.status_code == 200


def test_check_vector_connection_postgres_mode_ok_when_table_queryable(monkeypatch):
    monkeypatch.setattr(src.config, "DB_TYPE", "postgres")
    mock_conn = MagicMock()
    monkeypatch.setattr(src.database, "get_connection", lambda **kwargs: mock_conn)
    assert src.memory.check_vector_connection() is True
    mock_conn.execute.assert_called_once_with("SELECT 1 FROM janus_embeddings LIMIT 1")
    mock_conn.close.assert_called_once()


def test_check_vector_connection_postgres_mode_false_when_table_missing(monkeypatch):
    monkeypatch.setattr(src.config, "DB_TYPE", "postgres")
    mock_conn = MagicMock()
    mock_conn.execute.side_effect = Exception("relation \"janus_embeddings\" does not exist")
    monkeypatch.setattr(src.database, "get_connection", lambda **kwargs: mock_conn)
    assert src.memory.check_vector_connection() is False
    mock_conn.close.assert_called_once()
