import pytest
from fastapi.testclient import TestClient

import src.config
from src.database import get_connection, init_db
from src.persona import handle_governor_command, handle_web_slash_command
from src.web_server import app


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Redirects config.DB_PATH to a temp file and seeds it."""
    import src.daemon
    src.daemon._consecutive_stagnant_cycles = 0

    temp_db = tmp_path / "test_janus_governor.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)

    init_db()

    yield

    src.config.DB_PATH = orig_db_path


def _set_paused():
    conn = get_connection()
    conn.execute(
        "UPDATE system_config SET config_value = 'paused' WHERE config_key = 'governor.state';"
    )
    conn.execute(
        "UPDATE system_config SET config_value = '2026-07-06T00:00:00+00:00' WHERE config_key = 'governor.paused_at';"
    )
    conn.commit()
    conn.close()


def test_api_get_governor_status_running():
    client = TestClient(app)
    orig_require = src.config.REQUIRE_AUTH
    try:
        src.config.REQUIRE_AUTH = False
        resp = client.get("/api/governor/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "running"
        assert body["paused_at"] is None
        assert body["stagnant_threshold"] == 3
        assert body["cooldown_minutes"] == 30
        assert "consecutive_stagnant_cycles" in body
        assert "background_loop_count" in body
        assert "loop_hard_cap" in body
    finally:
        src.config.REQUIRE_AUTH = orig_require


def test_api_get_governor_status_paused():
    _set_paused()

    client = TestClient(app)
    orig_require = src.config.REQUIRE_AUTH
    try:
        src.config.REQUIRE_AUTH = False
        resp = client.get("/api/governor/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "paused"
        assert body["paused_at"] == "2026-07-06T00:00:00+00:00"
    finally:
        src.config.REQUIRE_AUTH = orig_require


def test_governor_status_cli_command():
    _set_paused()
    res = handle_governor_command("/governor status")
    assert "Smart Loop Governor Status" in res
    assert "paused" in res
    assert "2026-07-06T00:00:00+00:00" in res


@pytest.mark.asyncio
async def test_governor_status_web_command():
    res = await handle_web_slash_command("/governor status")
    assert "Smart Loop Governor Status" in res
    assert "running" in res


def test_governor_command_unknown_subcommand_reports_error():
    res = handle_governor_command("/governor bogus")
    assert "[Error]" in res


def test_stagnant_threshold_hardening_applies_to_pre_existing_row():
    """governor.stagnant_threshold predates the is_agent_modifiable=0 hardening, so
    on a database initialized before that change (still is_agent_modifiable=1),
    re-running init_db() (e.g. on daemon restart after upgrading) must flip it to 0
    rather than silently no-op via INSERT OR IGNORE against the existing row."""
    conn = get_connection()
    conn.execute(
        "UPDATE system_config SET is_agent_modifiable = 1 WHERE config_key = 'governor.stagnant_threshold';"
    )
    conn.commit()
    conn.close()

    init_db()

    conn = get_connection(read_only_constitution=True)
    row = conn.execute(
        "SELECT is_agent_modifiable FROM system_config WHERE config_key = 'governor.stagnant_threshold';"
    ).fetchone()
    conn.close()
    assert row[0] == 0


def test_governor_status_reads_db_backed_counter_not_process_global():
    """The status dict must reflect governor.consecutive_stagnant_cycles in
    system_config, not the in-process daemon._consecutive_stagnant_cycles global —
    the latter is meaningless when the reader (web_server) runs in a different OS
    process than the daemon loop, as in the project's Docker deployment mode."""
    import src.daemon

    conn = get_connection()
    conn.execute(
        "UPDATE system_config SET config_value = '2' WHERE config_key = 'governor.consecutive_stagnant_cycles';"
    )
    conn.commit()
    conn.close()

    # Simulate the cross-process case: the local global stays at its default (0)
    # even though the DB-backed mirror (written by a *different* process's daemon
    # loop) says 2.
    assert src.daemon._consecutive_stagnant_cycles == 0

    status = src.daemon.get_governor_status_dict()
    assert status["consecutive_stagnant_cycles"] == 2

    res = handle_governor_command("/governor status")
    assert "2/3" in res


def test_governor_config_locked_from_agent_modification():
    conn = get_connection(read_only_constitution=True)
    try:
        rows = conn.execute(
            "SELECT config_key, is_agent_modifiable FROM system_config WHERE config_key IN "
            "('governor.stagnant_threshold', 'governor.cooldown_minutes', 'governor.state', "
            "'governor.paused_at', 'governor.consecutive_stagnant_cycles');"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 5
    for _key, modifiable in rows:
        assert modifiable == 0
