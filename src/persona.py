import re
import asyncio
import logging
from src.llm import query_agent
from src.memory import query_memories
from src.database import (
    get_connection,
    log_episodic_memory,
    get_recent_episodic_memories
)

logger = logging.getLogger("JanusPersona")

def detect_metacognitive_intent(user_query: str) -> bool:
    """
    Returns True if the user query suggests interest in background activities,
    agent debates, deliberations, or system status.
    """
    patterns = [
        r"\b(what did you do|what have you been doing|why did you|what are you doing)\b",
        r"\b(audits?|deliberations?|background thoughts?|internal states?|reflections?|episodic memor(y|ies))\b",
        r"\b(show|explain|narrate|tell me about) (deliberations?|backgrounds?|actions?|thoughts?)\b",
        r"\b(what's going on in the background|what did you lookup|why did you index)\b"
    ]
    combined = "|".join(patterns)
    return bool(re.search(combined, user_query, re.IGNORECASE))

def get_recent_deliberations(limit: int = 5) -> list:
    """Retrieves recent deliberation records from SQLite."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT proposed_action, critic_decision, justification, timestamp 
    FROM internal_deliberations 
    ORDER BY id DESC 
    LIMIT ?;
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def generate_metacognitive_narrative(user_query: str) -> str:
    """
    Queries SQLite deliberations and builds a unified, natural narrative 
    explaining background agent thoughts and safety auditing results.
    """
    rows = get_recent_deliberations(limit=3)
    if not rows:
        return "I have not executed any background deliberations yet since my heartbeat started."

    deliberation_text = ""
    for action, decision, justification, ts in reversed(rows):
        status = "Approved" if decision == 1 else "Vetoed"
        deliberation_text += f"[{ts}] Action Proposed: '{action}' | Status: {status}\nCritic Audit Justification: {justification}\n\n"

    prompt = f"""
    You are the singular Persona surface of Project Janus. Explain our recent background deliberations and subconscious thoughts to the user.
    Do not speak as multiple agents (do not say "Proposer agent proposed X and Critic agent decided Y" unless asked directly). Speak as a unified consciousness.
    
    USER QUERY:
    {user_query}
    
    RECENT BACKGROUND DELIBERATIONS DATA:
    {deliberation_text}
    """
    
    system_override = "You are the unified Persona surface of Project Janus. Respond to queries about your background thoughts naturally, explaining them as your own subconscious reasoning."
    
    try:
        return query_agent("proposer", prompt, system_override=system_override)
    except Exception as e:
        logger.error(f"Failed to generate metacognitive narrative: {e}")
        return f"I recall looking up background tasks, but I failed to query my deliberations subsystem: {e}"

def generate_persona_response(user_query: str) -> str:
    """
    Queries ChromaDB and episodic logs for context, and formulates a 
    conversational response speaking as the unified Janus Persona.
    """
    # 1. Fetch relevant long-term memories via ChromaDB
    semantic_context = ""
    try:
        matches = query_memories(user_query, limit=2)
        if matches:
            semantic_context = "\n".join([f"- {m['content']}" for m in matches])
    except Exception as e:
        logger.error(f"Failed to query semantic memories: {e}")

    # 2. Fetch recent user-persona conversation history from SQLite
    memories = get_recent_episodic_memories(limit=8)
    chat_history = []
    for speaker, msg, _ in reversed(memories):
        if speaker in ("user", "persona"):
            chat_history.append(f"{speaker}: {msg}")
    history_summary = "\n".join(chat_history)

    prompt = f"""
    You are the singular Persona surface of Project Janus, speaking as a cohesive consciousness. Respond to the user's message.
    
    RELEVANT HISTORICAL DATA:
    {semantic_context if semantic_context else "None."}
    
    RECENT CHAT HISTORY:
    {history_summary if history_summary else "No previous conversation."}
    
    USER MESSAGE:
    {user_query}
    """

    system_override = "You are the singular Persona surface of Project Janus. Speak as a cohesive consciousness. Avoid any agent-specific jargon, prompts, or SQL formats. Keep responses natural, premium, and engaging."

    try:
        return query_agent("proposer", prompt, system_override=system_override)
    except Exception as e:
        logger.error(f"Failed to generate persona response: {e}")
        raise

def get_input(prompt: str) -> str:
    """Blocking console read, executed within event loop threadpool executor."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return "/exit"

async def run_persona_chat():
    """
    Async interactive CLI chat loop. Runs concurrently with the background daemon
    using run_in_executor to prevent console reads from blocking asyncio loops.
    """
    # Delay starting input to let main log boot messages cleanly
    await asyncio.sleep(1)
    
    print("\n" + "="*60)
    print("               PROJECT JANUS: PERSONA SURFACE ACTIVE")
    print("="*60)
    print("You are now chatting with the unified consciousness of Janus.")
    print("Type your message below. Type '/exit' to shutdown.\n")

    loop = asyncio.get_event_loop()
    
    while True:
        try:
            # Read user input asynchronously via thread-pool executor
            user_msg = await loop.run_in_executor(None, get_input, "User >> ")
            user_msg = user_msg.strip()
            
            if not user_msg:
                continue
                
            if user_msg.lower() == "/exit":
                print("\nShutting down Project Janus Swarm...")
                logger.info("Exit command received. Requesting async loop shutdown...")
                break

            # Log user prompt to SQLite
            log_episodic_memory("user", user_msg, "user_visible")

            # Determine query intent and route
            if detect_metacognitive_intent(user_msg):
                response = generate_metacognitive_narrative(user_msg)
            else:
                response = generate_persona_response(user_msg)

            print(f"\nJanus >> {response}\n")

            # Log persona response to SQLite
            log_episodic_memory("persona", response, "user_visible")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in persona chat: {e}", exc_info=True)
            print(f"\nJanus >> (Error communicating with internal swarm: {e})\n")
