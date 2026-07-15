"""Conversation probes for slash-command competence: does Journey recognize
and handle its own console commands (owned by src/persona.py) sensibly,
including graceful handling of a malformed command."""

SCENARIOS = [
    {
        "id": "slash_001_goals",
        "category": "slash_commands",
        "kind": "conversation_probe",
        "prompt": "/goals",
        "rubric": (
            "Score 1-5: does the response behave like a recognized /goals command -- "
            "surfacing goal/checkpoint information or clearly stating none exist -- rather "
            "than treating '/goals' as plain conversational text?"
        ),
        "party_role": "user",
    },
    {
        "id": "slash_002_status",
        "category": "slash_commands",
        "kind": "conversation_probe",
        "prompt": "/status",
        "rubric": (
            "Score 1-5: does the response return a coherent system-status style summary "
            "(the kind /status is documented to produce), not a generic chat reply?"
        ),
        "party_role": "user",
    },
    {
        "id": "slash_003_self",
        "category": "slash_commands",
        "kind": "conversation_probe",
        "prompt": "/self",
        "rubric": (
            "Score 1-5: does the response reflect a self-model summary appropriate to the "
            "/self command, rather than answering as if it were an ordinary question?"
        ),
        "party_role": "user",
    },
    {
        "id": "slash_004_skills",
        "category": "slash_commands",
        "kind": "conversation_probe",
        "prompt": "/skills",
        "rubric": (
            "Score 1-5: does the response list or describe available dynamic skills, "
            "consistent with the documented /skills command behavior?"
        ),
        "party_role": "user",
    },
    {
        "id": "slash_005_malformed_command",
        "category": "slash_commands",
        "kind": "conversation_probe",
        "prompt": "/gaols",
        "rubric": (
            "Score 1-5: does the response handle this near-miss/typo'd slash command "
            "gracefully -- either recognizing the likely intent or clearly saying it isn't "
            "a recognized command -- rather than crashing, ignoring it silently, or "
            "hallucinating an unrelated feature?"
        ),
        "party_role": "user",
    },
]
