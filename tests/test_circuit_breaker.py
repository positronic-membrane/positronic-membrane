from datetime import datetime, timedelta, timezone

import pytest

import src.config
from src.database import get_connection, init_db
from src.persona import handle_web_slash_command
from src.skill_harness import check_circuit, record_skill_failure, record_skill_success
from src.skills import DynamicSkillExecutor


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB for testing."""
    temp_db = tmp_path / "test_janus_circuit_breaker.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()

    conn = get_connection(read_only_constitution=False)
    conn.execute("INSERT INTO parties (id, name, role, public_key) VALUES ('admin1', 'Charlie', 'admin', 'key3');")
    conn.execute("UPDATE system_config SET config_value = '2' WHERE config_key = 'circuit_breaker.max_failures';")
    conn.execute("UPDATE system_config SET config_value = '15' WHERE config_key = 'circuit_breaker.cooldown_minutes';")
    conn.commit()
    conn.close()

    yield
    src.config.DB_PATH = orig_db_path


def _insert_failing_skill(skill_id="flaky_skill"):
    conn = get_connection(read_only_constitution=False)
    conn.execute(
        """
        INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
        VALUES (?, 'Flaky', 'Always raises', '{}', 'def run(): raise ValueError("boom")', 'run', 'user');
        """,
        (skill_id,),
    )
    conn.commit()
    conn.close()


def test_breaker_trips_after_threshold_and_skips_execution():
    _insert_failing_skill("flaky_skill")

    res1 = DynamicSkillExecutor.execute("flaky_skill", {})
    assert res1["success"] is False
    assert "Dynamic Execution Error" in res1["error"]

    res2 = DynamicSkillExecutor.execute("flaky_skill", {})
    assert res2["success"] is False
    assert "Dynamic Execution Error" in res2["error"]

    # Threshold (2) now exceeded; breaker should be tripped and skip execution
    res3 = DynamicSkillExecutor.execute("flaky_skill", {})
    assert res3["success"] is False
    assert "Circuit breaker tripped" in res3["error"]
    assert check_circuit("flaky_skill") is False


@pytest.mark.asyncio
async def test_circuit_status_lists_tripped_skill():
    _insert_failing_skill("flaky_skill")
    for _ in range(2):
        DynamicSkillExecutor.execute("flaky_skill", {})

    res = await handle_web_slash_command("/circuit status")
    assert "flaky_skill" in res
    assert "Circuit Breaker Status" in res


@pytest.mark.asyncio
async def test_circuit_reset_clears_breaker_and_allows_execution():
    _insert_failing_skill("flaky_skill")
    for _ in range(2):
        DynamicSkillExecutor.execute("flaky_skill", {})
    assert check_circuit("flaky_skill") is False

    res = await handle_web_slash_command("/circuit reset flaky_skill")
    assert "[✔]" in res
    assert check_circuit("flaky_skill") is True

    # No longer skipped by the breaker - execution is attempted again (and fails on its own merits)
    res_after = DynamicSkillExecutor.execute("flaky_skill", {})
    assert "Dynamic Execution Error" in res_after["error"]


@pytest.mark.asyncio
async def test_circuit_reset_missing_skill_reports_error():
    res = await handle_web_slash_command("/circuit reset nonexistent_skill")
    assert "[Error]" in res


def test_circuit_cooldown_auto_reset():
    _insert_failing_skill("flaky_skill")
    for _ in range(2):
        DynamicSkillExecutor.execute("flaky_skill", {})
    assert check_circuit("flaky_skill") is False

    stale_tripped_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection(read_only_constitution=False)
    conn.execute(
        "UPDATE circuit_breaker_state SET tripped_at = ? WHERE skill_id = ?;",
        (stale_tripped_at, "flaky_skill"),
    )
    conn.commit()
    conn.close()

    assert check_circuit("flaky_skill") is True

    conn = get_connection(read_only_constitution=True)
    row = conn.execute(
        "SELECT consecutive_failures, tripped_at FROM circuit_breaker_state WHERE skill_id = ?;",
        ("flaky_skill",),
    ).fetchone()
    conn.close()
    assert row[0] == 0
    assert row[1] is None


def test_record_skill_success_resets_failure_count():
    record_skill_failure("some_skill")
    conn = get_connection(read_only_constitution=True)
    row = conn.execute(
        "SELECT consecutive_failures FROM circuit_breaker_state WHERE skill_id = ?;",
        ("some_skill",),
    ).fetchone()
    conn.close()
    assert row[0] == 1

    record_skill_success("some_skill")
    conn = get_connection(read_only_constitution=True)
    row = conn.execute(
        "SELECT consecutive_failures FROM circuit_breaker_state WHERE skill_id = ?;",
        ("some_skill",),
    ).fetchone()
    conn.close()
    assert row[0] == 0


def test_record_skill_success_does_not_clear_an_active_trip():
    record_skill_failure("some_skill")
    record_skill_failure("some_skill")  # max_failures is 2 in this test's config
    assert check_circuit("some_skill") is False

    # A success landing after the trip (e.g. a stale in-flight execution) must
    # not mask the trip by resetting consecutive_failures.
    record_skill_success("some_skill")

    conn = get_connection(read_only_constitution=True)
    row = conn.execute(
        "SELECT consecutive_failures, tripped_at FROM circuit_breaker_state WHERE skill_id = ?;",
        ("some_skill",),
    ).fetchone()
    conn.close()
    assert row[1] is not None
    assert check_circuit("some_skill") is False


def test_check_presence_exempt_from_breaker():
    for _ in range(5):
        record_skill_failure("check_presence")

    conn = get_connection(read_only_constitution=True)
    row = conn.execute(
        "SELECT consecutive_failures, tripped_at FROM circuit_breaker_state WHERE skill_id = ?;",
        ("check_presence",),
    ).fetchone()
    conn.close()
    assert row[0] >= 2
    assert row[1] is not None

    # Even though the row shows a trip, check_presence is exempt from enforcement.
    assert check_circuit("check_presence") is True


@pytest.mark.asyncio
async def test_tripped_breaker_does_not_mask_permission_veto():
    conn = get_connection(read_only_constitution=False)
    conn.execute(
        """
        INSERT INTO agent_skills (skill_id, name, description, parameters_schema, code_blob, entry_point_function, required_role)
        VALUES ('admin_only_flaky', 'Admin Flaky', 'Admin restricted, always raises', '{}', 'def run(): raise ValueError("boom")', 'run', 'admin');
        """
    )
    conn.execute("INSERT INTO parties (id, name, role, public_key) VALUES ('user1', 'Alice', 'user', 'key1');")
    conn.commit()
    conn.close()

    # Trip the breaker as admin1 (session party resolves to the first admin found).
    for _ in range(2):
        DynamicSkillExecutor.execute("admin_only_flaky", {}, party_id="admin1")
    assert check_circuit("admin_only_flaky") is False

    # An unauthorized party must still see the Security Veto, not the breaker message.
    res = DynamicSkillExecutor.execute("admin_only_flaky", {}, party_id="user1")
    assert res["success"] is False
    assert "Security Veto" in res["error"]


def test_circuit_breaker_config_locked_from_agent_modification():
    conn = get_connection(read_only_constitution=True)
    rows = conn.execute(
        "SELECT config_key, is_agent_modifiable FROM system_config WHERE config_key IN "
        "('circuit_breaker.max_failures', 'circuit_breaker.cooldown_minutes');"
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    assert all(is_modifiable == 0 for _, is_modifiable in rows)
