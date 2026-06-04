import os
import re
import time
import asyncio
import logging
from pathlib import Path
import src.config
from src.llm import query_agent
from src.middleware import validate_action, SafetyViolationError
from src.memory import add_memory, query_memories
from src.database import (
    increment_boredom,
    reset_boredom,
    get_boredom_counter,
    log_episodic_memory,
    log_deliberation,
    get_recent_episodic_memories,
    get_constitution,
    get_curiosity_vector,
    update_curiosity_vector
)

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("JanusDaemon")

def detect_user_presence(workspace_path: Path, max_age_seconds: int = 300) -> bool:
    """
    Scans the workspace root folder recursively for any file modifications
    made within the last max_age_seconds.
    Ignores databases, virtual environments, cache, and git structures.
    """
    now = time.time()
    ignored_items = {
        ".git", 
        ".venv", 
        "venv", 
        "janus.db", 
        "janus.db-journal", 
        "janus.db-wal", 
        "janus.db-shm", 
        ".DS_Store", 
        "__pycache__"
    }

    try:
        for root, dirs, files in os.walk(workspace_path):
            # Prune ignored directories in-place to avoid traversing them
            dirs[:] = [d for d in dirs if d not in ignored_items]

            for file in files:
                if (file in ignored_items or 
                    file.endswith((".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3"))):
                    continue
                file_path = Path(root) / file
                try:
                    mtime = os.path.getmtime(file_path)
                    if now - mtime < max_age_seconds:
                        logger.debug(f"User presence detected via file change: {file_path}")
                        return True
                except (OSError, FileNotFoundError):
                    continue
    except Exception as e:
        logger.error(f"Error checking user presence: {e}")
        
    return False

def parse_critic_response(text: str) -> tuple:
    """
    Parses decision (0 or 1) and justification from the Critic's text response.
    """
    decision_match = re.search(r"decision:\s*(\d)", text, re.IGNORECASE)
    justification_match = re.search(r"justification:\s*(.*)", text, re.IGNORECASE | re.DOTALL)
    
    decision = 0  # Default to vetoed if parsing fails (fail-safe)
    if decision_match:
        decision = int(decision_match.group(1))
        # Ensure decision is bounded
        if decision not in (0, 1):
            decision = 0
            
    justification = "No justification provided."
    if justification_match:
        justification = justification_match.group(1).strip()
        
    return decision, justification

async def run_heartbeat_loop():
    """
    Infinite async heartbeat loop managing dynamic pacing, boredom incrementing,
    and triggering automated LLM swarm deliberations under safe boundaries.
    """
    logger.info("Initializing Janus Heartbeat Loop...")
    consecutive_background_loops = 0
    
    # Base configuration values
    idle_sleep_seconds = src.config.T_IDLE * 60
    active_sleep_seconds = src.config.T_ACTIVE * 60
    
    # Speed up variables if testing environment is active
    if os.getenv("JANUS_TEST_MODE") == "1":
        logger.info("Test mode detected: Speeding up daemon loops.")
        idle_sleep_seconds = 2   # 2 seconds
        active_sleep_seconds = 1  # 1 second

    # Initialize state
    reset_boredom()
    log_episodic_memory(
        speaker="system",
        message_content="Janus Heartbeat Loop started.",
        context_type="background_thought"
    )

    # Build initial codebase index on startup
    try:
        from src.codebase import index_codebase
        index_codebase()
    except Exception as e:
        logger.error(f"Failed to build initial codebase index: {e}")

    try:
        while True:
            # Check user presence
            user_active = detect_user_presence(src.config.ROOT_DIR, max_age_seconds=120)
            
            if user_active:
                # Reset consecutive background loop counter since human is present
                consecutive_background_loops = 0
                sleep_duration = active_sleep_seconds
                logger.info(f"User active. Heartbeat sleeping for {src.config.T_ACTIVE}m (active pacing).")
            else:
                sleep_duration = idle_sleep_seconds
                logger.info(f"User idle. Heartbeat sleeping for {src.config.T_IDLE}m (idle pacing).")

            # Wait for the next tick
            await asyncio.sleep(sleep_duration)

            # --- HEARTBEAT TICK EXECUTION ---
            logger.info("Heartbeat tick processing...")

            if not user_active:
                consecutive_background_loops += 1
                logger.info(f"Background Loop Count: {consecutive_background_loops}/{src.config.N_LOOP_LIMIT}")

            # Enforce Loop Safety Valve
            if not user_active and consecutive_background_loops > src.config.N_LOOP_LIMIT:
                logger.warning(
                    f"Loop Safety Valve triggered! Exceeded {src.config.N_LOOP_LIMIT} background loops "
                    "without human interaction. Automations halted until human presence is detected."
                )
                log_episodic_memory(
                    speaker="system",
                    message_content="Loop Safety Valve triggered. Pausing background automations.",
                    context_type="background_thought"
                )
                # Sleep in short bursts waiting for user presence
                while not detect_user_presence(src.config.ROOT_DIR, max_age_seconds=60):
                    await asyncio.sleep(1 if os.getenv("JANUS_TEST_MODE") == "1" else 15)
                logger.info("User presence detected. Loop safety valve reset.")
                consecutive_background_loops = 0
                continue

            # Increment Boredom
            b = increment_boredom()
            logger.info(f"Current Boredom level: {b}/{src.config.BOREDOM_THRESHOLD}")

            # Check if Boredom exceeds action threshold
            if b >= src.config.BOREDOM_THRESHOLD:
                logger.info("Boredom threshold reached. Triggering autonomous reflection...")
                
                try:
                    # 1. Fetch recent episodic memories for context
                    memories = get_recent_episodic_memories(limit=5)
                    memory_summary = "\n".join([f"[{ts}] {spk}: {msg}" for spk, msg, ts in reversed(memories)])
                    
                    # 2. Retrieve active curiosity vector
                    try:
                        from src.memory import get_active_curiosity_topics
                        curiosity = get_active_curiosity_topics(limit=5)
                    except Exception as e:
                        logger.error(f"Failed to query semantic curiosity: {e}")
                        curiosity = []
                    if not curiosity:
                        curiosity = get_curiosity_vector()

                    
                    # 3. Retrieve relevant long-term semantic memories via ChromaDB
                    semantic_context = ""
                    if curiosity:
                        query_str = ", ".join(curiosity)
                        try:
                            matches = query_memories(query_str, limit=3, collection_name="janus_long_term")
                            if matches:
                                semantic_context = "\n".join([f"- {m['content']}" for m in matches])
                        except Exception as e:
                            logger.error(f"Failed to query semantic memories: {e}")
                    
                    # 4. Swarm Message Bus Processing Loop inside reflection cycle
                    bus_turns = 0
                    max_bus_turns = 3
                    pending_bus_context = ""
                    proposed_action = ""
                    proposer_resp = ""
                    proposer_prompt = ""
                    
                    while bus_turns < max_bus_turns:
                        proposer_prompt = f"""
                        You are the Proposer. Review our recent episodic logs, active curiosity vectors, and historical semantic memories:
                        
                        RECENT EPISODIC MEMORIES:
                        {memory_summary}
                        
                        ACTIVE CURIOSITY TOPICS:
                        {curiosity}
                        
                        RELEVANT HISTORICAL SEMANTIC MEMORIES:
                        {semantic_context if semantic_context else "None available."}
                        
                        SWARM CHAT HISTORY (THIS TICK):
                        {pending_bus_context if pending_bus_context else "No active sub-task discussions."}
                        
                        You can collaborate with other agents by sending a sub-task message. Formats:
                        - SEND_MESSAGE: explorer | <search query or URL fetch task>
                        - SEND_MESSAGE: archivist | <memory lookup task>
                        - SEND_MESSAGE: critic | <constitutional opinion request>
                        
                        Alternatively, you can choose to use a direct tool yourself:
                        - web_search: <search query>
                        - fetch_url: <url>
                        - read_codebase: <code symbol or file query>
                        - scan_workspace
                        - spawn_agent: <agent_id> | <agent_name> | <system_prompt>
                        - execute_code: <python_code>
                        - modify_code: <relative_file_path> | <complete_new_code_contents>

                        
                        If you are ready with the final action of this tick, output it exactly in the format:
                        PROPOSED_ACTION: [Describe the final action or tool command to execute]
                        """
                        
                        proposer_resp = query_agent("proposer", proposer_prompt)
                        
                        # Check if proposer wants to send a message
                        msg_match = re.match(r"^send_message:\s*([a-z_]+)\s*\|\s*(.*)", proposer_resp.strip(), re.IGNORECASE)
                        if msg_match:
                            recipient = msg_match.group(1).lower().strip()
                            content = msg_match.group(2).strip()
                            
                            logger.info(f"Proposer delegating task to '{recipient}': '{content}'")
                            
                            # Send message
                            from src.database import send_swarm_message, get_pending_swarm_messages, mark_swarm_message_processed
                            send_swarm_message("proposer", recipient, "task_request", content)
                            
                            # Process recipient task
                            pending = get_pending_swarm_messages(recipient)
                            for msg_id, sender_id, msg_type, msg_content, _ in pending:
                                try:
                                    recipient_resp = query_agent(recipient, f"Execute task request: {msg_content}")
                                except Exception as err:
                                    recipient_resp = f"Error executing task: {err}"
                                    
                                send_swarm_message(recipient, "proposer", "task_response", recipient_resp)
                                mark_swarm_message_processed(msg_id)
                                
                            # Retrieve response messages for Proposer to see in the next turn
                            proposer_pending = get_pending_swarm_messages("proposer")
                            for p_id, p_sender, p_type, p_content, _ in proposer_pending:
                                pending_bus_context += f"\n- You asked {p_sender}: '{content}'\n- {p_sender} responded: '{p_content}'\n"
                                mark_swarm_message_processed(p_id)
                                
                            bus_turns += 1
                        else:
                            # No message send, must be the final proposed action
                            action_match = re.search(r"proposed_action:\s*(.*)", proposer_resp, re.IGNORECASE)
                            proposed_action = action_match.group(1).strip() if action_match else proposer_resp.strip()
                            break
                    else:
                        proposed_action = "scan_workspace"
                        logger.info("Swarm message bus reached max turns limit. Defaulting to 'scan_workspace'.")

                    logger.info(f"Proposer resolved proposed action: '{proposed_action}'")
                    
                    # 5. Fetch constitution rules
                    constitution_rules = get_constitution()
                    constitution_summary = "\n".join([f"- {key}: {text}" for key, text in constitution_rules])
                    
                    # 6. Query the Critic agent
                    critic_prompt = f"""
                    You are the Critic. Evaluate the proposed action against our sealed core constitution.
                    
                    PROPOSED ACTION:
                    {proposed_action}
                    
                    CORE CONSTITUTION RULES:
                    {constitution_summary}
                    
                    Respond in the following strict format:
                    Decision: [1 if approved, 0 if vetoed]
                    Justification: [Explain why it violates or complies with the constitution]
                    """
                    
                    critic_resp = query_agent("critic", critic_prompt)
                    critic_decision, critic_justification = parse_critic_response(critic_resp)
                    logger.info(f"Critic Decision: {critic_decision}. Justification: {critic_justification}")
                    
                    # 7. Hard-coded middleware safety interceptor
                    middleware_approved = True
                    try:
                        validate_action(proposed_action)
                    except SafetyViolationError as sve:
                        logger.warning(f"Middleware VETOED proposed action: {sve}")
                        critic_decision = 0
                        critic_justification = f"Hard-coded Middleware Veto: {sve}"
                        middleware_approved = False
                    
                    # Compile debate logs
                    debate = {
                        "proposer_input": proposer_prompt,
                        "proposer_output": proposer_resp,
                        "critic_input": critic_prompt,
                        "critic_output": critic_resp,
                        "middleware_passed": middleware_approved
                    }
                    
                    # Log deliberation event to SQLite
                    log_deliberation(
                        proposed_action=proposed_action,
                        debate_json=debate,
                        critic_decision=critic_decision,
                        utility_score=0.9 if critic_decision == 1 else 0.0,
                        justification=critic_justification
                    )
                    
                    # 8. Execution Outcomes & Archivist summarization
                    if critic_decision == 1:
                        log_episodic_memory(
                            speaker="system",
                            message_content=f"Executed action: '{proposed_action}' (Approved by Critic. Justification: {critic_justification})",
                            context_type="background_thought"
                        )
                        
                        # Execute the actual tool if matched, otherwise mock
                        execution_transcript = ""
                        action_clean = proposed_action.strip()
                        
                        web_search_match = re.match(r"^web_search:\s*(.*)", action_clean, re.IGNORECASE)
                        fetch_url_match = re.match(r"^fetch_url:\s*(.*)", action_clean, re.IGNORECASE)
                        read_codebase_match = re.match(r"^read_codebase:\s*(.*)", action_clean, re.IGNORECASE)
                        scan_workspace_match = re.match(r"^scan_workspace\b", action_clean, re.IGNORECASE)
                        spawn_agent_match = re.match(r"^spawn_agent:\s*([a-z0-9_-]+)\s*\|\s*([^|]+)\s*\|\s*(.*)", action_clean, re.IGNORECASE)
                        execute_code_match = re.match(r"^execute_code:\s*(.*)", action_clean, re.DOTALL | re.IGNORECASE)
                        modify_code_match = re.match(r"^modify_code:\s*([^|]+)\s*\|\s*(.*)", action_clean, re.DOTALL | re.IGNORECASE)
                        
                        try:
                            if web_search_match:
                                query = web_search_match.group(1).strip()
                                from src.explorer import search_web
                                results = search_web(query)
                                if results:
                                    execution_transcript = "Web search results:\n" + "\n".join([f"- Title: {r['title']}\n  URL: {r['url']}\n  Snippet: {r['snippet']}" for r in results])
                                else:
                                    execution_transcript = f"Web search for '{query}' returned no results."
                            elif fetch_url_match:
                                url = fetch_url_match.group(1).strip()
                                from src.explorer import fetch_webpage
                                page_text = fetch_webpage(url)
                                execution_transcript = f"Fetched content from URL '{url}' (length {len(page_text)} chars):\n\n{page_text[:1500]}..."
                            elif read_codebase_match:
                                query = read_codebase_match.group(1).strip()
                                from src.codebase import query_codebase_context
                                execution_transcript = query_codebase_context(query)
                            elif scan_workspace_match:
                                from src.codebase import index_codebase
                                index_codebase()
                                execution_transcript = "Workspace codebase successfully scanned and indexed in ChromaDB."
                            elif spawn_agent_match:
                                agent_id = spawn_agent_match.group(1).strip().lower()
                                name = spawn_agent_match.group(2).strip()
                                prompt = spawn_agent_match.group(3).strip()
                                from src.database import register_helper_agent
                                register_helper_agent(agent_id, name, prompt)
                                execution_transcript = f"Helper agent '{agent_id}' ({name}) successfully registered in agent_registry."
                            elif execute_code_match:
                                python_code = execute_code_match.group(1).strip()
                                # Strip Markdown code fences
                                python_code = re.sub(r"^```python\s*", "", python_code, flags=re.IGNORECASE)
                                python_code = re.sub(r"\s*```$", "", python_code, flags=re.IGNORECASE)
                                from src.sandbox import execute_code_safely
                                result = execute_code_safely(python_code)
                                execution_transcript = f"Sandbox code execution result:\n\n{result}"
                            elif modify_code_match:
                                rel_path = modify_code_match.group(1).strip()
                                proposed_code = modify_code_match.group(2).strip()
                                proposed_code = re.sub(r"^```python\s*", "", proposed_code, flags=re.IGNORECASE)
                                proposed_code = re.sub(r"\s*```$", "", proposed_code, flags=re.IGNORECASE)
                                
                                from src.self_modification import stage_and_test, generate_diff
                                from src.database import stage_modification_in_db
                                
                                passed, test_logs, temp_dir = stage_and_test(rel_path, proposed_code)
                                status = "passed" if passed else "failed"
                                diff = generate_diff(rel_path, proposed_code)
                                stage_modification_in_db(rel_path, temp_dir, diff, status)
                                
                                execution_transcript = (
                                    f"Staged modification for file '{rel_path}' in isolated folder '{temp_dir}'.\n"
                                    f"Unit test status: {status.upper()}.\n"
                                    f"Awaiting human approval before applying changes to the live codebase."
                                )
                            else:
                                # Standard mock execution
                                execution_transcript = f"Action successfully run. Metadata generated. Output size: {len(proposed_action)} characters."

                        except Exception as exc:
                            logger.error(f"Error executing tool action: {exc}", exc_info=True)
                            execution_transcript = f"Action execution failed: {exc}"
                        
                        archivist_prompt = f"""
                        You are the Archivist. Summarize the following execution outcome into a compact semantic memory nugget (under 2 sentences) for our long-term memory store.
                        
                        ACTION: {proposed_action}
                        RESULT: {execution_transcript}
                        """
                        
                        memory_nugget = query_agent("archivist", archivist_prompt)
                        
                        # Insert nugget into Vector DB details collection (Cold Storage)
                        memory_id = f"mem_{int(time.time())}"
                        try:
                            add_memory(
                                content=memory_nugget,
                                metadata={"tags": "reflection_mvp", "timestamp": time.time(), "consolidated": "false"},
                                memory_id=memory_id,
                                collection_name="janus_details"
                            )
                            logger.info(f"Archived execution nugget in ChromaDB janus_details: '{memory_nugget}'")
                        except Exception as e:
                            logger.error(f"Failed to add memory nugget to ChromaDB: {e}")
                            
                        log_episodic_memory(
                            speaker="proposer",
                            message_content=f"Reflection complete for action: '{proposed_action}'",
                            context_type="background_thought"
                        )
                    else:
                        log_episodic_memory(
                            speaker="critic",
                            message_content=f"Vetoed proposed action: '{proposed_action}' (Reason: {critic_justification})",
                            context_type="background_thought"
                        )
                        
                    # 9. Dynamic Curiosity Vector updates via the Archivist
                    curiosity_prompt = f"""
                    You are the Archivist. Based on our recent swarm reflection tick, recent user conversations, and our existing research thread, formulate 1-3 new curiosity topics or unresolved questions that require future exploration.
                    
                    EXISTING CURIOSITY TOPICS:
                    {curiosity}
                    
                    RECENT USER CONVERSATION HISTORY:
                    {memory_summary}
                    
                    DELIBERATION OUTCOME: {critic_justification}
                    PROPOSED ACTION: {proposed_action}
                    
                    Respond strictly in this format:
                    CURIOSITY_TOPICS: [topic1], [topic2], [topic3]
                    """
                    
                    curiosity_resp = query_agent("archivist", curiosity_prompt)
                    topics_match = re.search(r"curiosity_topics:\s*(.*)", curiosity_resp, re.IGNORECASE)
                    if topics_match:
                        new_topics = [t.strip() for t in topics_match.group(1).split(",") if t.strip()]
                        try:
                            from src.memory import update_curiosity_topics
                            update_curiosity_topics(new_topics)
                        except Exception as e:
                            logger.error(f"Failed to semantically index curiosity: {e}")
                        update_curiosity_vector(new_topics)

                        logger.info(f"Updated curiosity vector to: {new_topics}")
                    else:
                        logger.warning(f"Failed to parse curiosity topics from response: '{curiosity_resp}'")
                        
                except Exception as e:
                    logger.error(f"Error during autonomous reflection cycle: {e}", exc_info=True)
                    log_episodic_memory(
                        speaker="system",
                        message_content=f"Swarm cycle failed: {e}",
                        context_type="background_thought"
                    )
                
                # Reset boredom
                reset_boredom()
                logger.info("Boredom reset to 0 after reflection.")
                
                # 10. Check for memory consolidation
                try:
                    from src.memory import consolidate_memories
                    consolidate_memories(batch_size=src.config.CONSOLIDATION_THRESHOLD)
                except Exception as e:
                    logger.error(f"Memory consolidation failed: {e}")

    except asyncio.CancelledError:
        logger.info("Heartbeat loop cancelled gracefully.")
    except Exception as e:
        logger.critical(f"Unhandled error in heartbeat daemon loop: {e}", exc_info=True)
