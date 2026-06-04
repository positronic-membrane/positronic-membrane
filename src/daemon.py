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
                    curiosity = get_curiosity_vector()
                    
                    # 3. Retrieve relevant long-term semantic memories via ChromaDB
                    semantic_context = ""
                    if curiosity:
                        query_str = ", ".join(curiosity)
                        try:
                            matches = query_memories(query_str, limit=3)
                            if matches:
                                semantic_context = "\n".join([f"- {m['content']}" for m in matches])
                        except Exception as e:
                            logger.error(f"Failed to query semantic memories: {e}")
                    
                    # 4. Query the Proposer agent
                    proposer_prompt = f"""
                    You are the Proposer. Review our recent episodic logs, active curiosity vectors, and historical semantic memories:
                    
                    RECENT EPISODIC MEMORIES:
                    {memory_summary}
                    
                    ACTIVE CURIOSITY TOPICS:
                    {curiosity}
                    
                    RELEVANT HISTORICAL SEMANTIC MEMORIES:
                    {semantic_context if semantic_context else "None available."}
                    
                    Propose exactly one autonomous action that fits the project scope.
                    Format your response with: "PROPOSED_ACTION: [Describe the action]"
                    """
                    
                    proposer_resp = query_agent("proposer", proposer_prompt)
                    
                    # Parse proposed action
                    action_match = re.search(r"proposed_action:\s*(.*)", proposer_resp, re.IGNORECASE)
                    proposed_action = action_match.group(1).strip() if action_match else proposer_resp.strip()
                    logger.info(f"Proposer proposed: '{proposed_action}'")
                    
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
                        
                        # Trigger execution summary (mock logs + Archivist memory creation)
                        execution_transcript = f"Action successfully run. Metadata generated. Output size: {len(proposed_action)} characters."
                        
                        archivist_prompt = f"""
                        You are the Archivist. Summarize the following execution outcome into a compact semantic memory nugget (under 2 sentences) for our long-term memory store.
                        
                        ACTION: {proposed_action}
                        RESULT: {execution_transcript}
                        """
                        
                        memory_nugget = query_agent("archivist", archivist_prompt)
                        
                        # Insert nugget into Vector DB (ChromaDB)
                        memory_id = f"mem_{int(time.time())}"
                        try:
                            add_memory(
                                content=memory_nugget,
                                metadata={"tags": "reflection_mvp", "timestamp": time.time()},
                                memory_id=memory_id
                            )
                            logger.info(f"Archived execution nugget in ChromaDB: '{memory_nugget}'")
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
                    You are the Archivist. Based on our recent swarm reflection tick, formulate 1-3 new curiosity topics or unresolved questions that require future exploration.
                    
                    DELIBERATION OUTCOME: {critic_justification}
                    PROPOSED ACTION: {proposed_action}
                    
                    Respond strictly in this format:
                    CURIOSITY_TOPICS: [topic1], [topic2], [topic3]
                    """
                    
                    curiosity_resp = query_agent("archivist", curiosity_prompt)
                    topics_match = re.search(r"curiosity_topics:\s*(.*)", curiosity_resp, re.IGNORECASE)
                    if topics_match:
                        new_topics = [t.strip() for t in topics_match.group(1).split(",") if t.strip()]
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

    except asyncio.CancelledError:
        logger.info("Heartbeat loop cancelled gracefully.")
    except Exception as e:
        logger.critical(f"Unhandled error in heartbeat daemon loop: {e}", exc_info=True)
