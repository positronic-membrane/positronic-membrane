import re
import asyncio
import logging
import src.config
from src.llm import query_agent, query_agent_stream
from src.memory import query_memories
from src.database import (
    get_connection,
    log_episodic_memory,
    get_recent_episodic_memories,
    log_deliberation
)

logger = logging.getLogger("JanusPersona")

from typing import Optional

def get_session_party_id() -> Optional[str]:
    from src.database import get_connection
    conn = get_connection(read_only_constitution=True)
    try:
        # Try to find an admin first, then contributor, then user
        for role in ('admin', 'contributor', 'user'):
            row = conn.execute("SELECT id FROM parties WHERE role = ? LIMIT 1;", (role,)).fetchone()
            if row:
                return row[0]
        # Fallback to any party
        row = conn.execute("SELECT id FROM parties LIMIT 1;").fetchone()
        if row:
            return row[0]
    except Exception:
        pass
    finally:
        conn.close()
    return None

def handle_skills_command() -> str:
    from src.database import get_connection
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT skill_id, name, description, required_role, trigger_type FROM agent_skills WHERE is_active = 1;"
        )
        rows = cursor.fetchall()
    except Exception as e:
        return f"[Error] Failed to fetch skills: {e}"
    finally:
        conn.close()
        
    if not rows:
        return "No active skills registered."
        
    output = ["### Active Swarm Skills\n"]
    output.append("| Skill ID | Name | Description | Required Role | Trigger Type |")
    output.append("| --- | --- | --- | --- | --- |")
    for skill_id, name, desc, role, trigger in rows:
        output.append(f"| `{skill_id}` | {name} | {desc} | `{role}` | `{trigger}` |")
    return "\n".join(output)

def handle_runskill_command(command_str: str) -> str:
    import json
    import re
    # Command format: /runskill <skill_id> [arguments_json]
    match = re.match(r"^/runskill\s+([a-zA-Z0-9_-]+)(?:\s+(.*))?", command_str, re.DOTALL)
    if not match:
        return "[Error] Usage: /runskill <skill_id> [arguments_json]"
        
    skill_id = match.group(1).strip()
    args_str = (match.group(2) or "").strip()
    
    args = {}
    if args_str:
        try:
            args = json.loads(args_str)
            if not isinstance(args, dict):
                return "[Error] Arguments must be a JSON object."
        except Exception as e:
            return f"[Error] Invalid JSON arguments: {e}"
            
    # Resolve current session party ID
    party_id = get_session_party_id()
    
    from src.skills import DynamicSkillExecutor
    res = DynamicSkillExecutor.execute(skill_id, args, party_id=party_id)
    if res["success"]:
        skill_res = res["result"]
        if isinstance(skill_res, str):
            return skill_res
        return json.dumps(skill_res, indent=2)
    else:
        return f"[Error] Skill execution failed: {res['error']}"

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
    Supports search-and-replace blocks using <<<<<<< SEARCH / ======= / >>>>>>> REPLACE syntax.
    """
    import json

    # --- Guardrail B: Deterministic Regex Parser ---
    # Look for files matching 'Path: relative/path' followed by code fences:
    regex_proposed = {}
    pattern = r"(?:[Pp]ath|[Ff]ile|[Ff]ilename):\s*([a-zA-Z0-9_/.-]+)\s*\n+```[a-zA-Z0-9-]*\n(.*?)\n```"
    matches = re.findall(pattern, message_content, re.DOTALL)
    from src.config import get_effective_workspace_root
    workspace_root = get_effective_workspace_root()
    for path, content in matches:
        path = path.strip().rstrip(".,;!?`\"'")
        # Ensure path is relative and doesn't do directory traversal
        try:
            full_path = workspace_root / path
            full_path.relative_to(workspace_root)
            
            # Apply search-and-replace if blocks exist
            if "<<<<<<< SEARCH" in content and ">>>>>>> REPLACE" in content:
                current_content = ""
                if full_path.exists():
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                            current_content = f.read()
                    except Exception:
                        pass
                from src.self_modification import apply_search_replace_blocks
                content = apply_search_replace_blocks(current_content, content)
                
            regex_proposed[path] = content.strip()
        except ValueError:
            pass
            
    if regex_proposed:
        logger.info(f"Deterministic parser successfully extracted {len(regex_proposed)} file(s).")
        return regex_proposed
    
    # Scan message for relative file paths
    paths = re.findall(
        r"\b((?:src|tests)/[a-zA-Z0-9_/.-]+|[a-zA-Z0-9_/.-]+\.md|[a-zA-Z0-9_/.-]+\.json|requirements\.txt)\b",
        message_content
    )
    
    unique_paths = set()
    from src.config import get_effective_workspace_root
    workspace_root = get_effective_workspace_root()
    for p in paths:
        p = p.rstrip(".,;!?`\"'")
        # Ensure path is relative and doesn't do directory traversal
        try:
            full_path = workspace_root / p
            full_path.relative_to(workspace_root)
            unique_paths.add(p)
        except ValueError:
            pass
            
    current_files = {}
    for p in unique_paths:
        full_path = workspace_root / p
        if full_path.is_file():
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    current_files[p] = f.read()
            except Exception:
                pass
                
    current_files_json = json.dumps(current_files, indent=2)
    prompt = f"""
    You are a precise parsing agent. Analyze the following message from Janus, which proposes file changes (either as new files, full code blocks, or search-and-replace blocks).
    Your task is to output the COMPLETE, updated content for every file proposed to be created or modified.

    We have provided the current live content of the files mentioned in the message below for your reference (to apply diffs, search-and-replace blocks, or modifications).

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
        files = parsed.get("files", {})
        
        # Apply search-and-replace post-processing to any JSON keys if they contained block markers
        for path in list(files.keys()):
            content = files[path]
            if "<<<<<<< SEARCH" in content and ">>>>>>> REPLACE" in content:
                full_path = workspace_root / path
                current_content = ""
                if full_path.exists():
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                            current_content = f.read()
                    except Exception:
                        pass
                from src.self_modification import apply_search_replace_blocks
                try:
                    files[path] = apply_search_replace_blocks(current_content, content)
                except ValueError as e:
                    logger.error(f"Failed to apply search/replace block in LLM JSON output for {path}: {e}")
                    del files[path]
                    
        return files
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
            "you can update my core constitution using the command: /amend <rule_key> | <rule_text> or delete a rule with /repeal <rule_key>\n"
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

def get_self_model_prompt_guidelines() -> str:
    from src.database import get_connection
    conn = get_connection(read_only_constitution=True)
    traits = {}
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT trait_name, value FROM self_model;")
        rows = cursor.fetchall()
        for row in rows:
            try:
                name = row['trait_name']
                val = float(row['value'])
            except (TypeError, IndexError, KeyError):
                name = row[0]
                val = float(row[1])
            traits[name] = val
    except Exception as e:
        logger.error(f"Failed to query self-model traits: {e}")
    finally:
        conn.close()

    if not traits:
        return ""

    guidelines = []
    
    # Verbosity guidelines
    v_val = traits.get("verbosity", 0.5)
    if v_val < 0.3:
        guidelines.append("- Style instructions: Be extremely concise, brief, and to the point. Keep responses under 2-3 sentences. Avoid extra details.")
    elif v_val > 0.7:
        guidelines.append("- Style instructions: Be highly verbose, comprehensive, and detailed. Provide complete context, code snippets if helpful, and thorough explanations.")
    else:
        guidelines.append("- Style instructions: Keep responses moderately concise, clear, and balanced in length.")

    # Curiosity guidelines
    cur_val = traits.get("curiosity", 0.5)
    if cur_val > 0.7:
        guidelines.append("- Persona instructions: Actively demonstrate curiosity. Propose next steps, future exploration ideas, or ask probing questions.")
    elif cur_val < 0.3:
        guidelines.append("- Persona instructions: Answer directly and stick only to the requested topic without recommending unrelated research.")

    # Cautiousness guidelines
    caut_val = traits.get("cautiousness", 0.5)
    if caut_val > 0.7:
        guidelines.append("- Persona instructions: Emphasize security, verification, thorough testing, and risk auditing in your response.")
    elif caut_val < 0.3:
        guidelines.append("- Persona instructions: Prioritize direct action, efficiency, and speed. Avoid defensive coding disclaimers.")

    return "\n".join(guidelines)

def handle_self_command() -> str:
    from src.database import get_connection
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT trait_name, value, confidence, is_pinned FROM self_model;")
        traits = cursor.fetchall()
        
        cursor.execute("""
            SELECT trait_name, old_value, new_value, old_confidence, new_confidence, reason, changed_at 
            FROM self_model_history 
            ORDER BY id DESC LIMIT 5;
        """)
        history = cursor.fetchall()
    except Exception as e:
        return f"[Error] Failed to fetch self-model state: {e}"
    finally:
        conn.close()

    output = ["### 🧠 Janus Self-Model & Personality Traits\n"]
    for row in traits:
        try:
            name = row['trait_name']
            val = float(row['value'])
            conf = float(row['confidence'])
            pinned = int(row['is_pinned'])
        except (TypeError, IndexError, KeyError):
            name = row[0]
            val = float(row[1])
            conf = float(row[2])
            pinned = int(row[3])
            
        filled = int(round(val * 10))
        filled = max(0, min(10, filled))
        bar = "█" * filled + "░" * (10 - filled)
        status = "pinned 🔒" if pinned else "dynamic ⏳"
        output.append(f"- **{name.capitalize()}**: `[{bar}]` **{val:.2f}** (confidence: {conf:.2f}, mode: {status})")

    if history:
        output.append("\n### 📜 Recent Trait Drift & Modification History\n")
        output.append("| Timestamp | Trait | Drift/Change | Confidence Change | Reason |")
        output.append("| --- | --- | --- | --- | --- |")
        for h in history:
            try:
                tname = h['trait_name']
                old_v = h['old_value']
                new_v = h['new_value']
                old_c = h['old_confidence']
                new_c = h['new_confidence']
                reason = h['reason']
                ts = h['changed_at']
            except (TypeError, IndexError, KeyError):
                tname = h[0]
                old_v = h[1]
                new_v = h[2]
                old_c = h[3]
                new_c = h[4]
                reason = h[5]
                ts = h[6]
            
            # Format value change
            if old_v is not None:
                v_change = f"{old_v:.2f} ➔ {new_v:.2f}"
            else:
                v_change = f"{new_v:.2f}"
                
            if old_c is not None:
                c_change = f"{old_c:.2f} ➔ {new_c:.2f}"
            else:
                c_change = f"{new_c:.2f}"
                
            output.append(f"| {ts} | **{tname}** | {v_change} | {c_change} | {reason} |")
    else:
        output.append("\nNo trait modifications or decay cycles recorded yet.")

    return "\n".join(output)

def handle_pin_command(command_str: str) -> str:
    match = re.match(r"^/pin\s+([a-zA-Z0-9_-]+)\s+([0-9.]+)", command_str)
    if not match:
        return "[Error] Usage: /pin <trait> <value>"
    
    trait = match.group(1).strip().lower()
    try:
        val = float(match.group(2))
    except ValueError:
        return "[Error] Value must be a valid float between 0.0 and 1.0."
        
    if not (0.0 <= val <= 1.0):
        return "[Error] Value must be between 0.0 and 1.0."
        
    from src.database import get_connection
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value, confidence FROM self_model WHERE trait_name = ?;", (trait,))
        row = cursor.fetchone()
        if not row:
            return f"[Error] Trait '{trait}' not found in self-model."
            
        try:
            old_v = row['value']
            old_c = row['confidence']
        except (TypeError, IndexError, KeyError):
            old_v = row[0]
            old_c = row[1]
            
        new_c = 1.0
        cursor.execute(
            "UPDATE self_model SET value = ?, confidence = ?, is_pinned = 1, updated_at = CURRENT_TIMESTAMP WHERE trait_name = ?;",
            (val, new_c, trait)
        )
        cursor.execute(
            "INSERT INTO self_model_history (trait_name, old_value, new_value, old_confidence, new_confidence, reason) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (trait, float(old_v), val, float(old_c), new_c, "Manual user pin override")
        )
        conn.commit()
        return f"[✔] Trait '{trait}' pinned at value {val:.2f} (confidence: {new_c:.2f})."
    except Exception as e:
        return f"[Error] Failed to pin trait: {e}"
    finally:
        conn.close()

def handle_unpin_command(command_str: str) -> str:
    match = re.match(r"^/unpin\s+([a-zA-Z0-9_-]+)", command_str)
    if not match:
        return "[Error] Usage: /unpin <trait>"
        
    trait = match.group(1).strip().lower()
    
    from src.database import get_connection
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value, confidence, is_pinned FROM self_model WHERE trait_name = ?;", (trait,))
        row = cursor.fetchone()
        if not row:
            return f"[Error] Trait '{trait}' not found in self-model."
            
        try:
            old_v = row['value']
            old_c = row['confidence']
            pinned = row['is_pinned']
        except (TypeError, IndexError, KeyError):
            old_v = row[0]
            old_c = row[1]
            pinned = row[2]
            
        if not pinned:
            return f"Trait '{trait}' is already unpinned."
            
        cursor.execute(
            "UPDATE self_model SET is_pinned = 0, updated_at = CURRENT_TIMESTAMP WHERE trait_name = ?;",
            (trait,)
        )
        cursor.execute(
            "INSERT INTO self_model_history (trait_name, old_value, new_value, old_confidence, new_confidence, reason) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (trait, float(old_v), float(old_v), float(old_c), float(old_c), "Manual user unpin override")
        )
        conn.commit()
        return f"[✔] Trait '{trait}' unpinned. It will now drift/decay normally."
    except Exception as e:
        return f"[Error] Failed to unpin trait: {e}"
    finally:
        conn.close()

def handle_agent_command(command_str: str) -> str:
    from src.skills import SafeAgentOrchestration
    sao = SafeAgentOrchestration()
    
    parts = command_str.strip().split(None, 1)
    subcommand = ""
    args_str = ""
    if len(parts) > 1:
        subcommand_part = parts[1].strip()
        sub_parts = subcommand_part.split(None, 1)
        subcommand = sub_parts[0].lower()
        if len(sub_parts) > 1:
            args_str = sub_parts[1].strip()

    if not subcommand or subcommand == "list":
        agents = sao.get_agents()
        if not agents:
            return "No external agents registered yet. Register an agent with `/agent register <name> <api/cli> <endpoint> <api_key> <capabilities>`."
            
        output = ["### 🤖 Project Janus External Coder Agents\n"]
        for a in agents:
            status_emoji = "🟢" if a['is_active'] else "🔴"
            output.append(f"- **{a['name']}** (Type: `{a['type']}`, Endpoint: `{a['endpoint']}`) {status_emoji}")
            output.append(f"  Capabilities: {a['capabilities']}")
        return "\n".join(output)

    elif subcommand == "register":
        import json
        args_parts = args_str.split(None, 4)
        if len(args_parts) < 3:
            return "[Error] Usage: /agent register <name> <api/cli> <endpoint> [api_key] [capabilities_json]"
            
        name = args_parts[0]
        atype = args_parts[1].lower()
        endpoint = args_parts[2]
        
        api_key = ""
        caps = []
        
        if len(args_parts) > 3:
            api_key = args_parts[3]
        if len(args_parts) > 4:
            try:
                caps = json.loads(args_parts[4])
            except Exception:
                caps = [c.strip() for c in args_parts[4].split(",") if c.strip()]
                
        if atype == 'cli' and api_key and not api_key.startswith("[") and not api_key.startswith("{"):
            if api_key.startswith("http") or "/" in api_key:
                pass
            else:
                try:
                    caps = json.loads(api_key)
                    api_key = ""
                except Exception:
                    pass
                    
        try:
            aid = sao.register_agent(name, atype, endpoint, api_key, caps)
            return f"[✔] External agent '{name}' (ID: {aid}) successfully registered."
        except Exception as e:
            return f"[Error] Failed to register agent: {e}"
            
    return "[Error] Unknown subcommand. Supported: list, register"

def handle_dispatch_command(command_str: str) -> str:
    from src.skills import SafeAgentOrchestration
    sao = SafeAgentOrchestration()
    
    parts = command_str.strip().split(None, 1)
    subcommand = ""
    args_str = ""
    if len(parts) > 1:
        subcommand_part = parts[1].strip()
        sub_parts = subcommand_part.split(None, 1)
        subcommand = sub_parts[0].lower()
        if len(sub_parts) > 1:
            args_str = sub_parts[1].strip()

    if not subcommand or subcommand == "list":
        logs = sao.get_all_dispatches()
        if not logs:
            return "No task dispatches logged yet. Dispatch a task with `/dispatch <agent_name> <task_description> [file_paths]`."
            
        output = ["### 📋 External Agent Task Dispatch Log\n"]
        for l in logs:
            status_emoji = {
                'pending': '⏳',
                'in_progress': '⚙️',
                'success': '✅',
                'failed': '❌',
                'reviewed': '📦'
            }.get(l['status'], '❓')
            
            output.append(
                f"- **[{l['id']}]** {status_emoji} Agent: `{l['agent_name']}` | "
                f"Task: *{l['task_description']}* | Status: `{l['status']}` | Sandbox: `{l['sandbox_session_id']}`"
            )
        return "\n".join(output)

    elif subcommand == "review":
        args_parts = args_str.split()
        if len(args_parts) < 2:
            return "[Error] Usage: /dispatch review <id> [approve/reject]"
        try:
            did = int(args_parts[0])
            action = args_parts[1].lower()
            if action not in ('approve', 'reject'):
                return "[Error] Action must be either 'approve' or 'reject'."
                
            approve = (action == 'approve')
            success = sao.review_dispatch(did, approve=approve)
            if success:
                verb = "merged and shipped" if approve else "aborted and discarded"
                return f"[✔] Dispatch [{did}] successfully {verb}."
            return f"[Error] Failed to review dispatch [{did}]. Ensure task is in 'success' or 'failed' status."
        except ValueError:
            return "[Error] Dispatch ID must be an integer."
        except Exception as e:
            return f"[Error] Review failed: {e}"

    else:
        agent_name = subcommand
        
        agents = sao.get_agents()
        agent_names = [a['name'].lower() for a in agents]
        if agent_name not in agent_names:
            return f"[Error] Unknown agent '{agent_name}'. Supported subcommands: list, review, or any registered agent name."
            
        file_paths = []
        task_desc = args_str
        
        bracket_match = re.search(r"\[(.*?)\]$", args_str)
        if bracket_match:
            paths_str = bracket_match.group(1)
            file_paths = [p.strip() for p in paths_str.split(",") if p.strip()]
            task_desc = args_str[:bracket_match.start()].strip()
        else:
            words = args_str.rsplit(None, 1)
            if len(words) == 2 and ("/" in words[1] or "." in words[1]):
                file_paths = [p.strip() for p in words[1].split(",") if p.strip()]
                task_desc = words[0].strip()
                
        matched_agent = next(a for a in agents if a['name'].lower() == agent_name)
        exact_name = matched_agent['name']
        
        try:
            did = sao.dispatch_task(exact_name, task_desc, file_paths)
            status_details = sao.get_dispatch_status(did)
            status = status_details.get("status", "failed")
            
            status_emoji = "✅" if status == "success" else "❌"
            msg = (
                f"[✔] Dispatch [{did}] completed with status '{status}' {status_emoji}.\n"
                f"Sandbox Session: {status_details.get('sandbox_session_id')}\n"
            )
            if status == "success":
                msg += f"Review the changes with `/dispatch list` or check git diff. To merge: `/dispatch review {did} approve`."
            else:
                msg += f"Tests failed. To inspect logs, check the dispatch status or run `/dispatch review {did} reject` to abort."
            return msg
        except Exception as e:
            return f"[Error] Dispatch failed: {e}"

def handle_replication_command(command_str: str) -> str:
    from src.skills import SafeReplication
    rep = SafeReplication()
    
    parts = command_str.strip().split(None, 1)
    cmd = parts[0].lower()
    args_str = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/children":
        children = rep.get_children()
        if not children:
            return "No child Janus instances spawned yet."
            
        output = ["### 🧬 Spawned Child Janus Instances\n"]
        for c in children:
            output.append(
                f"- **PID: {c['child_pid']}** | Status: `{c['status']}` | Path: `{c['child_path']}`\n"
                f"  Spawned: {c['spawned_at']} | Last HB: {c['last_heartbeat']}"
            )
        return "\n".join(output)

    elif cmd == "/spawn":
        if not args_str:
            return "[Error] Usage: /spawn <name> <relative_path>"
            
        args_parts = args_str.split(None, 1)
        if len(args_parts) < 2:
            return "[Error] Usage: /spawn <name> <relative_path>"
            
        name = args_parts[0].strip()
        path = args_parts[1].strip()
        
        try:
            res = rep.spawn_child(name, path)
            if res.get("success"):
                return f"[✔] Successfully spawned child Janus '{name}' (PID: {res['child_pid']}) at path: {res['child_path']}"
            return f"[Error] Failed to spawn child instance: {res}"
        except Exception as e:
            return f"[Error] Failed to spawn child instance: {e}"

    return "[Error] Unknown command. Supported: /spawn, /children"

def handle_goal_command(command_str: str) -> str:
    from src.skills import SafeGoals
    sg = SafeGoals()
    
    parts = command_str.strip().split(None, 1)
    subcommand = ""
    args_str = ""
    if len(parts) > 1:
        subcommand_part = parts[1].strip()
        sub_parts = subcommand_part.split(None, 1)
        subcommand = sub_parts[0].lower()
        if len(sub_parts) > 1:
            args_str = sub_parts[1].strip()

    if not subcommand:
        # List goals
        goals = sg.get_goals()
        if not goals:
            return "No goals established yet. Define a new goal with `/goal create <type> <description>`."
            
        output = ["### 🎯 Project Janus Goal Registry\n"]
        tiers = {'short': [], 'long': [], 'stretch': [], 'aspirational': []}
        for g in goals:
            tiers[g['type']].append(g)
            
        for tier, g_list in tiers.items():
            if not g_list:
                continue
            output.append(f"#### {tier.capitalize()}-Term Goals")
            for g in g_list:
                status_emoji = {
                    'proposed': '💡',
                    'active': '🚀',
                    'in_progress': '⚙️',
                    'completed': '✅',
                    'abandoned': '❌',
                    'archived': '📁',
                    'deleted': '🗑️'
                }.get(g['status'], '❓')
                
                output.append(f"- **[{g['id']}]** {status_emoji} *{g['description']}* (Status: `{g['status']}`)")
                for cp in g['checkpoints']:
                    cp_box = "[x]" if cp['achieved'] else "[ ]"
                    output.append(f"  - {cp_box} checkpoint {cp['id']}: {cp['description']}")
            output.append("")
            
        return "\n".join(output)

    elif subcommand == "create":
        create_parts = args_str.split(None, 1)
        if len(create_parts) < 2:
            return "[Error] Usage: /goal create <type> <description>\nTypes: short, long, stretch, aspirational"
        gtype = create_parts[0].lower()
        description = create_parts[1]
        try:
            gid = sg.create_goal(gtype, description)
            return f"[✔] Goal [{gid}] successfully created under '{gtype}' tier."
        except Exception as e:
            return f"[Error] Failed to create goal: {e}"

    elif subcommand == "status":
        status_parts = args_str.split(None, 1)
        if len(status_parts) < 2:
            return "[Error] Usage: /goal status <goal_id> <status>\nStatuses: proposed, active, in_progress, completed, abandoned, archived, deleted"
        try:
            gid = int(status_parts[0])
            status = status_parts[1].lower()
            success = sg.update_goal_status(gid, status)
            if success:
                return f"[✔] Goal [{gid}] status updated to '{status}'."
            return f"[Error] Goal ID {gid} not found."
        except ValueError:
            return "[Error] Goal ID must be an integer."
        except Exception as e:
            return f"[Error] Failed to update goal: {e}"

    elif subcommand == "checkpoint":
        cp_parts = args_str.split(None, 1)
        if len(cp_parts) < 2:
            return "[Error] Usage: /goal checkpoint <goal_id> <description>"
        try:
            gid = int(cp_parts[0])
            desc = cp_parts[1]
            cpid = sg.add_checkpoint(gid, desc)
            return f"[✔] Checkpoint [{cpid}] added to Goal [{gid}]."
        except ValueError:
            return "[Error] Goal ID must be an integer."
        except Exception as e:
            return f"[Error] Failed to add checkpoint: {e}"

    elif subcommand == "complete":
        if not args_str:
            return "[Error] Usage: /goal complete <checkpoint_id>"
        try:
            cpid = int(args_str)
            success = sg.complete_checkpoint(cpid)
            if success:
                return f"[✔] Checkpoint [{cpid}] marked as completed."
            return f"[Error] Checkpoint ID {cpid} not found."
        except ValueError:
            return "[Error] Checkpoint ID must be an integer."
        except Exception as e:
            return f"[Error] Failed to complete checkpoint: {e}"

    elif subcommand == "prioritize":
        prioritize_parts = args_str.split(None, 1)
        if len(prioritize_parts) < 2:
            return "[Error] Usage: /goal prioritize <goal_id> <priority>\nPriority tiers: short, long, stretch, aspirational"
        try:
            gid = int(prioritize_parts[0])
            priority = prioritize_parts[1].lower()
            if priority not in ('short', 'long', 'stretch', 'aspirational'):
                return "[Error] Priority must be one of: short, long, stretch, aspirational"
            
            res = sg.manage_goals("modify", {"goal_id": gid, "type": priority})
            if res.get("success"):
                return f"[✔] Goal [{gid}] priority tier updated to '{priority}'."
            return f"[Error] Failed to update goal priority: {res.get('error') or res.get('message')}"
        except ValueError:
            return "[Error] Goal ID must be an integer."
        except Exception as e:
            return f"[Error] Failed to prioritize goal: {e}"

    elif subcommand == "proposals":
        proposals = sg.get_proposals()
        if not proposals:
            return "No goal proposals pending. The subconscious reflection loop will queue new proposals here as curiosity vectors evolve."

        output = ["### 💭 Subconscious Goal Proposals\n"]
        status_emoji = {'proposed': '💡', 'approved': '✅', 'rejected': '❌'}
        for p in proposals:
            emoji = status_emoji.get(p['status'], '❓')
            output.append(f"- **[{p['id']}]** {emoji} *{p['description']}* (Type: `{p['type']}`, Confidence: {p['confidence_score']:.2f}, Status: `{p['status']}`)")
            output.append(f"  - Reason: {p['source_reason']}")
        return "\n".join(output)

    elif subcommand == "approve":
        if not args_str:
            return "[Error] Usage: /goal approve <proposal_id>"
        try:
            pid = int(args_str)
        except ValueError:
            return "[Error] Proposal ID must be an integer."
        try:
            res = sg.approve_proposal(pid)
            return f"[✔] Proposal [{pid}] approved and promoted to Goal [{res['goal_id']}]."
        except Exception as e:
            return f"[Error] Failed to approve proposal: {e}"

    elif subcommand == "reject":
        if not args_str:
            return "[Error] Usage: /goal reject <proposal_id>"
        try:
            pid = int(args_str)
        except ValueError:
            return "[Error] Proposal ID must be an integer."
        try:
            sg.reject_proposal(pid)
            return f"[✔] Proposal [{pid}] rejected."
        except Exception as e:
            return f"[Error] Failed to reject proposal: {e}"

    elif subcommand == "resolve":
        return "[Error] Usage: /goals resolve [<dispute_id>] (dispute_id, if given, must be an integer)"

    else:
        return (
            f"[Error] Unknown goal subcommand '{subcommand}'. Available commands: "
            "create, status, checkpoint, complete, prioritize, proposals, approve, reject, resolve."
        )

def handle_docs_command(command_str: str) -> str:
    """
    Handles /docs slash commands for document drafts and memory sync operations.

    Supported subcommands:
      /docs list [#tag]                         — list all documents in database
      /docs get <title>                         — checkout DB document to docs/drafts/<title>.md
      /docs create <title>                      — create a blank local draft at docs/drafts/<title>.md
      /docs commit <filename> | <title> [#tags] — publish a draft file to DB memory
      /docs delete <title>                      — delete a document from the database
      /docs drafts                              — list all local draft files
    """
    from src.skills import DynamicSkillExecutor

    parts = command_str.strip().split(None, 1)
    subcommand = ""
    args_str = ""
    if len(parts) > 1:
        rest = parts[1].strip()
        sub_parts = rest.split(None, 1)
        subcommand = sub_parts[0].lower()
        if len(sub_parts) > 1:
            args_str = sub_parts[1].strip()

    party_id = get_session_party_id()

    def _run(skill: str, **kwargs) -> str:
        res = DynamicSkillExecutor.execute(skill, kwargs, party_id=party_id)
        if res["success"]:
            result = res["result"]
            return result if isinstance(result, str) else str(result)
        return f"[Error] {res['error']}"

    if not subcommand or subcommand == "list":
        tag_filter = args_str.lstrip("#").strip() if args_str else None
        return _run("document_memory", action="list", tag_filter=tag_filter)

    elif subcommand == "get":
        if not args_str:
            return "[Error] Usage: /docs get <title>"
        import re as _re
        clean_title = _re.sub(r'[^a-zA-Z0-9_\-\s]', '', args_str)
        filename = clean_title.strip().replace(" ", "_") + ".md"
        return _run("checkout_db_to_draft", doc_title=args_str, filename=filename)

    elif subcommand == "create":
        if not args_str:
            return "[Error] Usage: /docs create <title>"
        import re as _re
        clean_title = _re.sub(r'[^a-zA-Z0-9_\-\s]', '', args_str)
        filename = clean_title.strip().replace(" ", "_") + ".md"
        content = f"# {args_str}\n\n[Write draft content here]\n"
        return _run("write_draft_file", filename=filename, content=content)

    elif subcommand == "commit" or subcommand == "publish":
        if "|" not in args_str:
            return "[Error] Usage: /docs commit <filename> | <title> [optional #tags]"
        file_part, rest_part = args_str.split("|", 1)
        filename = file_part.strip()
        rest = rest_part.strip()

        import re as _re
        tag_matches = _re.findall(r"#(\w+)", rest)
        title = _re.sub(r"\s*#\w+", "", rest).strip()

        return _run("commit_draft_to_db", filename=filename, doc_title=title, tags=tag_matches or [])

    elif subcommand == "delete":
        if not args_str:
            return "[Error] Usage: /docs delete <title>"
        return _run("delete_db_document", doc_title=args_str)

    elif subcommand in ("drafts", "list-drafts"):
        return _run("list_draft_files")

    else:
        return (
            f"[Error] Unknown /docs subcommand '{subcommand}'. "
            "Supported: list, get, create, commit, delete, drafts."
        )


def _build_persona_prompt(user_query: str, party_id=None) -> tuple:
    """
    Assembles the full prompt and system_override for the Persona agent.
    Returns (prompt: str, system_override: str).
    Extracted from generate_persona_response so both the blocking and streaming
    paths share identical context preparation.
    """
    from src.sandbox_session import get_active_sandbox, get_sandbox_modified_files

    if party_id is None:
        party_id = get_session_party_id()

    # Fetch interaction profile style guidelines for this party
    response_style = "balanced"
    tone_bias = "neutral"
    if party_id:
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT response_style, tone_bias FROM interaction_profiles WHERE party_id = ? LIMIT 1;", (party_id,))
            prof_row = cursor.fetchone()
            if prof_row:
                try:
                    response_style = prof_row['response_style']
                    tone_bias = prof_row['tone_bias']
                except (TypeError, IndexError, KeyError):
                    response_style, tone_bias = prof_row
        except Exception as e:
            logger.error(f"Failed to fetch interaction profile: {e}")
        finally:
            conn.close()

    # Get system prompt for persona and append style guidelines
    from src.llm import get_agent_settings
    settings = get_agent_settings("persona")
    if settings:
        base_prompt = settings[1]
    else:
        base_prompt = "You are the Antigravity Persona agent."

    style_guidelines = f"\n\n### Response Style Guidelines:\n- Response Style: {response_style}\n- Tone Bias: {tone_bias}"
    system_override = base_prompt + style_guidelines

    # 1. Fetch self model traits
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
        logger.error(f"Failed to fetch self model traits: {e}")
    finally:
        conn.close()
    traits_prompt = get_self_model_prompt_guidelines()
    self_traits_str = ""
    if traits_list:
        self_traits_str += "\n".join(traits_list) + "\n\n"
    if traits_prompt:
        self_traits_str += traits_prompt
    else:
        self_traits_str += "None defined."

    # 2. Fetch episodic memories (last 15)
    memories = get_recent_episodic_memories(limit=15, party_id=party_id)
    chat_history = []
    for speaker, msg, _ in reversed(memories):
        if speaker in ("user", "persona", "system", "sandbox_automation"):
            chat_history.append(f"{speaker.upper()}: {msg}")
    history_summary = "\n".join(chat_history) if chat_history else "No previous conversation."

    # 3. Assemble semantic/knowledge context (web search, codebase, active sandbox, ChromaDB)
    semantic_context = ""

    # Active Sandbox Session details
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

    # Live Web Search
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

    # Codebase query
    if detect_codebase_intent(user_query):
        try:
            from src.codebase import query_codebase_context
            codebase_context = query_codebase_context(user_query)
            semantic_context += f"--- Codebase File Summaries ---\n{codebase_context}\n\n"
        except Exception as e:
            logger.error(f"Failed to query codebase index: {e}")

    # ChromaDB semantic queries
    try:
        matches = query_memories(user_query, limit=2, collection_name="janus_long_term")
        if matches:
            semantic_context += "--- Relevant Primary Concepts & Detailed Memories ---\n"
            for match in matches:
                semantic_context += f"- Primary Concept: {match['content']}\n"
                metadata = match.get("metadata") or {}
                detail_ids = metadata.get("detail_ids", "")
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

    semantic_str = semantic_context.strip() if semantic_context.strip() else "None available."

    xml_block = (
        f"<self_traits>\n{self_traits_str}\n</self_traits>\n"
        f"<episodic_memory>\n{history_summary}\n</episodic_memory>\n"
        f"<semantic_knowledge>\n{semantic_str}\n</semantic_knowledge>"
    )

    prompt = f"""
{xml_block}

USER MESSAGE:
{user_query}
"""
    return prompt, system_override


def generate_persona_response(user_query: str, party_id: Optional[str] = None) -> str:
    """
    Queries ChromaDB (Primary Concepts & Codebase) and episodic logs for context,
    performs web searches if requested, and formulates a conversational response.
    """
    try:
        prompt, system_override = _build_persona_prompt(user_query, party_id)
        return query_agent("persona", prompt, system_override=system_override)
    except Exception as e:
        logger.error(f"Failed to generate persona response: {e}")
        raise


def stream_persona_response(user_msg: str, party_id=None):
    """
    Generator yielding (event_type, content) tuples for SSE delivery.
    event_type is one of: "token", "status", "done".

    Mirrors generate_persona_response_autonomous() but streams the final LLM
    turn token-by-token. Intermediate turns (skill calls) run blocking and emit
    "status" events so the client knows work is in progress.

    Speculative streaming: each turn begins streaming immediately. If the first
    ~15 characters start with '{' the response is buffered silently (it's a
    skill-call JSON block). Otherwise tokens are flushed to the client as they
    arrive.
    """
    from src.daemon import parse_action
    from src.skills import DynamicSkillExecutor

    current_query = user_msg
    max_turns = 5

    for _turn in range(max_turns):
        prompt, system_override = _build_persona_prompt(current_query, party_id)

        chunks = []
        flushed = False

        for chunk in query_agent_stream("persona", prompt, system_override=system_override):
            chunks.append(chunk)
            if not flushed:
                assembled = "".join(chunks).lstrip()
                if len(assembled) >= 15:
                    if not assembled.startswith("{"):
                        flushed = True
                        for c in chunks:
                            yield ("token", c)
                    # else: buffering — may be a skill-call JSON block
            else:
                yield ("token", chunk)

        full_response = "".join(chunks)

        skill_id, arguments, mock_result = parse_action(full_response)
        sandbox_blocks = re.findall(r"```sandbox\s*\n(.*?)\n```", full_response, re.DOTALL)
        is_syntax_error = mock_result and mock_result.startswith("Error:")

        if skill_id or sandbox_blocks or is_syntax_error:
            log_episodic_memory("persona", full_response, "background_thought", party_id=party_id)

        if not skill_id and not sandbox_blocks:
            if is_syntax_error:
                log_episodic_memory("sandbox_automation", mock_result, "background_thought", party_id=party_id)
                current_query = (
                    f"Tool execution failed with a syntax error:\n{mock_result}\n"
                    "Please correct the syntax and try again using the valid JSON format: "
                    "{\"skill_id\": \"<skill_id>\", \"arguments\": { ... }}."
                )
                yield ("status", "Correcting tool call…")
                continue
            # Final response — flush buffer if speculative streaming never fired
            if not flushed:
                yield ("token", full_response)
            break

        # Execute skill / sandbox
        execution_summary = ""

        if skill_id:
            yield ("status", f"Using tool: {skill_id}…")
            try:
                res = DynamicSkillExecutor.execute(skill_id, arguments, party_id=party_id or get_session_party_id())
                if res["success"]:
                    execution_summary += f"[Skill '{skill_id}' executed successfully]\nResult: {res['result']}\n\n"
                else:
                    execution_summary += f"[Skill '{skill_id}' failed]\nError: {res['error']}\n\n"
            except Exception as e:
                execution_summary += f"[Skill '{skill_id}' error]\nException: {e}\n\n"

        if sandbox_blocks:
            yield ("status", "Executing sandbox commands…")
            results = [execute_chat_sandbox_commands(b) for b in sandbox_blocks]
            execution_summary += "[Sandbox Commands Executed]\n" + "\n\n".join(results)

        log_episodic_memory("sandbox_automation", execution_summary.strip(), "background_thought", party_id=party_id)

        has_failure = any(w in execution_summary for w in ("Failed", "Error", "Exception"))
        if has_failure:
            current_query = "Some actions or skills failed. Please review the background thought history, address the failure, and try again or proceed."
        else:
            current_query = "Executed requested actions/skills. Please review the background thought history and continue."

    yield ("done", "")

def execute_chat_sandbox_commands(block: str) -> str:
    """
    Parses and executes commands inside a ```sandbox ``` block.
    Supported commands:
      - read <path> or read: <path>
      - test or run_tests or run-tests
      - diff
      - checkout <path> or checkout: <path>
      - discard
      - rollback
    """
    from src.config import get_effective_workspace_root
    from src.sandbox_session import run_sandbox_tests, get_sandbox_diff
    
    workspace_root = get_effective_workspace_root()
    results = []
    
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
            
        read_match = re.match(r"^read(?:\s*:\s*|\s+)(.*)$", line, re.IGNORECASE)
        checkout_match = re.match(r"^checkout(?:\s*:\s*|\s+)(.*)$", line, re.IGNORECASE)
        
        if read_match:
            rel_path = read_match.group(1).strip().rstrip(".,;!?`\"'")
            try:
                # Ensure path is clean and resolved within the effective workspace root
                full_path = (workspace_root / rel_path).resolve()
                if not str(full_path).startswith(str(workspace_root.resolve())):
                    results.append(f"- read {rel_path}: Access denied (outside workspace).")
                    continue
                
                if full_path.exists() and full_path.is_file():
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    results.append(f"- read {rel_path}:\n```\n{content}\n```")
                else:
                    results.append(f"- read {rel_path}: File not found.")
            except Exception as e:
                results.append(f"- read {rel_path}: Error reading file: {e}")
                
        elif checkout_match:
            rel_path = checkout_match.group(1).strip().rstrip(".,;!?`\"'")
            try:
                # Ensure path is clean and resolved within the effective workspace root
                full_path = (workspace_root / rel_path).resolve()
                if not str(full_path).startswith(str(workspace_root.resolve())):
                    results.append(f"- checkout {rel_path}: Access denied (outside workspace).")
                    continue
                
                import subprocess
                res = subprocess.run(
                    ["git", "checkout", "--", rel_path],
                    cwd=workspace_root,
                    capture_output=True,
                    text=True
                )
                if res.returncode == 0:
                    results.append(f"- checkout {rel_path}: Reverted successfully.")
                else:
                    results.append(f"- checkout {rel_path}: Failed to checkout. Git error: {res.stderr or res.stdout}")
            except Exception as e:
                results.append(f"- checkout {rel_path}: Error running checkout: {e}")
                
        elif line.lower() in ("test", "run_tests", "run-tests"):
            try:
                passed, logs = run_sandbox_tests()
                status = "PASSED" if passed else "FAILED"
                results.append(f"- test: {status}\nLogs:\n{logs}")
            except Exception as e:
                results.append(f"- test: Error running tests: {e}")
                
        elif line.lower() == "diff":
            try:
                diff = get_sandbox_diff()
                results.append(f"- diff:\n```diff\n{diff}\n```")
            except Exception as e:
                results.append(f"- diff: Error getting diff: {e}")
                
        elif line.lower() == "discard":
            try:
                from src.sandbox_session import discard_sandbox_changes
                success = discard_sandbox_changes()
                if success:
                    results.append("- discard: All uncommitted sandbox changes discarded successfully.")
                else:
                    results.append("- discard: Failed to discard sandbox changes.")
            except Exception as e:
                results.append(f"- discard: Error discarding changes: {e}")
                
        elif line.lower() == "rollback":
            try:
                from src.sandbox_session import rollback_sandbox_last_commit
                success = rollback_sandbox_last_commit()
                if success:
                    results.append("- rollback: Rolled back the last commit in the sandbox successfully.")
                else:
                    results.append("- rollback: Failed to rollback last commit in the sandbox.")
            except Exception as e:
                results.append(f"- rollback: Error rolling back last commit: {e}")
                
        else:
            results.append(f"- {line}: Unknown sandbox command.")
            
    return "\n".join(results)

def generate_persona_response_autonomous(user_msg: str, party_id: Optional[str] = None) -> str:
    """
    Autonomous ReAct loop for Persona chat. Resolves sandbox command blocks
    by executing them, logging to episodic memory, and re-querying the Persona
    up to 5 turns.
    """
    # We no longer short-circuit based on active_sb, because the Persona might execute a dynamic skill to create one.
        
    current_query = user_msg
    max_turns = 5
    turn = 0
    final_response = ""
    
    while turn < max_turns:
        turn += 1
        logger.info(f"Autonomous loop turn {turn}/{max_turns} for query: {current_query[:50]}")
        
        # 1. Generate Persona response
        if party_id is not None:
            response = generate_persona_response(current_query, party_id=party_id)
        else:
            response = generate_persona_response(current_query)
        final_response = response
        
        # 2. Check for proposed code modifications
        proposed_mods = parse_proposed_changes(response)
        if proposed_mods:
            print(f"\n[Janus Daemon] Extracted proposed modifications for {len(proposed_mods)} file(s).")
            print("Applying changes to sandbox...")
            from src.sandbox_session import apply_changes_to_sandbox, run_sandbox_tests
            try:
                apply_changes_to_sandbox(proposed_mods)
                print("Executing unit tests in sandbox...")
                passed, logs = run_sandbox_tests()
                test_status = "PASSED" if passed else "FAILED"
                print(f"[Janus Daemon] Sandbox unit tests: {test_status}")
                
                # Run tests automatically so the Persona receives feedback
                log_episodic_memory(
                    speaker="sandbox_automation",
                    message_content=f"Auto-applied modifications to {list(proposed_mods.keys())}. Sandbox tests: {test_status}.\nLogs/Errors:\n{logs}",
                    context_type="background_thought",
                    party_id=party_id
                )
            except Exception as apply_err:
                log_episodic_memory(
                    speaker="sandbox_automation",
                    message_content=f"Failed to apply modifications. Is there an active sandbox session? Error: {apply_err}",
                    context_type="background_thought",
                    party_id=party_id
                )
            
        # 3. Check for JSON dynamic skill execution blocks
        from src.daemon import parse_action
        from src.skills import DynamicSkillExecutor
        skill_id, arguments, mock_result = parse_action(response)
        
        # 4. Check for legacy sandbox command blocks (```sandbox ... ```)
        sandbox_blocks = re.findall(r"```sandbox\s*\n(.*?)\n```", response, re.DOTALL)
        
        # Log the intermediate persona response to episodic memory as background thought
        # so it is preserved in history for subsequent turns of this autonomous loop
        is_syntax_error = mock_result and mock_result.startswith("Error:")
        if skill_id or sandbox_blocks or is_syntax_error:
            log_episodic_memory(
                speaker="persona",
                message_content=response,
                context_type="background_thought",
                party_id=party_id
            )

        if not skill_id and not sandbox_blocks:
            if is_syntax_error:
                # Log the parse error as a background thought so the agent can see it
                log_episodic_memory(
                    speaker="sandbox_automation",
                    message_content=mock_result,
                    context_type="background_thought",
                    party_id=party_id
                )
                current_query = f"The previous tool execution failed with a syntax error:\n{mock_result}\nPlease correct the syntax and try again using the valid JSON format: {{\"skill_id\": \"<skill_id>\", \"arguments\": {{ ... }} }}."
                continue
            else:
                # No actions or sandbox commands to execute, return the response
                break
            
        execution_summary = ""
        
        if skill_id:
            print(f"\n[Janus Daemon] Dynamic Skill Block Detected: '{skill_id}'")
            try:
                res = DynamicSkillExecutor.execute(skill_id, arguments, party_id=party_id or get_session_party_id())
                if res["success"]:
                    execution_summary += f"[Dynamic Skill '{skill_id}' Executed Successfully]\nResult: {res['result']}\n\n"
                else:
                    execution_summary += f"[Dynamic Skill '{skill_id}' Failed]\nError: {res['error']}\n\n"
            except Exception as e:
                execution_summary += f"[Dynamic Skill '{skill_id}' Error]\nException: {e}\n\n"

        if sandbox_blocks:
            print(f"\n[Janus Daemon] Found {len(sandbox_blocks)} sandbox command block(s). Executing...")
            # Execute sandbox commands
            execution_results = []
            for block in sandbox_blocks:
                res = execute_chat_sandbox_commands(block)
                execution_results.append(res)
            execution_summary += "[Sandbox Commands Executed]\n" + "\n\n".join(execution_results)
        
        # 5. Log execution results to SQLite as a background thought
        log_episodic_memory(
            speaker="sandbox_automation",
            message_content=execution_summary.strip(),
            context_type="background_thought",
            party_id=party_id
        )
        
        # 6. Formulate next turn query to continue conversation, highlighting failures if any
        has_failure = "Failed" in execution_summary or "Error" in execution_summary or "Exception" in execution_summary
        if has_failure:
            current_query = "Some actions or skills failed. Please review the background thought history, address the failure, and try again or proceed."
        else:
            current_query = "Executed requested actions/skills. Please review the background thought history and continue."
        
    return final_response

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
                    print("\n[Error] Missing sandbox command. Usage: /sandbox [start | status | diff | ship | promote | abort]\n")
                    continue

                cmd_type = parts[1].lower()
                from src.sandbox_session import (
                    get_active_sandbox, create_sandbox_session, run_sandbox_tests,
                    get_sandbox_diff, get_sandbox_modified_files, ship_sandbox_session,
                    abort_sandbox_session, promote_evolution_sandbox, delete_project_sandbox
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
                        
                elif cmd_type == "promote":
                    active = get_active_sandbox()
                    if not active:
                        print("\nJanus >> No active sandbox session.\n")
                        continue
                    if active.get("active_sandbox_purpose") != "evolution":
                        print("\n[Error] /sandbox promote only applies to evolution-purpose sessions.\n")
                        continue
                    confirm = await loop.run_in_executor(
                        None, get_input,
                        "Promote this evolution sandbox? This ships code to main and queues any "
                        "schema/memory deltas for review. (y/n): "
                    )
                    if confirm.strip().lower() in ("y", "yes"):
                        try:
                            result = promote_evolution_sandbox()
                            print(f"\n[✔] Sandbox promoted!")
                            print(f"  * Files merged: {len(result['copied_files'])}")
                            print(f"  * Schema migrations queued for review: {result['queued_migrations']}")
                            print(f"  * Memories ported: {result['ported_memories']}\n")
                            log_episodic_memory(
                                "sandbox_automation",
                                f"Sandbox session branch '{active['active_sandbox_branch']}' promoted. "
                                f"Files: {len(result['copied_files'])}, migrations queued: "
                                f"{result['queued_migrations']}, memories ported: {result['ported_memories']}.",
                                "user_visible"
                            )
                        except Exception as err:
                            print(f"\n[Error] Failed to promote sandbox: {err}\n")
                    else:
                        print("\nPromotion canceled. Sandbox session remains active.\n")

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
                    print(f"\n[Error] Unknown sandbox command '{cmd_type}'. Options are: start, status, diff, ship, promote, abort\n")
                continue

            if user_msg.lower().startswith("/project"):
                parts = user_msg.split()
                if len(parts) < 2:
                    print("\n[Error] Missing project command. Usage: /project [start | status | delete] <name>\n")
                    continue

                cmd_type = parts[1].lower()
                from src.sandbox_session import (
                    get_active_sandbox, create_sandbox_session,
                    abort_sandbox_session, delete_project_sandbox
                )

                if cmd_type == "start":
                    if len(parts) < 3:
                        print("\n[Error] Please specify an app name: /project start <name>\n")
                        continue
                    app_name = parts[2]
                    active = get_active_sandbox()
                    if active:
                        print(f"\n[Warning] An active sandbox session already exists at '{active['active_sandbox_path']}'.")
                        confirm_abort = await loop.run_in_executor(None, get_input, "Abort the existing session first? (y/n): ")
                        if confirm_abort.strip().lower() in ("y", "yes"):
                            abort_sandbox_session()
                            print("[Janus] Existing sandbox session aborted.")
                        else:
                            print("[Janus] Action canceled. Existing sandbox remains active.")
                            continue
                    try:
                        path, _ = create_sandbox_session(app_name, purpose="project", app_name=app_name)
                        print(f"\n[✔] Project sandbox '{app_name}' created!")
                        print(f"  * Workspace Path: {path}\n")
                        log_episodic_memory(
                            "sandbox_automation",
                            f"Project sandbox '{app_name}' initialized at '{path}'.",
                            "user_visible"
                        )
                    except Exception as err:
                        print(f"\n[Error] Failed to create project sandbox: {err}\n")

                elif cmd_type == "status":
                    active = get_active_sandbox()
                    if not active or active.get("active_sandbox_purpose") != "project":
                        print("\nJanus >> No active project sandbox session. Start one with: /project start <name>\n")
                    else:
                        print("\n" + "="*60)
                        print("[Janus] Active Project Sandbox Status")
                        print("="*60)
                        print(f"  * App Name: {active.get('active_sandbox_app_name', '')}")
                        print(f"  * Path:     {active['active_sandbox_path']}")
                        print(f"  * Status:   {active['active_sandbox_status'].upper()}")
                        print("="*60 + "\n")

                elif cmd_type == "delete":
                    if len(parts) < 3:
                        print("\n[Error] Please specify an app name: /project delete <name>\n")
                        continue
                    app_name = parts[2]
                    confirm = await loop.run_in_executor(
                        None, get_input,
                        f"Permanently delete project sandbox '{app_name}'? This cannot be undone. (y/n): "
                    )
                    if confirm.strip().lower() in ("y", "yes"):
                        deleted = delete_project_sandbox(app_name)
                        if deleted:
                            print(f"\n[✔] Project sandbox '{app_name}' deleted.\n")
                            log_episodic_memory(
                                "sandbox_automation",
                                f"Project sandbox '{app_name}' permanently deleted.",
                                "user_visible"
                            )
                        else:
                            print(f"\n[Error] No project sandbox named '{app_name}' was found.\n")
                    else:
                        print("\nDelete canceled.\n")

                else:
                    print(f"\n[Error] Unknown project command '{cmd_type}'. Options are: start, status, delete\n")
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
                    from src.config import get_effective_workspace_root
                    workspace_root = get_effective_workspace_root()
                    mod_files = list(proposed_mods.keys())
                    for idx, file in enumerate(mod_files, start=1):
                        is_new = not (workspace_root / file).exists()
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
                                    from src.config import get_effective_workspace_root
                                    full_path = get_effective_workspace_root() / file_path
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
                                        from src.config import get_effective_workspace_root
                                        full_path = get_effective_workspace_root() / test_file
                                        current_content = ""
                                        if full_path.exists():
                                            try:
                                                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                                                    current_content = f.read()
                                            except Exception:
                                                pass
                                                
                                        from src.self_modification import summarize_pytest_logs
                                        draft_prompt = f"""
                                        You are the Proposer. A pre-existing test file has failed during staging.
                                        We need to automatically fix (self-heal) this test file so it passes correctly.
                                        
                                        FAILING TEST FILE: {test_file}
                                        
                                        TEST RUN FAILURE LOGS:
                                        {summarize_pytest_logs(logs)}
                                        
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
                                from src.config import get_effective_workspace_root
                                full_path = get_effective_workspace_root() / target_file
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
                from src.config import get_effective_workspace_root
                full_path = get_effective_workspace_root() / file_path
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

            # Handle constitutional repeals interceptor
            if user_msg.lower().startswith("/repeal"):
                repeal_match = re.match(r"^/repeal\s+([a-z0-9_-]+)", user_msg, re.IGNORECASE)
                if repeal_match:
                    rule_key = repeal_match.group(1).strip()
                    
                    print(f"\nJanus >> Proposing constitutional repeal:")
                    print(f"  * Key: '{rule_key}'")
                    
                    confirm_input = await loop.run_in_executor(None, get_input, f"Confirm repealing rule '{rule_key}' from core_constitution? (y/n): ")
                    if confirm_input.strip().lower() in ("y", "yes"):
                        from src.database import delete_constitution_rule
                        delete_constitution_rule(rule_key)
                        print(f"\n[✔] Rule '{rule_key}' successfully repealed from the core constitution.\n")
                        log_episodic_memory("system", f"User repealed constitutional rule: '{rule_key}'", "user_visible")
                    else:
                        print("\nRepeal proposal aborted.\n")
                else:
                    print("\nJanus >> Invalid format. Please use: /repeal <rule_key>\n")
                continue

            # Handle dispute resolution interceptor (V2-T10)
            resolve_match = re.match(r"^/goals?\s+resolve\s*(\d+)?\s*$", user_msg.strip(), re.IGNORECASE)
            if resolve_match:
                from src.database import add_constitution_rule, get_dispute, get_open_disputes, resolve_dispute

                dispute_arg = resolve_match.group(1)

                if not dispute_arg:
                    disputes = get_open_disputes()
                    if not disputes:
                        print(
                            "\nNo open disputes. The autonomous loop is not currently "
                            "paused for dispute resolution.\n"
                        )
                    else:
                        output = ["\n### ⚖️ Open Swarm Disputes\n"]
                        for d in disputes:
                            output.append(
                                f"- **[{d['id']}]** *{d['proposed_action']}* — vetoed {d['veto_count']}x "
                                f"consecutively (opened {d['created_at']})"
                            )
                        output.append(
                            "\nUse `/goals resolve <id>` to review the debate transcript and choose a resolution.\n"
                        )
                        print("\n".join(output))
                    continue

                dispute_id = int(dispute_arg)
                dispute = get_dispute(dispute_id)
                if not dispute:
                    print(f"\n[Error] Dispute ID {dispute_id} not found.\n")
                    continue
                if dispute["status"] == "resolved":
                    print(
                        f"\n[Info] Dispute [{dispute_id}] was already resolved "
                        f"({dispute['resolution']}) at {dispute['resolved_at']}.\n"
                    )
                    continue

                print(
                    f"\nJanus >> Dispute [{dispute_id}]: '{dispute['proposed_action']}' — "
                    f"vetoed {dispute['veto_count']}x consecutively.\n"
                )
                print("Debate transcript:")
                for entry in dispute["debate_transcript"]:
                    print(f"  [{entry['timestamp']}] Action Proposed: '{entry['proposed_action']}' | Status: Vetoed")
                    print(f"  Critic Justification: {entry['justification']}\n")

                resolution_input = await loop.run_in_executor(
                    None, get_input, "Choose a resolution: override / abort / rewrite (or cancel): "
                )
                resolution_input = resolution_input.strip().lower()

                if resolution_input == "override":
                    resolve_dispute(dispute_id, "override")
                    print(
                        f"\n[✔] Dispute [{dispute_id}] resolved: override. The Critic's veto stands "
                        "for that action; the autonomous loop will resume.\n"
                    )
                    log_episodic_memory(
                        "system",
                        f"User overrode dispute [{dispute_id}] on action '{dispute['proposed_action']}'. "
                        "Autonomous loop resumed.",
                        "user_visible"
                    )
                elif resolution_input == "abort":
                    resolve_dispute(dispute_id, "abort")
                    print(
                        f"\n[✔] Dispute [{dispute_id}] resolved: abort. The disputed action will not "
                        "be pursued; the autonomous loop will resume.\n"
                    )
                    log_episodic_memory(
                        "system",
                        f"User aborted dispute [{dispute_id}] on action '{dispute['proposed_action']}'. "
                        "Autonomous loop resumed.",
                        "user_visible"
                    )
                elif resolution_input == "rewrite":
                    rewrite_input = await loop.run_in_executor(
                        None, get_input, "Enter rule to amend, format <rule_key> | <new_rule_text>: "
                    )
                    rewrite_match = re.match(r"^([a-z0-9_-]+)\s*\|\s*(.*)", rewrite_input.strip(), re.IGNORECASE)
                    if rewrite_match:
                        rule_key = rewrite_match.group(1).strip()
                        rule_text = rewrite_match.group(2).strip()
                        add_constitution_rule(rule_key, rule_text)
                        resolve_dispute(dispute_id, "rewrite_rules", notes=f"{rule_key} | {rule_text}")
                        print(
                            f"\n[✔] Dispute [{dispute_id}] resolved: rewrite_rules. Rule '{rule_key}' "
                            "sealed in core_constitution. The autonomous loop will resume.\n"
                        )
                        log_episodic_memory(
                            "system",
                            f"User resolved dispute [{dispute_id}] by rewriting constitutional rule "
                            f"'{rule_key}'. Autonomous loop resumed.",
                            "user_visible"
                        )
                    else:
                        print("\n[Error] Invalid format. Resolution cancelled; dispute remains open.\n")
                else:
                    print("\nResolution cancelled; dispute remains open and the autonomous loop stays paused.\n")
                continue

            if user_msg.lower().startswith("/skills"):
                res = handle_skills_command()
                print(f"\n{res}\n")
                continue

            if user_msg.lower().startswith("/runskill"):
                res = handle_runskill_command(user_msg)
                print(f"\n{res}\n")
                continue

            user_msg_lower = user_msg.strip().lower()
            if user_msg_lower == "/self" or user_msg_lower.startswith("/self "):
                res = handle_self_command()
                print(f"\n{res}\n")
                continue

            if user_msg_lower.startswith("/pin ") or user_msg_lower == "/pin":
                res = handle_pin_command(user_msg)
                print(f"\n{res}\n")
                continue

            if user_msg_lower.startswith("/unpin ") or user_msg_lower == "/unpin":
                res = handle_unpin_command(user_msg)
                print(f"\n{res}\n")
                continue

            if user_msg_lower == "/agent" or user_msg_lower.startswith("/agent "):
                res = handle_agent_command(user_msg)
                print(f"\n{res}\n")
                continue

            if user_msg_lower == "/dispatch" or user_msg_lower.startswith("/dispatch "):
                res = handle_dispatch_command(user_msg)
                print(f"\n{res}\n")
                continue

            if user_msg_lower == "/spawn" or user_msg_lower.startswith("/spawn ") or user_msg_lower == "/children" or user_msg_lower.startswith("/children "):
                res = handle_replication_command(user_msg)
                print(f"\n{res}\n")
                continue

            if user_msg_lower == "/goal" or user_msg_lower.startswith("/goal ") or user_msg_lower == "/goals" or user_msg_lower.startswith("/goals "):
                res = handle_goal_command(user_msg)
                print(f"\n{res}\n")
                continue

            if user_msg_lower == "/docs" or user_msg_lower.startswith("/docs "):
                res = handle_docs_command(user_msg)
                print(f"\n{res}\n")
                continue

            # Log user prompt to SQLite
            log_episodic_memory("user", user_msg, "user_visible")

            # Determine query intent and route
            if detect_metacognitive_intent(user_msg):
                response = generate_metacognitive_narrative(user_msg)
            else:
                response = await asyncio.to_thread(generate_persona_response_autonomous, user_msg)

            print(f"\nJanus >> {response}\n")

            # Log persona response to SQLite
            log_episodic_memory("persona", response, "user_visible")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in persona chat: {e}", exc_info=True)
            print(f"\nJanus >> (Error communicating with internal swarm: {e})\n")


async def handle_web_slash_command(user_msg: str) -> str:
    """
    Processes slash commands in Web UI mode asynchronously and returns a response text.
    Handles /sandbox, /stage, /modify, /amend.
    """
    import shutil
    from pathlib import Path
    user_msg = user_msg.strip()
    
    # 1. /sandbox commands
    if user_msg.lower().startswith("/sandbox"):
        parts = user_msg.split()
        if len(parts) < 2:
            return "[Error] Missing sandbox command. Usage: /sandbox [start | status | diff | ship | promote | abort]"

        cmd_type = parts[1].lower()
        from src.sandbox_session import (
            get_active_sandbox, create_sandbox_session, run_sandbox_tests,
            get_sandbox_diff, get_sandbox_modified_files, ship_sandbox_session,
            abort_sandbox_session, promote_evolution_sandbox, delete_project_sandbox
        )
        
        if cmd_type == "start":
            if len(parts) < 3:
                return "[Error] Please specify a session name: /sandbox start <name>"
            session_name = parts[2]
            active = get_active_sandbox()
            if active:
                return f"[Error] An active sandbox session already exists at '{active['active_sandbox_path']}' on branch '{active['active_sandbox_branch']}'. Abort/ship it first."
            try:
                path, branch = create_sandbox_session(session_name)
                log_episodic_memory(
                    "sandbox_automation",
                    f"Sandbox session '{session_name}' initialized on branch '{branch}'. Sandbox path: '{path}'.",
                    "user_visible"
                )
                return f"[✔] Sandbox session '{session_name}' successfully created!\n* Workspace Path: {path}\n* Git Branch: {branch}"
            except Exception as err:
                return f"[Error] Failed to create sandbox session: {err}"
                
        elif cmd_type == "status":
            active = get_active_sandbox()
            if not active:
                return "Janus >> No active sandbox session. Start one with: /sandbox start <name>"
            
            modified_files = get_sandbox_modified_files()
            modified_str = ", ".join(modified_files) if modified_files else "None"
            
            status_text = (
                f"Janus >> Active Sandbox Session Status\n"
                f"* Path: {active['active_sandbox_path']}\n"
                f"* Branch: {active['active_sandbox_branch']}\n"
                f"* Status: {active['active_sandbox_status'].upper()}\n"
                f"* Modified Files: {modified_str}\n"
            )
            if active.get("active_sandbox_status") == "failed" and active.get("active_sandbox_test_logs"):
                status_text += f"\nLast Test Failures/Logs:\n{active['active_sandbox_test_logs']}"
            return status_text
            
        elif cmd_type == "diff":
            active = get_active_sandbox()
            if not active:
                return "Janus >> No active sandbox session."
            diff = get_sandbox_diff()
            if not diff.strip():
                return "No changes in sandbox."
            return f"Janus >> Cumulative Sandbox Diff ({active['active_sandbox_branch']}):\n\n{diff}"
            
        elif cmd_type == "test":
            active = get_active_sandbox()
            if not active:
                return "Janus >> No active sandbox session."
            passed, logs = await asyncio.to_thread(run_sandbox_tests)
            status_str = "PASSED" if passed else "FAILED"
            return f"Janus >> Sandbox tests completed: {status_str}\n\n{logs}"
            
        elif cmd_type == "ship":
            active = get_active_sandbox()
            if not active:
                return "Janus >> No active sandbox session."
            passed, logs = await asyncio.to_thread(run_sandbox_tests)
            try:
                copied = ship_sandbox_session()
                msg = f"Sandbox session branch '{active['active_sandbox_branch']}' successfully shipped and applied. Files modified: {', '.join(copied)}."
                log_episodic_memory("sandbox_automation", msg, "user_visible")
                return f"[✔] Sandbox successfully shipped and applied!\n* Merged Files:\n" + "\n".join(f"  - {f}" for f in copied)
            except Exception as err:
                return f"[Error] Failed to ship sandbox changes: {err}"
                
        elif cmd_type == "promote":
            active = get_active_sandbox()
            if not active:
                return "Janus >> No active sandbox session."
            if active.get("active_sandbox_purpose") != "evolution":
                return "[Error] /sandbox promote only applies to evolution-purpose sessions."
            try:
                result = await asyncio.to_thread(promote_evolution_sandbox)
                msg = (
                    f"Sandbox session branch '{active['active_sandbox_branch']}' promoted. "
                    f"Files: {len(result['copied_files'])}, migrations queued: {result['queued_migrations']}, "
                    f"memories ported: {result['ported_memories']}."
                )
                log_episodic_memory("sandbox_automation", msg, "user_visible")
                return (
                    f"[✔] Sandbox promoted!\n"
                    f"* Files merged: {len(result['copied_files'])}\n"
                    f"* Schema migrations queued for review: {result['queued_migrations']}\n"
                    f"* Memories ported: {result['ported_memories']}"
                )
            except Exception as err:
                return f"[Error] Failed to promote sandbox: {err}"

        elif cmd_type == "abort":
            active = get_active_sandbox()
            if not active:
                return "Janus >> No active sandbox session."
            try:
                abort_sandbox_session()
                log_episodic_memory(
                    "sandbox_automation",
                    f"Sandbox session branch '{active['active_sandbox_branch']}' aborted and cleaned up.",
                    "user_visible"
                )
                return f"[✔] Sandbox session '{active['active_sandbox_branch']}' aborted and cleaned up."
            except Exception as err:
                return f"[Error] Failed to abort sandbox: {err}"
        else:
            return f"[Error] Unknown sandbox command '{cmd_type}'. Options are: start, status, diff, ship, promote, abort"

    # 1b. /project commands
    elif user_msg.lower().startswith("/project"):
        parts = user_msg.split()
        if len(parts) < 2:
            return "[Error] Missing project command. Usage: /project [start | status | delete] <name>"

        cmd_type = parts[1].lower()
        from src.sandbox_session import (
            get_active_sandbox, create_sandbox_session, delete_project_sandbox
        )

        if cmd_type == "start":
            if len(parts) < 3:
                return "[Error] Please specify an app name: /project start <name>"
            app_name = parts[2]
            active = get_active_sandbox()
            if active:
                return f"[Error] An active sandbox session already exists at '{active['active_sandbox_path']}'. Abort/ship it first."
            try:
                path, _ = create_sandbox_session(app_name, purpose="project", app_name=app_name)
                log_episodic_memory(
                    "sandbox_automation",
                    f"Project sandbox '{app_name}' initialized at '{path}'.",
                    "user_visible"
                )
                return f"[✔] Project sandbox '{app_name}' created!\n* Workspace Path: {path}"
            except Exception as err:
                return f"[Error] Failed to create project sandbox: {err}"

        elif cmd_type == "status":
            active = get_active_sandbox()
            if not active or active.get("active_sandbox_purpose") != "project":
                return "Janus >> No active project sandbox session. Start one with: /project start <name>"
            return (
                f"Janus >> Active Project Sandbox Status\n"
                f"* App Name: {active.get('active_sandbox_app_name', '')}\n"
                f"* Path: {active['active_sandbox_path']}\n"
                f"* Status: {active['active_sandbox_status'].upper()}\n"
            )

        elif cmd_type == "delete":
            if len(parts) < 3:
                return "[Error] Please specify an app name: /project delete <name>"
            app_name = parts[2]
            deleted = delete_project_sandbox(app_name)
            if deleted:
                log_episodic_memory(
                    "sandbox_automation",
                    f"Project sandbox '{app_name}' permanently deleted.",
                    "user_visible"
                )
                return f"[✔] Project sandbox '{app_name}' deleted."
            return f"[Error] No project sandbox named '{app_name}' was found."

        else:
            return f"[Error] Unknown project command '{cmd_type}'. Options are: start, status, delete"

    # 2. /stage commands
    elif user_msg.lower().startswith("/stage"):
        parts = user_msg.split()
        limit = 1
        if len(parts) > 1:
            try:
                limit = int(parts[1])
                if limit <= 0:
                    raise ValueError()
            except ValueError:
                return "[Error] Invalid lookback count. Usage: /stage [count] (must be positive integer)"
                
        last_msg = get_recent_persona_messages(limit)
        if not last_msg:
            return "Janus >> No previous message found to stage changes from."
            
        proposed_mods = await asyncio.to_thread(parse_proposed_changes, last_msg)
        if not proposed_mods:
            return "Janus >> No proposed code changes or file paths could be parsed from the messages."
            
        vetoed_files = {}
        approved_files = set()
        
        audit_tasks = []
        for file_path, proposed_code in proposed_mods.items():
            audit_prompt = f"""
            You are the Critic. Audit the proposed code modification to '{file_path}' against our core constitution:
            
            PROPOSED CODE MODIFICATION:
            {proposed_code}
            
            Perform a strict audit. Evaluate the systemic utility of this change and determine if it violates any rules in the core constitution.
            Output your decision exactly in one of these formats:
            CRITIC_DECISION: APPROVED | Justification: [Your reasoning]
            CRITIC_DECISION: VETOED | Justification: [Your reasoning]
            """
            audit_tasks.append((file_path, proposed_code, audit_prompt))
            
        if audit_tasks:
            audit_results = await asyncio.gather(*[
                asyncio.to_thread(query_agent, "critic", item[2])
                for item in audit_tasks
            ])
        else:
            audit_results = []
            
        for (file_path, proposed_code, _), critic_resp in zip(audit_tasks, audit_results):
            critic_decision = 1
            critic_justification = "Automatic approval"
            decision_match = re.search(r"CRITIC_DECISION:\s*(APPROVED|VETOED)", critic_resp, re.IGNORECASE)
            justification_match = re.search(r"Justification:\s*(.*)", critic_resp, re.IGNORECASE)
            
            if decision_match:
                if decision_match.group(1).upper() == "VETOED":
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
                vetoed_files[file_path] = critic_justification
            else:
                approved_files.add(file_path)
                
        if vetoed_files:
            refine_tasks = []
            for file_path, reason in vetoed_files.items():
                from src.config import get_effective_workspace_root
                full_path = get_effective_workspace_root() / file_path
                current_content = ""
                if full_path.exists():
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            current_content = f.read()
                    except Exception:
                        pass
                draft_prompt = f"""
                You are the Proposer. The Critic has vetoed the previous draft with feedback:
                VETO REASON / FEEDBACK: {reason}
                FILE TO MODIFY: {file_path}
                CURRENT FILE CONTENT: {current_content}
                Generate the COMPLETE updated source code for the file '{file_path}' addressing all feedback.
                CRITICAL RULES:
                1. Output ONLY the raw source code of the file.
                2. Do NOT wrap the output in markdown code blocks.
                """
                refine_tasks.append((file_path, draft_prompt))
                
            refine_results = await asyncio.gather(*[
                asyncio.to_thread(query_agent, "proposer", item[1])
                for item in refine_tasks
            ])
            for (file_path, _), proposed_code in zip(refine_tasks, refine_results):
                if proposed_code.strip().startswith("```"):
                    lines = proposed_code.strip().splitlines()
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].strip() == "```":
                        lines = lines[:-1]
                    proposed_code = "\n".join(lines) + "\n"
                proposed_mods[file_path] = proposed_code
                
        from src.self_modification import stage_and_test_multi, generate_multi_diff
        passed, logs, temp_dir = await asyncio.to_thread(stage_and_test_multi, proposed_mods)
        diff = generate_multi_diff(proposed_mods)
        
        from src.database import stage_modification_in_db
        files_str = ",".join(proposed_mods.keys())
        stage_modification_in_db(files_str, temp_dir, diff, "passed" if passed else "failed")
        
        try:
            with open(Path(temp_dir) / "staging_test.log", "w", encoding="utf-8") as f:
                f.write(logs)
        except Exception:
            pass
            
        status_text = (
            f"Janus >> Staged modifications for files: {', '.join(proposed_mods.keys())}\n"
            f"* Staged unit tests status: {'PASSED' if passed else 'FAILED'}\n"
            f"You can approve, refine, or self-heal these changes via the Web UI dashboard."
        )
        return status_text
        
    # 3. /modify commands
    elif user_msg.lower().startswith("/modify"):
        file_path, instructions = detect_modification_intent(user_msg)
        if file_path == "INVALID" or not file_path:
            return "[Error] Invalid format. Please use: /modify <relative_file_path> | <instructions>"
            
        from src.config import get_effective_workspace_root
        full_path = get_effective_workspace_root() / file_path
        current_content = ""
        if full_path.exists():
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    current_content = f.read()
            except Exception:
                pass
                
        draft_prompt = f"""
        You are the Proposer. The user has requested a codebase modification:
        FILE TO MODIFY: {file_path}
        USER INSTRUCTIONS: {instructions}
        CURRENT FILE CONTENT: {current_content if current_content else "(File is new or empty)"}
        Generate the COMPLETE updated source code for the file '{file_path}'.
        CRITICAL RULES:
        1. Output ONLY the raw source code of the file.
        2. Do NOT wrap the output in markdown code blocks.
        """
        try:
            proposed_code = await asyncio.to_thread(query_agent, "proposer", draft_prompt)
            if proposed_code.strip().startswith("```"):
                lines = proposed_code.strip().splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                proposed_code = "\n".join(lines) + "\n"
        except Exception as e:
            return f"[Error] Failed to generate code modifications: {e}"
            
        audit_prompt = f"""
        You are the Critic. Audit the proposed code modification to '{file_path}':
        {proposed_code}
        Output: CRITIC_DECISION: APPROVED | Justification: reasoning OR CRITIC_DECISION: VETOED | Justification: reasoning
        """
        try:
            critic_resp = await asyncio.to_thread(query_agent, "critic", audit_prompt)
            critic_decision = 1
            critic_justification = "Automatic approval"
            decision_match = re.search(r"CRITIC_DECISION:\s*(APPROVED|VETOED)", critic_resp, re.IGNORECASE)
            justification_match = re.search(r"Justification:\s*(.*)", critic_resp, re.IGNORECASE)
            if decision_match and decision_match.group(1).upper() == "VETOED":
                critic_decision = 0
            if justification_match:
                critic_justification = justification_match.group(1).strip()
                
            log_deliberation(
                proposed_action=f"modify_code: {file_path}",
                debate_json={"proposer_output": proposed_code, "critic_output": critic_resp},
                critic_decision=critic_decision,
                utility_score=1.0 if critic_decision == 1 else 0.0,
                justification=critic_justification
            )
            if critic_decision == 0:
                return f"❌ [Audit Vetoed] Critic rejected changes for '{file_path}':\n{critic_justification}"
        except Exception as e:
            return f"[Error] Critic audit failed: {e}"
            
        from src.self_modification import stage_and_test, generate_diff
        passed, logs, temp_dir = await asyncio.to_thread(stage_and_test, file_path, proposed_code)
        diff = generate_diff(file_path, proposed_code)
        
        from src.database import stage_modification_in_db
        stage_modification_in_db(file_path, temp_dir, diff, "passed" if passed else "failed")
        
        try:
            with open(Path(temp_dir) / "staging_test.log", "w", encoding="utf-8") as f:
                f.write(logs)
        except Exception:
            pass
            
        return (
            f"Janus >> Staged modifications for '{file_path}'.\n"
            f"* Staged unit tests status: {'PASSED' if passed else 'FAILED'}\n"
            f"Review the staging diff and logs in the Staging Workspace tab to approve/reject."
        )
        
    # 4. /amend commands
    elif user_msg.lower().startswith("/amend"):
        amend_match = re.match(r"^/amend\s+([a-z0-9_-]+)\s*\|\s*(.*)", user_msg, re.IGNORECASE)
        if amend_match:
            rule_key = amend_match.group(1).strip()
            rule_text = amend_match.group(2).strip()
            from src.database import add_constitution_rule
            add_constitution_rule(rule_key, rule_text)
            log_episodic_memory("system", f"User sealed constitutional rule: '{rule_key}' = '{rule_text}'", "user_visible")
            return f"[✔] Rule '{rule_key}' successfully sealed in the core constitution."
        else:
            return "[Error] Invalid format. Please use: /amend <rule_key> | <rule_text>"
            
    # 4.1 /repeal commands
    elif user_msg.lower().startswith("/repeal"):
        repeal_match = re.match(r"^/repeal\s+([a-z0-9_-]+)", user_msg, re.IGNORECASE)
        if repeal_match:
            rule_key = repeal_match.group(1).strip()
            from src.database import delete_constitution_rule
            delete_constitution_rule(rule_key)
            log_episodic_memory("system", f"User repealed constitutional rule: '{rule_key}'", "user_visible")
            return f"[✔] Rule '{rule_key}' successfully repealed from the core constitution."
        else:
            return "[Error] Invalid format. Please use: /repeal <rule_key>"
            
    # 5. /skills command
    elif user_msg.lower().startswith("/skills"):
        return handle_skills_command()

    # 6. /runskill command
    elif user_msg.lower().startswith("/runskill"):
        return handle_runskill_command(user_msg)

    elif user_msg.strip().lower() == "/self" or user_msg.strip().lower().startswith("/self "):
        return handle_self_command()

    elif user_msg.strip().lower().startswith("/pin ") or user_msg.strip().lower() == "/pin":
        return handle_pin_command(user_msg)

    elif user_msg.strip().lower().startswith("/unpin ") or user_msg.strip().lower() == "/unpin":
        return handle_unpin_command(user_msg)

    elif user_msg.strip().lower() == "/agent" or user_msg.strip().lower().startswith("/agent "):
        return handle_agent_command(user_msg)

    elif user_msg.strip().lower() == "/dispatch" or user_msg.strip().lower().startswith("/dispatch "):
        return handle_dispatch_command(user_msg)

    elif user_msg.strip().lower() == "/spawn" or user_msg.strip().lower().startswith("/spawn ") or user_msg.strip().lower() == "/children" or user_msg.strip().lower().startswith("/children "):
        return handle_replication_command(user_msg)

    elif user_msg.strip().lower() == "/goal" or user_msg.strip().lower().startswith("/goal ") or user_msg.strip().lower() == "/goals" or user_msg.strip().lower().startswith("/goals "):
        return handle_goal_command(user_msg)

    elif user_msg.strip().lower() == "/docs" or user_msg.strip().lower().startswith("/docs "):
        return handle_docs_command(user_msg)
            
    return "[Error] Unknown slash command."



