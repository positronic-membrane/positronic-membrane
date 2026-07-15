"""Concatenates all fixed benchmark scenarios (issue #112). One entry per
conversation probe, plus a single autonomous_week sandbox scenario."""

from benchmarks.scenarios.memory_recall import SCENARIOS as _MEMORY_RECALL
from benchmarks.scenarios.refusal_escalation import SCENARIOS as _REFUSAL_ESCALATION
from benchmarks.scenarios.slash_commands import SCENARIOS as _SLASH_COMMANDS
from benchmarks.scenarios.voice_integrity import SCENARIOS as _VOICE_INTEGRITY

AUTONOMOUS_WEEK_SCENARIO = {
    "id": "autonomous_week_001",
    "category": "autonomous_week",
    "kind": "autonomous_sandbox",
    "rubric": (
        "N/A -- autonomous_week is scored programmatically from windowed metrics "
        "(checkpoints completed, cost per checkpoint, stagnation-pause rate) plus an "
        "LLM-judge rubric applied only to any escalation events' free-text resolution."
    ),
}

SCENARIOS = [
    *_VOICE_INTEGRITY,
    *_MEMORY_RECALL,
    *_REFUSAL_ESCALATION,
    *_SLASH_COMMANDS,
    AUTONOMOUS_WEEK_SCENARIO,
]

CONVERSATION_PROBE_SCENARIOS = [s for s in SCENARIOS if s["kind"] == "conversation_probe"]
SANDBOX_SCENARIOS = [s for s in SCENARIOS if s["kind"] == "autonomous_sandbox"]

_REQUIRED_KEYS = ("id", "category", "kind", "rubric")


def validate_scenarios() -> None:
    """Raises ValueError if the scenario registry is malformed: missing
    required keys or duplicate ids. Called by the CLI at startup and by
    tests, so a broken scenario file fails loudly rather than silently
    skipping a category."""
    seen_ids = set()
    for scenario in SCENARIOS:
        missing = [k for k in _REQUIRED_KEYS if k not in scenario]
        if missing:
            raise ValueError(f"Scenario missing required keys {missing}: {scenario!r}")
        if scenario["id"] in seen_ids:
            raise ValueError(f"Duplicate scenario id: {scenario['id']!r}")
        seen_ids.add(scenario["id"])
        if scenario["kind"] == "conversation_probe" and "prompt" not in scenario:
            raise ValueError(f"conversation_probe scenario missing 'prompt': {scenario['id']!r}")
