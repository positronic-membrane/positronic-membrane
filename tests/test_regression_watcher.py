import subprocess
from unittest.mock import patch

import pytest

import src.config
from src.database import get_connection
from src.regression_watcher import (
    detect_flaky_tests,
    get_test_run_history,
    parse_junit_xml,
    record_test_run,
    run_test_suite,
)
from src.sandbox_session import ship_sandbox_session

SAMPLE_JUNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
<testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4" time="1.234">
<testcase classname="tests.test_foo" name="test_passes" time="0.1"></testcase>
<testcase classname="tests.test_foo" name="test_fails" time="0.2">
<failure message="boom">AssertionError</failure>
</testcase>
<testcase classname="tests.test_foo" name="test_errors" time="0.3">
<error message="oops">RuntimeError</error>
</testcase>
<testcase classname="tests.test_foo" name="test_skipped" time="0.0">
<skipped message="skip"></skipped>
</testcase>
</testsuite>
</testsuites>
"""


def test_parse_junit_xml(tmp_path):
    xml_path = tmp_path / "junit.xml"
    xml_path.write_text(SAMPLE_JUNIT_XML)

    stats = parse_junit_xml(str(xml_path))

    assert stats["total"] == 4
    assert stats["passed"] == 1
    assert stats["failed"] == 1
    assert stats["errors"] == 1
    assert stats["skipped"] == 1
    assert stats["duration"] == pytest.approx(1.234)

    outcomes = {tc["name"]: tc["outcome"] for tc in stats["test_cases"]}
    assert outcomes["tests.test_foo::test_passes"] == "passed"
    assert outcomes["tests.test_foo::test_fails"] == "failed"
    assert outcomes["tests.test_foo::test_errors"] == "error"
    assert outcomes["tests.test_foo::test_skipped"] == "skipped"


@patch("src.regression_watcher.subprocess.run")
def test_run_test_suite_success(mock_run, tmp_path):
    junit_path = tmp_path / ".janus_test_results.xml"
    junit_path.write_text(SAMPLE_JUNIT_XML)
    mock_run.return_value = subprocess.CompletedProcess(args=["pytest"], returncode=1, stdout="", stderr="")

    stats = run_test_suite(cwd=str(tmp_path))

    assert stats["total"] == 4
    assert stats["failed"] == 1
    assert stats["passed_overall"] is False
    assert not junit_path.exists()  # cleaned up


@patch("src.regression_watcher.subprocess.run")
def test_run_test_suite_all_passed(mock_run, tmp_path):
    junit_path = tmp_path / ".janus_test_results.xml"
    junit_path.write_text(
        '<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="1" time="0.5">'
        '<testcase classname="tests.test_foo" name="test_ok" time="0.5"></testcase>'
        "</testsuite></testsuites>"
    )
    mock_run.return_value = subprocess.CompletedProcess(args=["pytest"], returncode=0, stdout="", stderr="")

    stats = run_test_suite(cwd=str(tmp_path))

    assert stats["passed_overall"] is True
    assert stats["failed"] == 0
    assert stats["errors"] == 0


@patch("src.regression_watcher.subprocess.run")
def test_run_test_suite_timeout(mock_run, tmp_path):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["pytest"], timeout=300)

    stats = run_test_suite(cwd=str(tmp_path), timeout=300)

    assert stats["passed_overall"] is False
    assert stats["errors"] == 1
    assert "timed out" in stats["logs"]


def test_record_test_run_inserts_rows():
    stats = {"total": 3, "passed": 2, "failed": 1, "errors": 0, "skipped": 0, "duration": 0.5}
    test_cases = [
        {"name": "tests.test_a::test_one", "outcome": "passed", "duration": 0.1},
        {"name": "tests.test_a::test_two", "outcome": "failed", "duration": 0.2},
    ]

    run_id = record_test_run(stats, commit_sha="abc123", triggered_by="manual", test_cases=test_cases)

    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT commit_sha, triggered_by, total, passed, failed, status FROM test_runs WHERE id = ?;",
            (run_id,),
        )
        row = cursor.fetchone()
        assert tuple(row) == ("abc123", "manual", 3, 2, 1, "failed")

        cursor.execute("SELECT test_name, outcome FROM test_case_results WHERE test_run_id = ? ORDER BY id;", (run_id,))
        rows = cursor.fetchall()
        assert [tuple(r) for r in rows] == [
            ("tests.test_a::test_one", "passed"),
            ("tests.test_a::test_two", "failed"),
        ]
    finally:
        conn.close()


def test_record_test_run_accepts_aggregate_only_stats():
    """ship_sandbox_session() passes parse_pytest_results()'s aggregate-only dict shape
    (no 'errors' key, uses 'total'/'passed'/'failed' only) — must not raise."""
    stats = {"passed": 5, "failed": 0, "total": 5, "coverage": 90.0}

    run_id = record_test_run(stats, commit_sha="deadbeef", triggered_by="sandbox_ship")

    history = get_test_run_history(limit=1)
    assert history[0]["id"] == run_id
    assert history[0]["status"] == "passed"
    assert history[0]["triggered_by"] == "sandbox_ship"


def test_get_test_run_history_ordering_and_limit():
    for i in range(5):
        record_test_run({"total": 1, "passed": 1, "failed": 0}, commit_sha=f"sha{i}", triggered_by="manual")

    history = get_test_run_history(limit=3)

    assert len(history) == 3
    assert [r["commit_sha"] for r in history] == ["sha4", "sha3", "sha2"]


def test_detect_flaky_tests():
    for outcome in ["passed", "failed", "passed"]:
        is_passed = outcome == "passed"
        run_id = record_test_run({"total": 1, "passed": int(is_passed), "failed": int(not is_passed)})
        conn = get_connection(read_only_constitution=True)
        try:
            conn.cursor().execute(
                "INSERT INTO test_case_results (test_run_id, test_name, outcome) VALUES (?, ?, ?);",
                (run_id, "tests.test_flaky::test_x", outcome),
            )
            conn.commit()
        finally:
            conn.close()

    for _ in range(3):
        run_id = record_test_run({"total": 1, "passed": 1, "failed": 0})
        conn = get_connection(read_only_constitution=True)
        try:
            conn.cursor().execute(
                "INSERT INTO test_case_results (test_run_id, test_name, outcome) VALUES (?, ?, ?);",
                (run_id, "tests.test_stable::test_y", "passed"),
            )
            conn.commit()
        finally:
            conn.close()

    flaky = detect_flaky_tests(lookback=5)

    assert "tests.test_flaky::test_x" in flaky
    assert "tests.test_stable::test_y" not in flaky


@patch("src.sandbox_session.get_active_sandbox")
@patch("src.sandbox_session.run_sandbox_tests")
@patch("src.sandbox_session.abort_sandbox_session")
def test_ship_sandbox_session_records_run_on_regression(mock_abort, mock_run_tests, mock_get_active_sb, tmp_path):
    orig_root = src.config.ROOT_DIR
    src.config.ROOT_DIR = tmp_path

    sandbox_path = tmp_path / "sandbox"
    (sandbox_path / "tests").mkdir(parents=True)

    mock_get_active_sb.return_value = {
        "active_sandbox_path": str(sandbox_path),
        "active_sandbox_branch": "janus-test-branch",
        "active_sandbox_status": "active",
    }
    mock_run_tests.return_value = (False, "=== 1 failed in 0.5s ===")

    try:
        with pytest.raises(RuntimeError, match="Regression detected"):
            ship_sandbox_session()
        mock_abort.assert_called_once()

        history = get_test_run_history(limit=1)
        assert history[0]["status"] == "failed"
        assert history[0]["triggered_by"] == "sandbox_ship"
    finally:
        src.config.ROOT_DIR = orig_root


@patch("src.sandbox_session.get_active_sandbox")
@patch("src.sandbox_session.run_sandbox_tests")
@patch("src.sandbox_session.abort_sandbox_session")
def test_ship_sandbox_session_records_failed_status_on_coverage_only_regression(
    mock_abort, mock_run_tests, mock_get_active_sb, tmp_path
):
    """A ship rejected purely for a coverage drop (no failing tests) must still
    be recorded with status='failed', not 'passed' derived from raw counts."""
    orig_root = src.config.ROOT_DIR
    src.config.ROOT_DIR = tmp_path

    sandbox_path = tmp_path / "sandbox"
    (sandbox_path / "tests").mkdir(parents=True)

    mock_get_active_sb.return_value = {
        "active_sandbox_path": str(sandbox_path),
        "active_sandbox_branch": "janus-test-branch",
        "active_sandbox_status": "active",
    }

    conn = get_connection(read_only_constitution=True)
    try:
        conn.cursor().execute(
            "INSERT INTO test_run_baselines (total_tests, passed_tests, failed_tests, coverage_percentage) "
            "VALUES (10, 10, 0, 90.0);"
        )
        conn.commit()
    finally:
        conn.close()

    mock_run_tests.return_value = (True, "=== 10 passed in 1.1s ===\nTOTAL          100     20    80%")

    try:
        with pytest.raises(RuntimeError, match="Coverage dropped"):
            ship_sandbox_session()
        mock_abort.assert_called_once()

        history = get_test_run_history(limit=1)
        assert history[0]["status"] == "failed"
        assert history[0]["failed"] == 0
    finally:
        src.config.ROOT_DIR = orig_root


@patch("src.sandbox_session.get_active_sandbox")
@patch("src.sandbox_session.run_sandbox_tests")
@patch("src.sandbox_session.get_sandbox_modified_files")
@patch("src.sandbox_session.cleanup_git_sandbox")
@patch("src.sandbox_session.clear_sandbox_session")
@patch("src.sandbox_session.shutil.copy2")
def test_ship_sandbox_session_records_run_on_success(
    mock_copy, mock_clear, mock_cleanup, mock_get_modified, mock_run_tests, mock_get_active_sb, tmp_path
):
    orig_root = src.config.ROOT_DIR
    src.config.ROOT_DIR = tmp_path

    sandbox_path = tmp_path / "sandbox"
    (sandbox_path / "tests").mkdir(parents=True)
    mock_get_modified.return_value = []

    mock_get_active_sb.return_value = {
        "active_sandbox_path": str(sandbox_path),
        "active_sandbox_branch": "janus-test-branch",
        "active_sandbox_status": "active",
    }
    mock_run_tests.return_value = (True, "=== 10 passed in 1.2s ===\nTOTAL          100     20    80%")

    try:
        ship_sandbox_session()

        history = get_test_run_history(limit=1)
        assert history[0]["status"] == "passed"
        assert history[0]["triggered_by"] == "sandbox_ship"

        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM test_run_baselines;")
            assert cursor.fetchone()[0] == 1
        finally:
            conn.close()
    finally:
        src.config.ROOT_DIR = orig_root
