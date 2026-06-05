import re
import asyncio
import logging
import src.config
from src.llm import query_agent
from src.memory import query_memories
from src.database import (
    get_connection,
    log_episodic_memory,
    get_recent_episodic_memories,
    log_deliberation
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

def detect_modification_intent(user_query: str) -> tuple:
    """
    Detects if the user is asking to modify a specific file in the repository.
    Returns: (file_path, instructions) or (None, None)
    """
    # 1. Check for slash command first: /modify <file_path> | <instructions>
    if user_query.lower().startswith("/modify"):
        match = re.match(r"^/modify\s+([^\s|]+)\s*\|\s*(.*)", user_query, re.IGNORECASE)
        if match:
            return match.group(1).strip(), match.group(2).strip()
        else:
            return "INVALID", None

    # 2. Check for natural language intent: "modify <file> to <change>" or similar
    # Look for files matching src/*.py or tests/*.py
    path_match = re.search(r"\b(src/[a-z0-9_.-]+\.py|tests/[a-z0-9_.-]+\.py)\b", user_query, re.IGNORECASE)
    if path_match:
        file_path = path_match.group(1)
        # Check for modification verbs
        if any(verb in user_query.lower() for verb in ["modify", "change", "edit", "update", "rewrite", "replace", "add to"]):
            return file_path, user_query
            
    return None, None

def get_recent_persona_messages(limit: int = 1) -> str:
    """Fetches recent message contents spoken by 'persona' from SQLite and concatenates them."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT message_content FROM episodic_memory 
    WHERE speaker = 'persona' 
    ORDER BY id DESC 
    LIMIT ?;
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        return None
    # Concatenate in chronological order (oldest to newest)
    return "\n\n".join(row[0] for row in reversed(rows))

def parse_proposed_changes(message_content: str) -> dict:
    """
    Extracts potential file paths from the message, retrieves their current live contents,
    and queries the LLM to construct a mapping of relative file paths to their complete updated contents.
    """
    import json
    
    # Scan message for relative file paths
    paths = re.findall(
        r"\b((?:src|tests)/[a-zA-Z0-9_/.-]+|[a-zA-Z0-9_/.-]+\.md|[a-zA-Z0-9_/.-]+\.json|requirements\.txt)\b",
        message_content
    )
    
    unique_paths = set()
    for p in paths:
        p = p.rstrip(".,;!?`\"'")
        # Ensure path is relative and doesn't do directory traversal
        try:
            full_path = src.config.ROOT_DIR / p
            full_path.relative_to(src.config.ROOT_DIR)
            unique_paths.add(p)
        except ValueError:
            pass
            
    current_files = {}
    for p in unique_paths:
        full_path = src.config.ROOT_DIR / p
        if full_path.is_file():
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    current_files[p] = f.read()
            except Exception:
                pass
                
    current_files_json = json.dumps(current_files, indent=2)
    prompt = f"""
    You are a precise parsing agent. Analyze the following message from Janus, which proposes file changes (either as new files, full code blocks, or unified diffs).
    Your task is to output the COMPLETE, updated content for every file proposed to be created or modified.

    We have provided the current live content of the files mentioned in the message below for your reference (to apply diffs or modifications).

    CURRENT FILE CONTENTS:
    {current_files_json}

    PROPOSED MESSAGE:
    {message_content}

    Generate a JSON object mapping each relative file path to its COMPLETE new content.
    Ensure you output ONLY a valid JSON object matching the schema below. Do not wrap in markdown block, just output raw JSON:
    {{
      "files": {{
        "relative/path/to/file1": "complete updated content...",
        "relative/path/to/file2": "complete updated content..."
      }}
    }}
    """
    
    try:
        raw_json_str = query_agent("proposer", prompt)
        
        # Clean up any potential markdown code blocks
        raw_json_str = raw_json_str.strip()
        if raw_json_str.startswith("```"):
            lines = raw_json_str.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw_json_str = "\n".join(lines).strip()
            
        parsed = json.loads(raw_json_str)
        return parsed.get("files", {})
    except Exception as e:
        logger.error(f"Failed to parse proposed changes using LLM: {e}")
        return {}

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
    
    # Inject active sandbox information if present
    from src.sandbox_session import get_active_sandbox, get_sandbox_modified_files
    try:
        active_sb = get_active_sandbox()
        if active_sb:
            status = active_sb.get("active_sandbox_status", "active")
            sandbox_info = f"--- Active Sandbox Session ---\n"
            sandbox_info += f"- Path: {active_sb['active_sandbox_path']}\n"
            sandbox_info += f"- Branch: {active_sb['active_sandbox_branch']}\n"
            sandbox_info += f"- Test Status: {status.upper()}\n"
            
            modified = get_sandbox_modified_files()
            if modified:
                sandbox_info += f"- Modified Files: {', '.join(modified)}\n"
            
            if status == "failed" and active_sb.get("active_sandbox_test_logs"):
                sandbox_info += f"- Last Sandbox Pytest Failures:\n{active_sb['active_sandbox_test_logs']}\n"
            
            semantic_context += sandbox_info + "\n"
    except Exception as sb_err:
        logger.error(f"Failed to inject sandbox session details into prompt: {sb_err}")
    
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

    # 4. Fetch recent conversation history from SQLite (user, persona, system, sandbox_automation)
    memories = get_recent_episodic_memories(limit=15)
    chat_history = []
    for speaker, msg, _ in reversed(memories):
        if speaker in ("user", "persona", "system", "sandbox_automation"):
            chat_history.append(f"{speaker.upper()}: {msg}")
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

            if user_msg.lower().startswith("/sandbox"):
                parts = user_msg.split()
                if len(parts) < 2:
                    print("\n[Error] Missing sandbox command. Usage: /sandbox [start | status | diff | ship | abort]\n")
                    continue
                
                cmd_type = parts[1].lower()
                from src.sandbox_session import (
                    get_active_sandbox, create_sandbox_session, run_sandbox_tests,
                    get_sandbox_diff, get_sandbox_modified_files, ship_sandbox_session,
                    abort_sandbox_session
                )
                
                if cmd_type == "start":
                    if len(parts) < 3:
                        print("\n[Error] Please specify a session name: /sandbox start <name>\n")
                        continue
                    session_name = parts[2]
                    active = get_active_sandbox()
                    if active:
                        print(f"\n[Warning] An active sandbox session already exists at '{active['active_sandbox_path']}' on branch '{active['active_sandbox_branch']}'.")
                        confirm_abort = await loop.run_in_executor(None, get_input, "Abort the existing session first? (y/n): ")
                        if confirm_abort.strip().lower() in ("y", "yes"):
                            abort_sandbox_session()
                            print("[Janus] Existing sandbox session aborted.")
                        else:
                            print("[Janus] Action canceled. Existing sandbox remains active.")
                            continue
                            
                    try:
                        path, branch = create_sandbox_session(session_name)
                        print(f"\n[✔] Sandbox session '{session_name}' successfully created!")
                        print(f"  * Workspace Path: {path}")
                        print(f"  * Git Branch: {branch}\n")
                        log_episodic_memory(
                            "sandbox_automation",
                            f"Sandbox session '{session_name}' initialized on branch '{branch}'. Sandbox path: '{path}'.",
                            "user_visible"
                        )
                    except Exception as err:
                        print(f"\n[Error] Failed to create sandbox session: {err}\n")
                        
                elif cmd_type == "status":
                    active = get_active_sandbox()
                    if not active:
                        print("\nJanus >> No active sandbox session. Start one with: /sandbox start <name>\n")
                    else:
                        print("\n" + "="*60)
                        print("[Janus] Active Sandbox Session Status")
                        print("="*60)
                        print(f"  * Path:   {active['active_sandbox_path']}")
                        print(f"  * Branch: {active['active_sandbox_branch']}")
                        print(f"  * Status: {active['active_sandbox_status'].upper()}")
                        
                        modified_files = get_sandbox_modified_files()
                        if modified_files:
                            print("  * Modified Files:")
                            for f in modified_files:
                                print(f"    - {f}")
                        else:
                            print("  * Modified Files: None")
                            
                        if active.get("active_sandbox_status") == "failed" and active.get("active_sandbox_test_logs"):
                            print("="*60)
                            print("LAST RUN FAILURES / TEST LOGS:")
                            print(active["active_sandbox_test_logs"])
                        print("="*60 + "\n")
                        
                elif cmd_type == "diff":
                    active = get_active_sandbox()
                    if not active:
                        print("\nJanus >> No active sandbox session.\n")
                    else:
                        diff = get_sandbox_diff()
                        print("\n" + "="*60)
                        print(f"[Janus] Cumulative Sandbox Diff ({active['active_sandbox_branch']})")
                        print("="*60)
                        if diff.strip():
                            print(diff)
                        else:
                            print("No changes in sandbox.")
                        print("="*60 + "\n")
                        
                elif cmd_type == "ship":
                    active = get_active_sandbox()
                    if not active:
                        print("\nJanus >> No active sandbox session.\n")
                        continue
                        
                    # First run tests to check compliance
                    print("\n[Janus] Running final validations inside the sandbox...")
                    passed, logs = await asyncio.to_thread(run_sandbox_tests)
                    status_str = "PASSED" if passed else "FAILED"
                    print(f"Sandbox test suite status: {status_str}")
                    if not passed:
                        print("="*60)
                        print("TEST LOGS:")
                        print(logs)
                        print("="*60)
                        print("\n[Warning] Sandbox tests failed. Shipping may introduce regressions.")
                        
                    confirm = await loop.run_in_executor(None, get_input, "Proceed to ship and apply sandbox changes to live workspace? (y/n): ")
                    if confirm.strip().lower() in ("y", "yes"):
                        try:
                            copied = ship_sandbox_session()
                            print(f"\n[✔] Sandbox successfully shipped and applied! Disposed of worktree.")
                            print("Modified files merged:")
                            for f in copied:
                                print(f"  - {f}")
                            log_episodic_memory(
                                "sandbox_automation",
                                f"Sandbox session branch '{active['active_sandbox_branch']}' successfully shipped and applied to active workspace. Files modified: {', '.join(copied)}.",
                                "user_visible"
                            )
                            print()
                            # Restart to load new code if files changed
                            if copied:
                                print("Restarting async daemon loop to load new code...\n")
                                return
                        except Exception as err:
                            print(f"\n[Error] Failed to ship sandbox changes: {err}\n")
                    else:
                        print("\nShipping canceled. Sandbox session remains active.\n")
                        
                elif cmd_type == "abort":
                    active = get_active_sandbox()
                    if not active:
                        print("\nJanus >> No active sandbox session.\n")
                        continue
                    confirm = await loop.run_in_executor(None, get_input, "Are you sure you want to abort? All sandbox changes will be lost permanently. (y/n): ")
                    if confirm.strip().lower() in ("y", "yes"):
                        abort_sandbox_session()
                        print("\n[✔] Sandbox session aborted and temporary workspace cleaned.\n")
                        log_episodic_memory(
                            "sandbox_automation",
                            f"Sandbox session branch '{active['active_sandbox_branch']}' aborted and cleaned up.",
                            "user_visible"
                        )
                    else:
                        print("\nAbort canceled.\n")
                        
                else:
                    print(f"\n[Error] Unknown sandbox command '{cmd_type}'. Options are: start, status, diff, ship, abort\n")
                continue

            if user_msg.lower().startswith("/stage"):
                parts = user_msg.split()
                limit = 1
                if len(parts) > 1:
                    try:
                        limit = int(parts[1])
                        if limit <= 0:
                            raise ValueError()
                    except ValueError:
                        print("\n[Error] Invalid lookback count. Usage: /stage [count] (must be positive integer)\n")
                        continue
                
                last_msg = get_recent_persona_messages(limit)
                if not last_msg:
                    print("\nJanus >> No previous message found to stage changes from.\n")
                    continue
                
                if limit == 1:
                    print("\n[Janus] Parsing proposed changes from our last message...")
                else:
                    print(f"\n[Janus] Parsing proposed changes from our last {limit} messages...")
                proposed_mods = parse_proposed_changes(last_msg)
                
                if not proposed_mods:
                    print("\nJanus >> No proposed code changes or file paths could be parsed from the messages.\n")
                    continue
                
                approved_files = set()
                
                # Keep loop for selection/editing
                while True:
                    print("\n" + "="*60)
                    print("[Janus] Proposed Modifications:")
                    print("="*60)
                    mod_files = list(proposed_mods.keys())
                    for idx, file in enumerate(mod_files, start=1):
                        is_new = not (src.config.ROOT_DIR / file).exists()
                        status_str = "New File" if is_new else "Modified File"
                        print(f"  {idx}. {file} ({status_str})")
                    print("="*60)
                    print("Options:")
                    print("  - Enter 'y' to stage and run tests for all of the above.")
                    print("  - Enter 'n' to cancel/abort.")
                    print("  - Enter 'remove <number>' to exclude a file.")
                    print("  - Enter 'edit <number> | <new instructions>' to regenerate the changes for that file.")
                    print("="*60)
                    
                    selection = await loop.run_in_executor(None, get_input, "Selection >> ")
                    selection = selection.strip()
                    
                    if not selection:
                        continue
                    
                    if selection.lower() in ("n", "no", "cancel"):
                        print("\nStaging canceled.\n")
                        break
                    
                    elif selection.lower() in ("y", "yes"):
                        if not proposed_mods:
                            print("\nNo files are left to stage. Canceled.\n")
                            break
                        
                        # Step 1: Critic Audits
                        print("\n[Janus] Submitting changes to Critic agent for constitutional audit...")
                        vetoed_files = {}
                        
                        audit_tasks = []
                        for file_path, proposed_code in proposed_mods.items():
                            if file_path in approved_files:
                                print(f"✔ [Audit Cached] Critic already approved '{file_path}'. Skipping audit.")
                                continue
                            
                            audit_prompt = f"""
                            You are the Critic. Audit the proposed code modification to '{file_path}' against our core constitution:
                            
                            PROPOSED CODE MODIFICATION:
                            {proposed_code}
                            
                            Perform a strict audit. Evaluate the systemic utility of this change and determine if it violates any rules in the core constitution (e.g., security, imports, loop caps, system stability).
                            Output your decision exactly in one of these formats:
                            CRITIC_DECISION: APPROVED | Justification: [Your reasoning]
                            CRITIC_DECISION: VETOED | Justification: [Your reasoning]
                            """
                            audit_tasks.append((file_path, proposed_code, audit_prompt))
                            
                        # Run audits concurrently in background threads
                        if audit_tasks:
                            audit_results = await asyncio.gather(*[
                                asyncio.to_thread(query_agent, "critic", item[2])
                                for item in audit_tasks
                            ])
                        else:
                            audit_results = []
                        
                        for (file_path, proposed_code, _), critic_resp in zip(audit_tasks, audit_results):
                            try:
                                critic_decision = 1
                                critic_justification = "Automatic approval"
                                decision_match = re.search(r"CRITIC_DECISION:\s*(APPROVED|VETOED)", critic_resp, re.IGNORECASE)
                                justification_match = re.search(r"Justification:\s*(.*)", critic_resp, re.IGNORECASE)
                                
                                if decision_match:
                                    decision_str = decision_match.group(1).upper()
                                    if decision_str == "VETOED":
                                        critic_decision = 0
                                        
                                if justification_match:
                                    critic_justification = justification_match.group(1).strip()
                                    
                                log_deliberation(
                                    proposed_action=f"modify_code_multi: {file_path}",
                                    debate_json={"proposer_output": proposed_code, "critic_output": critic_resp},
                                    critic_decision=critic_decision,
                                    utility_score=1.0 if critic_decision == 1 else 0.0,
                                    justification=critic_justification
                                )
                                
                                if critic_decision == 0:
                                    print(f"\n❌ [Audit Vetoed] Critic rejected changes for '{file_path}':\n{critic_justification}\n")
                                    vetoed_files[file_path] = critic_justification
                                    if file_path in approved_files:
                                        approved_files.remove(file_path)
                                else:
                                    print(f"✔ [Audit Approved] Critic approved '{file_path}': {critic_justification}")
                                    approved_files.add(file_path)
                            except Exception as audit_err:
                                print(f"\n[Janus] Error processing audit for '{file_path}': {audit_err}\n")
                                vetoed_files[file_path] = f"Audit failed: {audit_err}"
                                if file_path in approved_files:
                                    approved_files.remove(file_path)
                        
                        if vetoed_files:
                            print("\n" + "="*60)
                            print("⚠️  Some files were vetoed by the Critic.")
                            print("="*60)
                            auto_refine_input = await loop.run_in_executor(
                                None, get_input, 
                                "Would you like to automatically refine these files using the Critic's feedback? (y/n): "
                            )
                            if auto_refine_input.strip().lower() in ("y", "yes"):
                                refine_tasks = []
                                for file_path, reason in vetoed_files.items():
                                    print(f"\n[Janus] Preparing automatic refinement for '{file_path}'...")
                                    
                                    # Read current file contents to assist proposer
                                    from pathlib import Path
                                    full_path = src.config.ROOT_DIR / file_path
                                    current_content = ""
                                    if full_path.exists():
                                        try:
                                            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                                                current_content = f.read()
                                        except Exception:
                                            pass
                                            
                                    draft_prompt = f"""
                                    You are the Proposer. The user has requested a codebase modification for a specific file during multi-file staging.
                                    The Critic has vetoed the previous draft with the following feedback:
                                    
                                    VETO REASON / FEEDBACK:
                                    {reason}
                                    
                                    FILE TO MODIFY: {file_path}
                                    
                                    CURRENT FILE CONTENT:
                                    {current_content if current_content else "(File is new or empty)"}
                                    
                                    Generate the COMPLETE updated source code for the file '{file_path}' addressing all feedback.
                                    
                                    CRITICAL RULES:
                                    1. Output ONLY the raw source code of the file.
                                    2. Do NOT wrap the output in markdown code blocks (e.g., do not use ```python or ```).
                                    3. Do NOT include any introductory or concluding conversational text.
                                    4. Ensure the code compiles, passes unit tests, and satisfies all guidelines.
                                    """
                                    refine_tasks.append((file_path, draft_prompt))
                                
                                # Run proposer regenerations concurrently in background threads
                                print(f"\n[Janus] Querying Proposer agents concurrently for {len(refine_tasks)} files...")
                                refine_results = await asyncio.gather(*[
                                    asyncio.to_thread(query_agent, "proposer", item[1])
                                    for item in refine_tasks
                                ])
                                
                                for (file_path, _), proposed_code in zip(refine_tasks, refine_results):
                                    try:
                                        if proposed_code.strip().startswith("```"):
                                            lines = proposed_code.strip().splitlines()
                                            if lines[0].startswith("```"):
                                                lines = lines[1:]
                                            if lines and lines[-1].strip() == "```":
                                                lines = lines[:-1]
                                            proposed_code = "\n".join(lines) + "\n"
                                        proposed_mods[file_path] = proposed_code
                                        if file_path in approved_files:
                                            approved_files.remove(file_path)
                                        print(f"✔ [Janus] Successfully regenerated '{file_path}'.")
                                    except Exception as draft_err:
                                        print(f"\n[Janus] Error regenerating code for '{file_path}': {draft_err}\n")
                            continue
                            
                        # Step 2: Stage and Test Multi
                        from src.self_modification import stage_and_test_multi, generate_multi_diff, apply_staged_multi
                        import shutil
                        print(f"\n[Janus] Creating staging workspace and running tests for all modified files...")
                        try:
                            diff = generate_multi_diff(proposed_mods)
                            passed, logs, temp_dir = stage_and_test_multi(proposed_mods)
                        except Exception as stage_err:
                            print(f"\n[Janus] Error staging changes: {stage_err}\n")
                            break
                            
                        # Step 3: Present combined results
                        print("\n" + "="*60)
                        print(f"⚠️  Staged Chat Modifications for multiple files:")
                        for file in proposed_mods:
                            print(f"  - {file}")
                        print(f"Staged unit tests status: {'PASSED' if passed else 'FAILED'}")
                        print("="*60)
                        print("DIFF:")
                        print(diff)
                        print("="*60)
                        if not passed:
                            print("TEST RUN FAILURE LOGS:")
                            print(logs)
                            print("="*60)
                            
                            # Log Parsing for test failures
                            failing_tests = []
                            for match in re.findall(r"(?:FAILED|ERROR)\s+(tests/test_[a-zA-Z0-9_-]+\.py)|(tests/test_[a-zA-Z0-9_-]+\.py)::\S+\s+(?:FAILED|ERROR)", logs):
                                failing_tests.append(match[0] or match[1])
                            failing_tests = sorted(list(set(failing_tests)))
                            if failing_tests:
                                print("Failing test file(s) detected:")
                                for f in failing_tests:
                                    print(f"  - {f}")
                                print("="*60)
                                
                                heal_input = await loop.run_in_executor(
                                    None, get_input,
                                    "Pre-existing test file(s) failed in staging. Would you like Janus to attempt self-healing? (y/n): "
                                )
                                if heal_input.strip().lower() in ("y", "yes"):
                                    heal_tasks = []
                                    for test_file in failing_tests:
                                        print(f"\n[Janus] Preparing self-healing for '{test_file}'...")
                                        full_path = src.config.ROOT_DIR / test_file
                                        current_content = ""
                                        if full_path.exists():
                                            try:
                                                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                                                    current_content = f.read()
                                            except Exception:
                                                pass
                                                
                                        draft_prompt = f"""
                                        You are the Proposer. A pre-existing test file has failed during staging.
                                        We need to automatically fix (self-heal) this test file so it passes correctly.
                                        
                                        FAILING TEST FILE: {test_file}
                                        
                                        TEST RUN FAILURE LOGS:
                                        {logs}
                                        
                                        CURRENT TEST FILE CONTENT:
                                        {current_content}
                                        
                                        Please fix this test file to resolve the failures shown in the logs.
                                        Follow mocking best practices (e.g. mock where imported/used, not where defined).
                                        
                                        Generate the COMPLETE updated source code for the test file '{test_file}'.
                                        
                                        CRITICAL RULES:
                                        1. Output ONLY the raw source code of the file.
                                        2. Do NOT wrap the output in markdown code blocks (e.g., do not use ```python or ```).
                                        3. Do NOT include any introductory or concluding conversational text.
                                        4. Ensure the code compiles, passes unit tests, and satisfies all guidelines.
                                        """
                                        heal_tasks.append((test_file, draft_prompt))
                                    
                                    print(f"\n[Janus] Querying Proposer agents concurrently to self-heal {len(heal_tasks)} files...")
                                    heal_results = await asyncio.gather(*[
                                        asyncio.to_thread(query_agent, "proposer", item[1])
                                        for item in heal_tasks
                                    ])
                                    
                                    for (test_file, _), proposed_code in zip(heal_tasks, heal_results):
                                        try:
                                            if proposed_code.strip().startswith("```"):
                                                lines = proposed_code.strip().splitlines()
                                                if lines[0].startswith("```"):
                                                    lines = lines[1:]
                                                if lines and lines[-1].strip() == "```":
                                                    lines = lines[:-1]
                                                proposed_code = "\n".join(lines) + "\n"
                                            proposed_mods[test_file] = proposed_code
                                            if test_file in approved_files:
                                                approved_files.remove(test_file)
                                            print(f"✔ [Janus] Successfully self-healed '{test_file}'.")
                                        except Exception as draft_err:
                                            print(f"\n[Janus] Error self-healing '{test_file}': {draft_err}\n")
                                            
                                    try:
                                        shutil.rmtree(temp_dir)
                                    except Exception:
                                        pass
                                    continue
                            
                        confirm_input = await loop.run_in_executor(None, get_input, "Approve and commit these changes? (y/n): ")
                        confirm_clean = confirm_input.strip().lower()
                        
                        if confirm_clean in ("y", "yes"):
                            try:
                                apply_staged_multi(temp_dir, proposed_mods)
                                print(f"\n[✔] Staged modifications applied to active codebase.")
                                for file in proposed_mods:
                                    log_episodic_memory("system", f"User approved staged multi-file self-modification for '{file}'.", "user_visible")
                                try:
                                    shutil.rmtree(temp_dir)
                                except Exception:
                                    pass
                                print("\nRestarting async daemon loop to load new code...\n")
                                return
                            except Exception as err:
                                print(f"\nError applying staged modifications: {err}\n")
                        else:
                            print("\nSelf-modifications aborted and staging directory cleaned.\n")
                            for file in proposed_mods:
                                log_episodic_memory("system", f"User rejected staged multi-file self-modification for '{file}'.", "user_visible")
                            try:
                                shutil.rmtree(temp_dir)
                            except Exception:
                                pass
                        break
                        
                    elif selection.lower().startswith("remove "):
                        match = re.match(r"^remove\s+(\d+)", selection, re.IGNORECASE)
                        if match:
                            idx = int(match.group(1)) - 1
                            if 0 <= idx < len(mod_files):
                                removed_file = mod_files[idx]
                                del proposed_mods[removed_file]
                                if removed_file in approved_files:
                                    approved_files.remove(removed_file)
                                print(f"\n[Janus] Excluded '{removed_file}' from staging list.")
                            else:
                                print("\n[Error] Invalid index.\n")
                        else:
                            print("\n[Error] Invalid remove syntax. Use: remove <number>\n")
                            
                    elif selection.lower().startswith("edit "):
                        match = re.match(r"^edit\s+(\d+)(?:\s*\|\s*(.*))?", selection, re.IGNORECASE)
                        if match:
                            idx = int(match.group(1)) - 1
                            edit_inst = match.group(2).strip() if match.group(2) else None
                            if 0 <= idx < len(mod_files):
                                target_file = mod_files[idx]
                                
                                if not edit_inst:
                                    # Fetch last veto from SQLite
                                    conn = get_connection(read_only_constitution=True)
                                    cursor = conn.cursor()
                                    cursor.execute("""
                                    SELECT justification FROM internal_deliberations 
                                    WHERE proposed_action LIKE ? AND critic_decision = 0 
                                    ORDER BY id DESC LIMIT 1;
                                    """, (f"%{target_file}%",))
                                    row = cursor.fetchone()
                                    conn.close()
                                    
                                    if row:
                                        edit_inst = f"Fix the issues raised by the Critic: {row[0]}"
                                        print(f"\n[Janus] Automatically refining '{target_file}' using Critic's feedback:")
                                        print(f"  > {row[0]}")
                                    else:
                                        print(f"\n[Error] No prior Critic veto justification found for '{target_file}'. Please specify instructions using: edit <number> | <instructions>\n")
                                        continue
                                
                                print(f"\n[Janus] Regenerating '{target_file}' changes based on instructions: '{edit_inst}'...")
                                
                                # Read current file contents to assist proposer
                                from pathlib import Path
                                full_path = src.config.ROOT_DIR / target_file
                                current_content = ""
                                if full_path.exists():
                                    try:
                                        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                                            current_content = f.read()
                                    except Exception:
                                        pass
                                
                                draft_prompt = f"""
                                You are the Proposer. The user has requested a codebase modification for a specific file during multi-file staging:
                                
                                FILE TO MODIFY: {target_file}
                                USER INSTRUCTIONS: {edit_inst}
                                
                                CURRENT FILE CONTENT:
                                {current_content if current_content else "(File is new or empty)"}
                                
                                Generate the COMPLETE updated source code for the file '{target_file}'.
                                
                                CRITICAL RULES:
                                1. Output ONLY the raw source code of the file.
                                2. Do NOT wrap the output in markdown code blocks (e.g., do not use ```python or ```).
                                3. Do NOT include any introductory or concluding conversational text.
                                4. Ensure the code compiles, passes unit tests, and satisfies the user's instructions.
                                """
                                try:
                                    proposed_code = query_agent("proposer", draft_prompt)
                                    if proposed_code.strip().startswith("```"):
                                        lines = proposed_code.strip().splitlines()
                                        if lines[0].startswith("```"):
                                            lines = lines[1:]
                                        if lines and lines[-1].strip() == "```":
                                            lines = lines[:-1]
                                        proposed_code = "\n".join(lines) + "\n"
                                    proposed_mods[target_file] = proposed_code
                                    if target_file in approved_files:
                                        approved_files.remove(target_file)
                                    print(f"✔ [Janus] Successfully regenerated '{target_file}'.")
                                except Exception as draft_err:
                                    print(f"\n[Janus] Error regenerating code: {draft_err}\n")
                            else:
                                print("\n[Error] Invalid index.\n")
                        else:
                            print("\n[Error] Invalid edit syntax. Use: edit <number> or edit <number> | <instructions>\n")
                    else:
                        print("\n[Error] Unknown selection. Please use: 'y', 'n', 'remove <number>', or 'edit <number> | <instructions>'.\n")
                continue

            # Intercept for synchronous self-modification requests (Option C)
            file_path, instructions = detect_modification_intent(user_msg)
            if file_path == "INVALID":
                print("\nJanus >> Invalid format. Please use: /modify <relative_file_path> | <instructions>\n")
                continue
            elif file_path:
                from src.self_modification import stage_and_test, generate_diff, apply_staged_change
                import shutil
                
                print(f"\n[Janus] Processing code modification request for '{file_path}'...")
                
                # Fetch current file content to help Proposer generate full code
                from pathlib import Path
                full_path = src.config.ROOT_DIR / file_path
                current_content = ""
                if full_path.exists():
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                            current_content = f.read()
                    except Exception:
                        pass
                
                # Step 1: Query Proposer to draft the new code content
                print("[Janus] Querying Proposer agent to draft modifications...")
                draft_prompt = f"""
                You are the Proposer. The user has requested a codebase modification:
                
                FILE TO MODIFY: {file_path}
                USER INSTRUCTIONS: {instructions}
                
                CURRENT FILE CONTENT:
                {current_content if current_content else "(File is new or empty)"}
                
                Generate the COMPLETE updated source code for the file '{file_path}'.
                
                CRITICAL RULES:
                1. Output ONLY the raw source code of the file.
                2. Do NOT wrap the output in markdown code blocks (e.g., do not use ```python or ```).
                3. Do NOT include any introductory or concluding conversational text.
                4. Ensure the code compiles, passes unit tests, and satisfies the user's instructions.
                """
                
                try:
                    proposed_code = query_agent("proposer", draft_prompt)
                    # Clean up code blocks if LLM still outputted them
                    if proposed_code.strip().startswith("```"):
                        lines = proposed_code.strip().splitlines()
                        if lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines and lines[-1].strip() == "```":
                            lines = lines[:-1]
                        proposed_code = "\n".join(lines) + "\n"
                except Exception as draft_err:
                    print(f"\n[Janus] Error generating code: {draft_err}\n")
                    continue
                
                # Step 2: Query Critic to audit the change
                print("[Janus] Submitting changes to Critic agent for constitutional audit...")
                audit_prompt = f"""
                You are the Critic. Audit the proposed code modification to '{file_path}' against our core constitution:
                
                PROPOSED CODE MODIFICATION:
                {proposed_code}
                
                USER INSTRUCTIONS:
                {instructions}
                
                Perform a strict audit. Evaluate the systemic utility of this change and determine if it violates any rules in the core constitution (e.g., security, imports, loop caps, system stability).
                Output your decision exactly in one of these formats:
                CRITIC_DECISION: APPROVED | Justification: [Your reasoning]
                CRITIC_DECISION: VETOED | Justification: [Your reasoning]
                """
                
                try:
                    critic_resp = query_agent("critic", audit_prompt)
                    
                    critic_decision = 1
                    critic_justification = "Automatic approval"
                    decision_match = re.search(r"CRITIC_DECISION:\s*(APPROVED|VETOED)", critic_resp, re.IGNORECASE)
                    justification_match = re.search(r"Justification:\s*(.*)", critic_resp, re.IGNORECASE)
                    
                    if decision_match:
                        decision_str = decision_match.group(1).upper()
                        if decision_str == "VETOED":
                            critic_decision = 0
                            
                    if justification_match:
                        critic_justification = justification_match.group(1).strip()
                        
                    # Log the deliberation to SQLite
                    log_deliberation(
                        proposed_action=f"modify_code: {file_path}",
                        debate_json={"proposer_output": proposed_code, "critic_output": critic_resp},
                        critic_decision=critic_decision,
                        utility_score=1.0 if critic_decision == 1 else 0.0,
                        justification=critic_justification
                    )
                    
                    if critic_decision == 0:
                        print(f"\n❌ [Audit Vetoed] Critic rejected the change:\n{critic_justification}\n")
                        continue
                    else:
                        print(f"✔ [Audit Approved] Critic approved the change: {critic_justification}")
                        
                except Exception as audit_err:
                    print(f"\n[Janus] Error auditing code changes: {audit_err}\n")
                    continue
                
                # Step 3: Stage and Run Tests
                print(f"[Janus] Creating staging workspace and running tests...")
                try:
                    diff = generate_diff(file_path, proposed_code)
                    passed, logs, temp_dir = stage_and_test(file_path, proposed_code)
                except Exception as stage_err:
                    print(f"\n[Janus] Error staging changes: {stage_err}\n")
                    continue
                
                # Step 4: Display gate and prompt
                print("\n" + "="*60)
                print(f"⚠️  Staged Chat Modification for: {file_path}")
                print(f"Staged unit tests status: {'PASSED' if passed else 'FAILED'}")
                print("="*60)
                print("DIFF:")
                print(diff)
                print("="*60)
                if not passed:
                    print("TEST RUN FAILURE LOGS:")
                    print(logs)
                    print("="*60)
                
                confirm_input = await loop.run_in_executor(None, get_input, "Approve and commit this change? (y/n): ")
                confirm_clean = confirm_input.strip().lower()
                
                if confirm_clean in ("y", "yes"):
                    try:
                        apply_staged_change(temp_dir, file_path)
                        print(f"\n[✔] Staged modifications applied to '{file_path}'.")
                        log_episodic_memory("system", f"User approved staged chat self-modification for '{file_path}'.", "user_visible")
                        try:
                            shutil.rmtree(temp_dir)
                        except Exception:
                            pass
                        print("\nRestarting async daemon loop to load new code...\n")
                        break
                    except Exception as err:
                        print(f"\nError applying staged modification: {err}\n")
                else:
                    print("\nSelf-modification aborted and staging directory cleaned.\n")
                    log_episodic_memory("system", f"User rejected staged chat self-modification for '{file_path}'.", "user_visible")
                    try:
                        shutil.rmtree(temp_dir)
                    except Exception:
                        pass
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

            # Check if there is an active sandbox session. If so, parse and apply changes in the background!
            from src.sandbox_session import get_active_sandbox, apply_changes_to_sandbox, run_sandbox_tests
            active_sb = get_active_sandbox()
            if active_sb:
                async def parse_and_apply_to_sandbox_async(msg_content):
                    try:
                        proposed = await asyncio.to_thread(parse_proposed_changes, msg_content)
                        if proposed:
                            print(f"\n[Janus Daemon] Extracted proposed modifications for {len(proposed)} file(s).")
                            print("Applying changes to sandbox in background...")
                            apply_changes_to_sandbox(proposed)
                            print("[Janus Daemon] Sandbox files updated. Executing unit tests in sandbox...")
                            passed, logs = await asyncio.to_thread(run_sandbox_tests)
                            status_str = "PASSED" if passed else "FAILED"
                            print(f"\n[Janus Daemon] Sandbox unit tests: {status_str}")
                            
                            # Log success or failure back to Janus as sandbox automation
                            if passed:
                                log_episodic_memory(
                                    "sandbox_automation",
                                    f"Sandbox testing completed successfully for branch '{active_sb['active_sandbox_branch']}'. All tests passed.",
                                    "user_visible"
                                )
                            else:
                                log_episodic_memory(
                                    "sandbox_automation",
                                    f"Sandbox testing FAILED for branch '{active_sb['active_sandbox_branch']}'. Errors/Logs:\n{logs}",
                                    "user_visible"
                                )
                                
                            if not passed:
                                print("  (Tip: Run '/sandbox status' or '/sandbox ship' to view test failure logs)")
                    except Exception as e:
                        logger.error(f"Error auto-applying to sandbox: {e}")
                
                asyncio.create_task(parse_and_apply_to_sandbox_async(response))

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in persona chat: {e}", exc_info=True)
            print(f"\nJanus >> (Error communicating with internal swarm: {e})\n")


