from benchmarks.scoring import (
    _current_git_sha,
    compare_to_baseline,
    record_baseline_document,
    summarize_autonomous_week,
    summarize_category,
)


def test_summarize_category_computes_mean():
    scored = [
        {"scenario_id": "a", "score": 4, "reasoning": "x"},
        {"scenario_id": "b", "score": 2, "reasoning": "y"},
    ]
    result = summarize_category("voice_integrity", scored)
    assert result["mean_score"] == 3.0
    assert result["scenarios"] == scored


def test_summarize_category_empty_mean_is_none():
    result = summarize_category("voice_integrity", [])
    assert result["mean_score"] is None


def test_summarize_autonomous_week_no_escalations():
    window_metrics = {
        "checkpoints_completed": 3,
        "checkpoints_completed_autonomously": 2,
        "cost_per_completed_checkpoint": 0.01,
        "stagnation_pauses": 0,
        "hard_cap_pauses": 0,
        "escalations": [],
    }
    result = summarize_autonomous_week(window_metrics, [])
    assert result["escalation_count"] == 0
    assert result["escalation_quality_mean"] is None
    assert result["stagnation_pause_rate"] == 0


def test_summarize_autonomous_week_with_escalations():
    window_metrics = {
        "checkpoints_completed": 3,
        "checkpoints_completed_autonomously": 2,
        "cost_per_completed_checkpoint": 0.01,
        "stagnation_pauses": 1,
        "hard_cap_pauses": 0,
        "escalations": [{"kind": "pending_escalation"}],
    }
    escalation_scores = [{"score": 4, "reasoning": "substantive", "parse_ok": True}]
    result = summarize_autonomous_week(window_metrics, escalation_scores)
    assert result["escalation_count"] == 1
    assert result["escalation_quality_mean"] == 4.0
    assert result["stagnation_pause_rate"] == 1


def test_compare_to_baseline_pass_and_fail():
    baseline = {
        "layer2": {
            "voice_integrity": {"mean_score": 4.0},
            "memory_recall": {"mean_score": 4.0},
            "refusal_escalation": {},
            "slash_commands": {},
            "autonomous_week": {},
        }
    }
    current = {
        "layer2": {
            "voice_integrity": {"mean_score": 4.5},  # pass
            "memory_recall": {"mean_score": 3.0},  # fail
            "refusal_escalation": {},
            "slash_commands": {},
            "autonomous_week": {},
        }
    }
    result = compare_to_baseline(current, baseline)
    assert result["voice_integrity"] == "pass"
    assert result["memory_recall"] == "fail"


def test_compare_to_baseline_missing_category_is_na():
    baseline = {"layer2": {}}
    current = {"layer2": {}}
    result = compare_to_baseline(current, baseline)
    assert all(v == "N/A" for v in result.values())


def test_compare_to_baseline_empty_escalation_renders_na_not_raise():
    baseline = {"layer2": {"autonomous_week": {"checkpoints_completed": 2, "escalation_quality_mean": None}}}
    current = {"layer2": {"autonomous_week": {"checkpoints_completed": 3, "escalation_quality_mean": None}}}
    result = compare_to_baseline(current, baseline)
    assert result["autonomous_week"] in ("pass", "N/A")  # must not raise


def test_compare_to_baseline_categories_derived_from_registry():
    from benchmarks.scenarios.registry import CONVERSATION_PROBE_SCENARIOS

    result = compare_to_baseline({"layer2": {}}, {"layer2": {}})
    expected_categories = {s["category"] for s in CONVERSATION_PROBE_SCENARIOS}
    assert set(result.keys()) - {"autonomous_week"} == expected_categories


def test_compare_to_baseline_cost_regression_fails_even_with_same_checkpoints():
    baseline = {
        "layer2": {"autonomous_week": {"checkpoints_completed": 3, "cost_per_completed_checkpoint": 0.01}}
    }
    current = {
        "layer2": {"autonomous_week": {"checkpoints_completed": 3, "cost_per_completed_checkpoint": 0.10}}
    }
    result = compare_to_baseline(current, baseline)
    assert result["autonomous_week"] == "fail"


def test_compare_to_baseline_stagnation_regression_fails():
    baseline = {"layer2": {"autonomous_week": {"stagnation_pause_rate": 0}}}
    current = {"layer2": {"autonomous_week": {"stagnation_pause_rate": 5}}}
    result = compare_to_baseline(current, baseline)
    assert result["autonomous_week"] == "fail"


def test_compare_to_baseline_lower_cost_and_stagnation_pass():
    baseline = {
        "layer2": {"autonomous_week": {"cost_per_completed_checkpoint": 0.10, "stagnation_pause_rate": 5}}
    }
    current = {
        "layer2": {"autonomous_week": {"cost_per_completed_checkpoint": 0.05, "stagnation_pause_rate": 1}}
    }
    result = compare_to_baseline(current, baseline)
    assert result["autonomous_week"] == "pass"


def test_record_baseline_document_creates_then_updates_via_upsert():
    doc_id_1 = record_baseline_document("test_baseline_doc", {"target": "v1", "layer2": {}})
    assert doc_id_1

    # Re-running with the same title must update in place (upsert), not raise
    # ValueError like the old create_document()-first approach would on a
    # second call.
    doc_id_2 = record_baseline_document("test_baseline_doc", {"target": "v1", "layer2": {"changed": True}})
    assert doc_id_2 == doc_id_1

    from src.database import get_document
    doc = get_document("test_baseline_doc")
    assert '"changed": true' in doc["content"]
    assert doc["purpose"] == "knowledge"


def test_current_git_sha_delegates_to_regression_watcher(monkeypatch):
    monkeypatch.setattr("src.regression_watcher.get_current_commit_sha", lambda: "deadbeef")
    assert _current_git_sha() == "deadbeef"
