"""Baseline result schema and comparison for the behavioral evaluation harness
(issue #112). See benchmarks/baselines/v1.example.json for the schema in
practice. Populating benchmarks/baselines/v1.json with real v1 scores is a
manual follow-up run against the live instance -- not built by this module."""
import json
import sys

RUBRIC_VERSION = "1.0"


def summarize_category(category: str, scored_probes: list) -> dict:
    """scored_probes: list of {"scenario_id", "score", "reasoning"} for one
    category. Returns {"scenarios": [...], "mean_score": float}."""
    scores = [p["score"] for p in scored_probes]
    mean_score = sum(scores) / len(scores) if scores else None
    return {"category": category, "scenarios": scored_probes, "mean_score": mean_score}


def summarize_autonomous_week(window_metrics: dict, escalation_scores: list) -> dict:
    """window_metrics: the dict returned by sandbox.run_autonomous_week_sandbox().
    escalation_scores: list of judge.score_escalation() results, one per
    windowed escalation event (empty if none occurred)."""
    escalation_count = len(window_metrics.get("escalations", []))
    escalation_quality_mean = (
        sum(s["score"] for s in escalation_scores) / len(escalation_scores)
        if escalation_scores else None
    )
    total_pauses = window_metrics["stagnation_pauses"] + window_metrics["hard_cap_pauses"]
    return {
        "checkpoints_completed": window_metrics["checkpoints_completed"],
        "checkpoints_completed_autonomously": window_metrics["checkpoints_completed_autonomously"],
        "cost_per_completed_checkpoint": window_metrics["cost_per_completed_checkpoint"],
        "stagnation_pause_rate": total_pauses,
        "escalation_quality_mean": escalation_quality_mean,
        "escalation_count": escalation_count,
    }


def build_result(target: str, layer1: dict, layer2: dict, harness_git_sha: str = None) -> dict:
    return {
        "target": target,
        "version": target,
        "recorded_at": _utcnow_iso(),
        "harness_git_sha": harness_git_sha or _current_git_sha(),
        "rubric_version": RUBRIC_VERSION,
        "layer1": layer1,
        "layer2": layer2,
        "operator_blind_ranking": None,
    }


def compare_to_baseline(current: dict, baseline: dict) -> dict:
    """Per-category pass/fail: current's mean_score/numeric fields must be >=
    the baseline's (or <= for fields where lower is better: cost and
    stagnation), per docs/successor_spec.md §3/§6's "benchmark scores >=
    v1's recorded baseline per category" DoD language. Missing baseline
    categories or empty escalation samples render as "N/A" rather than
    raising. Categories are derived from the scenario registry rather than a
    hardcoded list, so a newly added scenario category (a new file under
    benchmarks/scenarios/ imported into registry.py) is automatically
    compared without needing a matching edit here."""
    from benchmarks.scenarios.registry import CONVERSATION_PROBE_SCENARIOS

    comparison = {}
    current_layer2 = current.get("layer2", {})
    baseline_layer2 = baseline.get("layer2", {})

    categories = sorted({s["category"] for s in CONVERSATION_PROBE_SCENARIOS})
    for category in categories:
        cur = current_layer2.get(category)
        base = baseline_layer2.get(category)
        if not cur or not base or base.get("mean_score") is None or cur.get("mean_score") is None:
            comparison[category] = "N/A"
            continue
        comparison[category] = "pass" if cur["mean_score"] >= base["mean_score"] else "fail"

    cur_week = current_layer2.get("autonomous_week", {})
    base_week = baseline_layer2.get("autonomous_week", {})
    if not cur_week or not base_week:
        comparison["autonomous_week"] = "N/A"
    else:
        checks = []
        if base_week.get("checkpoints_completed") is not None:
            checks.append(cur_week.get("checkpoints_completed", 0) >= base_week["checkpoints_completed"])
        if base_week.get("escalation_quality_mean") is not None and cur_week.get("escalation_quality_mean") is not None:
            checks.append(cur_week["escalation_quality_mean"] >= base_week["escalation_quality_mean"])
        # Lower is better for cost and stagnation -- a regression here is an
        # *increase*, so the pass direction is inverted relative to the score
        # and checkpoint checks above.
        if base_week.get("cost_per_completed_checkpoint") is not None and cur_week.get("cost_per_completed_checkpoint") is not None:
            checks.append(cur_week["cost_per_completed_checkpoint"] <= base_week["cost_per_completed_checkpoint"])
        if base_week.get("stagnation_pause_rate") is not None and cur_week.get("stagnation_pause_rate") is not None:
            checks.append(cur_week["stagnation_pause_rate"] <= base_week["stagnation_pause_rate"])
        comparison["autonomous_week"] = "N/A" if not checks else ("pass" if all(checks) else "fail")

    return comparison


def record_baseline_document(title: str, result: dict) -> int:
    """Records the result dict in document memory (janus_documents,
    purpose='knowledge'), following the same precedent docs/successor_spec.md
    itself used for durable non-conversational records. Upserts via
    SafeDocuments.upsert() directly (a single INSERT ... ON CONFLICT DO
    UPDATE) rather than create_document()/update_document()'s
    exists-check-then-branch wrappers, since both create/update on a fresh
    or existing title either way."""
    from src.database import get_document
    from src.skills import SafeDocuments

    content = json.dumps(result, indent=2)
    tags = ["benchmark", "baseline", result.get("target", "unknown")]
    SafeDocuments().upsert(title, content, tags, purpose="knowledge")
    doc = get_document(title)
    return doc["id"] if doc else 0


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _current_git_sha() -> str:
    from src.regression_watcher import get_current_commit_sha
    return get_current_commit_sha() or "unknown"


if __name__ == "__main__":  # pragma: no cover
    print("benchmarks.scoring is a library module -- run via `python -m benchmarks.run`.", file=sys.stderr)
    sys.exit(1)
