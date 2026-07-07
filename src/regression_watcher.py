"""Regression Watcher (issue #66): runs the pytest suite, records results into
test_runs/test_case_results, and detects flaky tests.

Not used by ship_sandbox_session() to re-run tests — that flow already runs
pytest via its own SandboxExecutor and calls record_test_run() with the
resulting aggregate stats. run_test_suite() is for manual (/test run) and CI use.
"""
import logging
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import src.config
from src.database import get_connection

logger = logging.getLogger("JanusRegressionWatcher")


def _resolve_pytest() -> str:
    candidate = Path(sys.executable).parent / "pytest"
    if candidate.exists():
        return str(candidate)
    venv_candidate = src.config.ROOT_DIR / ".venv" / "bin" / "pytest"
    if venv_candidate.exists():
        return str(venv_candidate)
    return "pytest"


def get_current_commit_sha(cwd: Optional[str] = None) -> str:
    """Resolves the current commit sha via `git rev-parse HEAD`, defaulting
    to src.config.ROOT_DIR so callers don't silently depend on process cwd."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd or str(src.config.ROOT_DIR),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def parse_junit_xml(path: str) -> dict:
    """Parses a JUnit XML report into aggregate counts + per-test outcomes."""
    tree = ET.parse(path)
    root = tree.getroot()
    suite = root.find("testsuite") if root.tag == "testsuites" else root
    if suite is None:
        return {
            "total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
            "duration": 0.0, "test_cases": [],
        }

    test_cases = []
    for tc in suite.findall("testcase"):
        classname = tc.get("classname", "")
        name = tc.get("name", "")
        full_name = f"{classname}::{name}" if classname else name
        duration = float(tc.get("time", 0.0) or 0.0)
        if tc.find("failure") is not None:
            outcome = "failed"
        elif tc.find("error") is not None:
            outcome = "error"
        elif tc.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        test_cases.append({"name": full_name, "outcome": outcome, "duration": duration})

    passed = sum(1 for t in test_cases if t["outcome"] == "passed")
    failed = sum(1 for t in test_cases if t["outcome"] == "failed")
    errors = sum(1 for t in test_cases if t["outcome"] == "error")
    skipped = sum(1 for t in test_cases if t["outcome"] == "skipped")

    return {
        "total": len(test_cases),
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "duration": float(suite.get("time", 0.0) or 0.0),
        "test_cases": test_cases,
    }


def run_test_suite(cwd: Optional[str] = None, timeout: Optional[int] = None) -> dict:
    """Runs the full pytest suite via subprocess with a junitxml report, for
    manual (/test run) and CI use.

    Returns a dict with the same shape as parse_junit_xml(), plus
    "passed_overall": bool and "logs": str.
    """
    cwd = cwd or str(src.config.ROOT_DIR)
    timeout = timeout if timeout is not None else src.config.SANDBOX_TEST_TIMEOUT
    junit_path = Path(cwd) / ".janus_test_results.xml"

    env = {**os.environ, "JANUS_TEST_MODE": "1"}
    start = time.monotonic()
    try:
        result = subprocess.run(
            [_resolve_pytest(), "-v", "--tb=short", f"--junitxml={junit_path}"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        logs = result.stdout + "\n" + result.stderr
        returncode_ok = result.returncode == 0
    except subprocess.TimeoutExpired:
        return {
            "total": 0, "passed": 0, "failed": 0, "errors": 1, "skipped": 0,
            "duration": time.monotonic() - start, "test_cases": [],
            "passed_overall": False, "logs": f"Test run timed out after {timeout}s.",
        }
    except Exception as e:
        return {
            "total": 0, "passed": 0, "failed": 0, "errors": 1, "skipped": 0,
            "duration": time.monotonic() - start, "test_cases": [],
            "passed_overall": False, "logs": f"Error executing tests: {e}",
        }

    if not junit_path.exists():
        return {
            "total": 0, "passed": 0, "failed": 0, "errors": 1, "skipped": 0,
            "duration": time.monotonic() - start, "test_cases": [],
            "passed_overall": False, "logs": logs + "\n(No junitxml report produced.)",
        }

    stats = parse_junit_xml(str(junit_path))
    stats["passed_overall"] = returncode_ok and stats["failed"] == 0 and stats["errors"] == 0
    stats["logs"] = logs
    try:
        junit_path.unlink()
    except OSError:
        pass
    return stats


def record_test_run(
    stats: dict,
    commit_sha: Optional[str] = None,
    triggered_by: str = "manual",
    test_cases: Optional[list] = None,
    status: Optional[str] = None,
) -> int:
    """Inserts a test_runs row (+ optional test_case_results rows). Returns the new run id.

    Accepts either the full run_test_suite() dict shape or the aggregate-only
    shape produced by sandbox_session.py's parse_pytest_results() (no "errors"
    key, "duration" absent) — ship_sandbox_session() passes its stats dict
    unmodified.

    `status` defaults to a derivation from failed/errors counts, but callers
    that know a run was rejected for a reason not visible in those counts
    (e.g. a coverage-regression check) should pass it explicitly so the
    recorded status matches what actually happened.
    """
    duration = stats.get("duration_seconds", stats.get("duration", 0.0))
    errors = stats.get("errors", 0) or 0
    failed = stats.get("failed", 0) or 0
    if status is None:
        status = "passed" if (failed == 0 and errors == 0) else "failed"

    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO test_runs
                (commit_sha, triggered_by, total, passed, failed, errors, skipped, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                commit_sha, triggered_by, stats.get("total", 0), stats.get("passed", 0),
                failed, errors, stats.get("skipped", 0), duration, status,
            ),
        )
        run_id = cursor.lastrowid
        if test_cases:
            cursor.executemany(
                "INSERT INTO test_case_results (test_run_id, test_name, outcome, duration_seconds) "
                "VALUES (?, ?, ?, ?);",
                [(run_id, tc["name"], tc["outcome"], tc.get("duration")) for tc in test_cases],
            )
        conn.commit()
        return run_id
    finally:
        conn.close()


def get_test_run_history(limit: int = 10) -> list:
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, timestamp, commit_sha, triggered_by, total, passed, failed,
                   errors, skipped, duration_seconds, status
            FROM test_runs ORDER BY id DESC LIMIT ?;
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        cols = ["id", "timestamp", "commit_sha", "triggered_by", "total", "passed",
                "failed", "errors", "skipped", "duration_seconds", "status"]
        return [dict(zip(cols, row, strict=True)) for row in rows]
    finally:
        conn.close()


def detect_flaky_tests(lookback: int = 5) -> list:
    """Returns test_names whose outcome was non-uniform (both passed and
    failed/error) across their last `lookback` occurrences in
    test_case_results, most-recent-run-first."""
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT test_name FROM test_case_results;")
        names = [row[0] for row in cursor.fetchall()]

        flaky = []
        for name in names:
            cursor.execute(
                """
                SELECT outcome FROM test_case_results
                WHERE test_name = ?
                ORDER BY test_run_id DESC LIMIT ?;
                """,
                (name, lookback),
            )
            outcomes = {row[0] for row in cursor.fetchall()}
            if "passed" in outcomes and ("failed" in outcomes or "error" in outcomes):
                flaky.append(name)
        return flaky
    finally:
        conn.close()
