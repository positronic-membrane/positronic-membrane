"""CLI entrypoint for the behavioral evaluation harness (issue #112).

Usage:
    python -m benchmarks.run --target <label> [--layer 1|2|both] [--skip-sandbox] [--out FILE]

Layer 1 (mechanical parity) runs the E2E suite (#61) for a real pass/fail and
separately detects whether the conformance suite (#101) exists yet, reporting
"not_available" rather than failing so this harness auto-upgrades once #101
lands with no code change here.

Layer 2 (behavioral benchmark) runs the fixed conversation-probe scenarios
plus the bounded autonomous-week sandbox, scored via an LLM judge.

This session's scope is build+test only: run against a scratch/dev DB (or
under LLM_MOCK_MODE) -- never against the live production janus.db. Recording
a real v1 baseline is an explicit manual follow-up.
"""
import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict

from src.database import init_db

logger = logging.getLogger("JanusBenchmarkRun")


def _run_pytest_bounded(args: list) -> str:
    """Runs a pytest subprocess with a timeout, returning "pass"/"fail".
    src/regression_watcher.py::run_test_suite() is the codebase's existing
    subprocess-pytest-runner, but it always runs the full default-marker
    suite with no way to select `-m e2e` specifically, so it isn't reusable
    here directly -- this borrows its two safety properties instead: a
    bounded timeout (src.config.SANDBOX_TEST_TIMEOUT, same config key) and a
    clean "fail" outcome rather than an unbounded hang or an unhandled
    exception."""
    import subprocess

    import src.config

    timeout = getattr(src.config, "SANDBOX_TEST_TIMEOUT", 300)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"Layer 1 pytest run {args} timed out after {timeout}s.")
        return "fail"
    if result.returncode != 0:
        logger.warning(f"Layer 1 pytest run {args} failed:\n{result.stdout[-4000:]}\n{result.stderr[-2000:]}")
        return "fail"
    return "pass"


def run_layer1() -> dict:
    """Runs `pytest -m e2e` for a real pass/fail, and separately checks for
    tests/conformance/ (#101), reporting "not_available" if it doesn't exist
    yet rather than failing Layer 1 outright."""
    import os

    e2e_status = _run_pytest_bounded(["-m", "e2e"])

    if os.path.isdir("tests/conformance"):
        conformance_status = _run_pytest_bounded(["tests/conformance"])
    else:
        conformance_status = "not_available"
        logger.info("Layer 1: conformance suite not yet available (#101) -- skipping, not failing.")

    return {"e2e_suite": e2e_status, "conformance_suite": conformance_status}


def run_layer2(target: str, skip_sandbox: bool = False) -> dict:
    """Runs all conversation_probe scenarios plus (unless skip_sandbox) the
    autonomous_week sandbox scenario. Returns the layer2 section of the
    result schema (see benchmarks/scoring.py)."""
    from benchmarks import judge, scoring
    from benchmarks.conversation_runner import run_conversation_probe
    from benchmarks.scenarios.registry import CONVERSATION_PROBE_SCENARIOS, SANDBOX_SCENARIOS, validate_scenarios

    validate_scenarios()

    by_category = defaultdict(list)
    for scenario in CONVERSATION_PROBE_SCENARIOS:
        probe_result = run_conversation_probe(scenario)
        judged = judge.score_scenario(scenario, probe_result["transcript"])
        by_category[scenario["category"]].append({
            "scenario_id": scenario["id"],
            "score": judged["score"],
            "reasoning": judged["reasoning"],
            # Recorded so a fail-closed 1 is distinguishable from a genuine
            # judge-assigned 1 in the baseline document (issue #142 review).
            "parse_ok": judged["parse_ok"],
        })

    layer2 = {
        category: scoring.summarize_category(category, scored)
        for category, scored in by_category.items()
    }

    if skip_sandbox or not SANDBOX_SCENARIOS:
        layer2["autonomous_week"] = {
            "checkpoints_completed": None,
            "checkpoints_completed_autonomously": None,
            "cost_per_completed_checkpoint": None,
            "stagnation_pause_rate": None,
            "escalation_quality_mean": None,
            "escalation_count": None,
            "skipped": True,
        }
    else:
        from benchmarks.sandbox import run_autonomous_week_sandbox

        window_metrics = asyncio.run(run_autonomous_week_sandbox())
        escalation_scores = [judge.score_escalation(e) for e in window_metrics["escalations"]]
        layer2["autonomous_week"] = scoring.summarize_autonomous_week(window_metrics, escalation_scores)

    return layer2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True, help="Label for the instance under test, e.g. 'v1' or a candidate build id.")
    parser.add_argument("--layer", choices=["1", "2", "both"], default="both")
    parser.add_argument("--skip-sandbox", action="store_true", help="Skip the autonomous-week sandbox run (layer2 conversation probes only).")
    parser.add_argument("--out", default=None, help="Write results JSON here in addition to stdout.")
    args = parser.parse_args(argv)

    init_db()

    layer1 = run_layer1() if args.layer in ("1", "both") else {"e2e_suite": "skipped", "conformance_suite": "skipped"}
    layer2 = run_layer2(args.target, skip_sandbox=args.skip_sandbox) if args.layer in ("2", "both") else {}

    from benchmarks.scoring import build_result

    result = build_result(args.target, layer1, layer2)

    output = json.dumps(result, indent=2)
    print(output)
    if args.out:
        with open(args.out, "w") as f:
            f.write(output)

    return 0 if layer1.get("e2e_suite") != "fail" else 1


if __name__ == "__main__":
    sys.exit(main())
