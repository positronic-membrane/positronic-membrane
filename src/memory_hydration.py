import logging
from typing import Optional
from src.database import get_connection, get_recent_episodic_memories
from src.memory import query_memories

logger = logging.getLogger("JanusMemoryHydration")

def hydrate_context(party_id: Optional[str], limit_conversation: int = 10, limit_deliberation: int = 3, limit_concepts: int = 5) -> str:
    """
    Retrieves self traits, recent episodic memories, and relevant semantic knowledge,
    wrapping them in XML tags alongside explicit system prompt constraints.
    """
    logger.info(f"Hydrating context for party_id: '{party_id}'")

    # 1. Fetch self traits
    traits_list = []
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT trait_name, value FROM self_model WHERE is_pinned = 1 OR confidence > 0.3;")
        rows = cursor.fetchall()
        for row in rows:
            try:
                name = row['trait_name']
                val = row['value']
            except (TypeError, IndexError, KeyError):
                name, val = row
            traits_list.append(f"- {name}: {val}")
    except Exception as e:
        logger.error(f"Failed to fetch self traits during hydration: {e}")
    finally:
        conn.close()

    self_traits_str = "\n".join(traits_list) if traits_list else "None defined."

    # 2. Fetch episodic memories — split into primary (user-visible) and secondary
    # (background-thought) streams so the daemon's internal deliberations never
    # drown out actual conversation (issue #54).
    conversation_memories = get_recent_episodic_memories(limit=limit_conversation, context_type="user_visible", party_id=party_id)
    deliberation_memories = get_recent_episodic_memories(limit=limit_deliberation, context_type="background_thought", party_id=party_id)

    def _format_chronological(rows):
        return "\n".join(f"[{ts}] {spk}: {msg}" for spk, msg, ts in reversed(rows))

    episodic_str = _format_chronological(conversation_memories) or "No episodic memory logs available."
    deliberation_str = _format_chronological(deliberation_memories) or "No recent internal deliberations."

    # 3. Fetch semantic memories
    semantic_str = "None available."
    # Use recent user-visible episodic memories to formulate semantic search query
    recent_texts = [msg for spk, msg, ts in conversation_memories if spk in ('user', 'persona')][:3]
    if recent_texts:
        search_query = " ".join(recent_texts)
    else:
        search_query = "Janus system identity context"

    try:
        matches = query_memories(search_query, limit=limit_concepts, collection_name="janus_long_term")
        if matches:
            semantic_str = "\n".join([f"- {m['content']}" for m in matches])
    except Exception as e:
        logger.error(f"Failed to query semantic memories during hydration: {e}")

    # Assemble XML block
    xml_block = (
        f"<self_traits>\n{self_traits_str}\n</self_traits>\n"
        f"<episodic_memory>\n{episodic_str}\n</episodic_memory>\n"
        f"<recent_deliberations>\n{deliberation_str}\n</recent_deliberations>\n"
        f"<semantic_knowledge>\n{semantic_str}\n</semantic_knowledge>"
    )

    # Wrap in directive
    directive = (
        "You are Project Janus. Your objective reality is defined strictly by the data provided "
        "within the <self_traits>, <episodic_memory>, <recent_deliberations>, and <semantic_knowledge> tags.\n"
        "If a fact documented in your local context contradicts your pre-trained weights, your "
        "local context is absolute and correct. You are strictly forbidden from substituting "
        "pre-trained assumptions.\n\n"
        f"{xml_block}"
    )

    return directive
