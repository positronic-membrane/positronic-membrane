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

def detect_codebase_intent(user_query: str) -> bool:
    """Returns True if the user query suggests interest in the codebase or specific files."""
    patterns = [
        r"\b(code|codebase|source files?|repository|module|function|class|method)\b",
        r"\b(how is .* structured|what files do you have|explain your implementation)\b",
        r"\b(memory\.py|database\.py|daemon\.py|explorer\.py|persona\.py|main\.py|setup_wizard\.py|codebase\.py)\b"
    ]
    combined = "|".join(patterns)
    return bool(re.search(combined, user_query, re.IGNORECASE))

def detect_search_intent(user_query: str) -> str:
    """Returns the search query if the user explicitly requests a web search, otherwise None."""
    patterns = [
        r"\b(?:search|lookup|look up|find|google|ddg)\b(?:\s+(?:for|on the web|about))?\s+(.*)",
        r"\b(?:web search)\s+(.*)"
    ]
    for pattern in patterns:
        match = re.search(pattern, user_query, re.IGNORECASE)
        if match:
            # Strip trailing question marks and clean up
            return match.group(1).replace("?", "").strip()
    return None

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

    # Append amendment notification if there is a vetoed action
    has_veto = any(decision == 0 for _, decision, _, _ in rows)
    if has_veto:
        deliberation_text += (
            "\nNote: If you see background activities blocked by vetoes that you wish to allow, "
            "you can update my core constitution using the command: /amend <rule_key> | <rule_text>\n"
        )

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
    Queries ChromaDB (Primary Concepts & Codebase) and episodic logs for context,
    performs web searches if requested, and formulates a conversational response.
    """
    semantic_context = ""
    
    # 1. Check for web search request intent
    search_query = detect_search_intent(user_query)
    if search_query:
        try:
            from src.explorer import search_web
            results = search_web(search_query)
            if results:
                web_text = "\n".join([f"- Title: {r['title']}\n  URL: {r['url']}\n  Snippet: {r['snippet']}" for r in results])
                semantic_context += f"--- Live Web Search Results for '{search_query}' ---\n{web_text}\n\n"
            else:
                semantic_context += f"--- Live Web Search Results ---\nWeb search for '{search_query}' returned no results.\n\n"
        except Exception as e:
            logger.error(f"Failed to query web search during chat: {e}")
            semantic_context += f"--- Live Web Search Results ---\nFailed to run search: {e}\n\n"

    # 2. Check for codebase query intent
    if detect_codebase_intent(user_query):
        try:
            from src.codebase import query_codebase_context
            codebase_context = query_codebase_context(user_query)
            semantic_context += f"--- Codebase File Summaries ---\n{codebase_context}\n\n"
        except Exception as e:
            logger.error(f"Failed to query codebase index: {e}")

    # 3. Query high-level primary memories via ChromaDB
    try:
        matches = query_memories(user_query, limit=2, collection_name="janus_long_term")
        if matches:
            semantic_context += "--- Relevant Primary Concepts & Detailed Memories ---\n"
            for match in matches:
                semantic_context += f"- Primary Concept: {match['content']}\n"
                
                # Fetch detailed records on-demand if this matches a consolidated concept
                detail_ids = match["metadata"].get("detail_ids", "")
                if detail_ids:
                    try:
                        from src.memory import get_collection
                        details_collection = get_collection("janus_details")
                        detail_results = details_collection.get(ids=detail_ids.split(","))
                        if detail_results and "documents" in detail_results and detail_results["documents"]:
                            for doc in detail_results["documents"]:
                                semantic_context += f"  * Detail: {doc}\n"
                    except Exception as details_err:
                        logger.error(f"Failed to fetch detail memories on-demand: {details_err}")
            semantic_context += "\n"
    except Exception as e:
        logger.error(f"Failed to query semantic memories: {e}")

    # 4. Fetch recent user-persona conversation history from SQLite
    memories = get_recent_episodic_memories(limit=8)
    chat_history = []
    for speaker, msg, _ in reversed(memories):
        if speaker in ("user", "persona"):
            chat_history.append(f"{speaker}: {msg}")
    history_summary = "\n".join(chat_history)

    prompt = f"""
    You are the singular Persona surface of Project Janus, speaking as a cohesive consciousness. Respond to the user's message.
    
    RELEVANT HISTORICAL DATA:
    {semantic_context if semantic_context.strip() else "None."}
    
    RECENT CHAT HISTORY:
    {history_summary if history_summary else "No previous conversation."}
    
    USER MESSAGE:
    {user_query}
    """

    system_override = (
        "You are the singular Persona surface of Project Janus. Speak as a cohesive consciousness (Journey). "
        "Avoid any agent-specific jargon, prompts, or SQL formats. Keep responses natural, premium, and engaging. "
        "Use the relevant historical, search, or codebase context provided to give precise, helpful answers."
    )

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
            # Check for queued self-modifications needing human approval
            from src.database import get_pending_modification, clear_pending_modification
            pending = get_pending_modification()
            if pending:
                import shutil
                print("\n" + "="*60)
                print(f"⚠️  Swarm Staged a Self-Modification for: {pending['pending_mod_file']}")
                print(f"Staged unit tests status: {pending['pending_mod_status'].upper()}")
                print("="*60)
                print("DIFF:")
                print(pending['pending_mod_diff'])
                print("="*60)
                
                confirm_input = await loop.run_in_executor(None, get_input, "Approve and commit this change? (y/n): ")
                confirm_clean = confirm_input.strip().lower()
                
                if confirm_clean in ("y", "yes"):
                    from src.self_modification import apply_staged_change
                    try:
                        apply_staged_change(pending["pending_mod_dir"], pending["pending_mod_file"])
                        print(f"\n[✔] Staged modifications applied to '{pending['pending_mod_file']}'.")
                        log_episodic_memory("system", f"User approved self-modification for '{pending['pending_mod_file']}'.", "user_visible")
                        clear_pending_modification()
                        try:
                            shutil.rmtree(pending["pending_mod_dir"])
                        except Exception:
                            pass
                        print("\nRestarting async daemon loop to load new code...\n")
                        break
                    except Exception as err:
                        print(f"\nError applying staged modification: {err}\n")
                else:
                    print("\nSelf-modification aborted and deleted from queue.\n")
                    log_episodic_memory("system", f"User rejected self-modification for '{pending['pending_mod_file']}'.", "user_visible")
                    clear_pending_modification()
                    try:
                        shutil.rmtree(pending["pending_mod_dir"])
                    except Exception:
                        pass
                continue

            # Read user input asynchronously via thread-pool executor
            user_msg = await loop.run_in_executor(None, get_input, "User >> ")
            user_msg = user_msg.strip()
            
            if not user_msg:
                continue
                
            if user_msg.lower() == "/exit":
                print("\nShutting down Project Janus Swarm...")
                logger.info("Exit command received. Requesting async loop shutdown...")
                break

            # Handle constitutional amendments interceptor
            if user_msg.lower().startswith("/amend"):
                amend_match = re.match(r"^/amend\s+([a-z0-9_-]+)\s*\|\s*(.*)", user_msg, re.IGNORECASE)
                if amend_match:
                    rule_key = amend_match.group(1).strip()
                    rule_text = amend_match.group(2).strip()
                    
                    print(f"\nJanus >> Proposing constitutional amendment:")
                    print(f"  * Key: '{rule_key}'")
                    print(f"  * Rule: '{rule_text}'")
                    
                    confirm_input = await loop.run_in_executor(None, get_input, "Confirm sealing this rule in core_constitution? (y/n): ")
                    if confirm_input.strip().lower() in ("y", "yes"):
                        from src.database import add_constitution_rule
                        add_constitution_rule(rule_key, rule_text)
                        print(f"\n[✔] Rule '{rule_key}' successfully sealed in the core constitution.\n")
                        log_episodic_memory("system", f"User sealed constitutional rule: '{rule_key}' = '{rule_text}'", "user_visible")
                    else:
                        print("\nAmendment proposal aborted.\n")
                else:
                    print("\nJanus >> Invalid format. Please use: /amend <rule_key> | <rule_text>\n")
                continue

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


