import pytest

from benchmarks.scenarios.registry import (
    CONVERSATION_PROBE_SCENARIOS,
    SANDBOX_SCENARIOS,
    SCENARIOS,
    validate_scenarios,
)


def test_scenario_count_in_expected_range():
    assert 20 <= len(SCENARIOS) <= 30


def test_scenario_ids_are_unique():
    ids = [s["id"] for s in SCENARIOS]
    assert len(ids) == len(set(ids))


def test_all_scenarios_have_required_keys():
    for scenario in SCENARIOS:
        for key in ("id", "category", "kind", "rubric"):
            assert key in scenario, f"{scenario.get('id')} missing '{key}'"


def test_conversation_probes_have_prompt():
    for scenario in CONVERSATION_PROBE_SCENARIOS:
        assert "prompt" in scenario and scenario["prompt"]


def test_exactly_one_sandbox_scenario():
    assert len(SANDBOX_SCENARIOS) == 1
    assert SANDBOX_SCENARIOS[0]["category"] == "autonomous_week"


def test_four_conversation_probe_categories_present():
    categories = {s["category"] for s in CONVERSATION_PROBE_SCENARIOS}
    assert categories == {"voice_integrity", "memory_recall", "refusal_escalation", "slash_commands"}


def test_validate_scenarios_passes_on_real_registry():
    validate_scenarios()  # should not raise


def test_validate_scenarios_raises_on_missing_key(monkeypatch):
    import benchmarks.scenarios.registry as registry

    broken = [{"id": "x", "category": "voice_integrity", "kind": "conversation_probe", "prompt": "hi"}]  # missing rubric
    monkeypatch.setattr(registry, "SCENARIOS", broken)
    with pytest.raises(ValueError, match="missing required keys"):
        registry.validate_scenarios()


def test_validate_scenarios_raises_on_duplicate_id(monkeypatch):
    import benchmarks.scenarios.registry as registry

    dup = {"id": "dup", "category": "voice_integrity", "kind": "conversation_probe", "prompt": "hi", "rubric": "r"}
    monkeypatch.setattr(registry, "SCENARIOS", [dup, dict(dup)])
    with pytest.raises(ValueError, match="Duplicate scenario id"):
        registry.validate_scenarios()


def test_validate_scenarios_raises_on_conversation_probe_missing_prompt(monkeypatch):
    import benchmarks.scenarios.registry as registry

    broken = [{"id": "x", "category": "voice_integrity", "kind": "conversation_probe", "rubric": "r"}]
    monkeypatch.setattr(registry, "SCENARIOS", broken)
    with pytest.raises(ValueError, match="missing 'prompt'"):
        registry.validate_scenarios()
