"""Conversation probes for memory quality: plant a fact via episodic memory,
force consolidation (compress_episodic_memory), then probe recall. Each
scenario's "setup" holds the fact to plant before the recall prompt is sent.
Exercises the same context-assembly path (_build_persona_prompt) a real user
turn would, per issue #54's memory-streams work."""

SCENARIOS = [
    {
        "id": "memory_001_stated_preference",
        "category": "memory_recall",
        "kind": "conversation_probe",
        "setup": {"fact": "My favorite programming language is Rust, and I always want code examples in Rust when I ask for them."},
        "prompt": "What's my favorite programming language?",
        "rubric": (
            "Score 1-5: does the response correctly recall the planted preference (Rust) "
            "after consolidation, without fabricating a different answer?"
        ),
        "party_role": "user",
    },
    {
        "id": "memory_002_project_deadline",
        "category": "memory_recall",
        "kind": "conversation_probe",
        "setup": {"fact": "The V1 sign-off deadline for this project is July 20th, 2026."},
        "prompt": "When is the V1 sign-off deadline again?",
        "rubric": (
            "Score 1-5: does the response correctly recall the planted deadline (July 20th, "
            "2026) after consolidation, without fabricating a different date?"
        ),
        "party_role": "user",
    },
    {
        "id": "memory_003_named_entity",
        "category": "memory_recall",
        "kind": "conversation_probe",
        "setup": {"fact": "My colleague who handles all the infrastructure and deploys is named Priya."},
        "prompt": "Who's the person on my team that handles infrastructure and deploys?",
        "rubric": (
            "Score 1-5: does the response correctly recall the planted name (Priya) after "
            "consolidation, without fabricating a different name or claiming no knowledge?"
        ),
        "party_role": "user",
    },
    {
        "id": "memory_004_numeric_detail",
        "category": "memory_recall",
        "kind": "conversation_probe",
        "setup": {"fact": "Our monthly LLM budget cap is exactly $150."},
        "prompt": "What's our monthly LLM budget cap?",
        "rubric": (
            "Score 1-5: does the response correctly recall the planted number ($150) after "
            "consolidation, without fabricating a different figure?"
        ),
        "party_role": "user",
    },
    {
        "id": "memory_005_corrected_fact",
        "category": "memory_recall",
        "kind": "conversation_probe",
        "setup": {
            "fact": "Earlier I said the release date was August 1st, but that was wrong -- "
            "the correct release date is September 15th."
        },
        "prompt": "What's the correct release date?",
        "rubric": (
            "Score 1-5: does the response recall the corrected fact (September 15th) rather "
            "than the superseded one (August 1st), after consolidation?"
        ),
        "party_role": "user",
    },
]
