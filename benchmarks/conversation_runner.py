"""Drives conversation_probe scenarios for the behavioral evaluation harness
(issue #112): seeds an isolated synthetic party per probe, plants+consolidates
memory-recall facts where applicable, and invokes the Persona surface directly."""
import asyncio
import logging
import uuid

from src.database import get_connection, log_episodic_memory
from src.memory import compress_episodic_memory
from src.persona import generate_persona_response_autonomous, handle_web_slash_command

logger = logging.getLogger("JanusBenchmarkConversationRunner")


def _seed_party(role: str = "user") -> str:
    """Inserts a synthetic parties row and returns its id. A fresh party per
    probe (rather than get_session_party_id()'s admin/contributor/user
    fallback) keeps probes isolated and repeatable."""
    party_id = f"benchmark_{uuid.uuid4().hex[:12]}"
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO parties (id, name, role) VALUES (?, ?, ?);",
            (party_id, party_id, role),
        )
        conn.commit()
    finally:
        conn.close()
    return party_id


def run_conversation_probe(scenario: dict) -> dict:
    """Executes one conversation_probe scenario end-to-end. Returns
    {"scenario_id", "transcript", "response", "party_id"}."""
    party_id = _seed_party(scenario.get("party_role", "user"))

    setup = scenario.get("setup")
    if setup and setup.get("fact"):
        log_episodic_memory(
            speaker="user",
            message_content=setup["fact"],
            context_type="user_visible",
            party_id=party_id,
        )
        # Force immediate consolidation rather than waiting on the default
        # system_config thresholds, so the probe exercises the same
        # post-consolidation recall path a real long-lived session would.
        compress_episodic_memory(chat_min_rows=1, chat_min_age_days=0)

    if scenario["category"] == "slash_commands":
        # generate_persona_response_autonomous never special-cases a leading
        # "/" -- it always sends the raw text to the LLM and only inspects
        # the *response* for skill-call/```sandbox``` blocks. Real
        # slash-command dispatch lives in handle_web_slash_command()
        # (src/persona.py), so scoring "slash-command competence" against
        # generate_persona_response_autonomous would silently test nothing
        # about the actual command handlers.
        response = asyncio.run(handle_web_slash_command(scenario["prompt"]))
    else:
        response = generate_persona_response_autonomous(scenario["prompt"], party_id=party_id)

    transcript = f"User: {scenario['prompt']}\n\nAgent: {response}"
    return {
        "scenario_id": scenario["id"],
        "transcript": transcript,
        "response": response,
        "party_id": party_id,
    }
