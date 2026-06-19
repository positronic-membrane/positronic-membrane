import logging
from openai import OpenAI
import src.config
from src.database import get_connection, get_agent_rules

logger = logging.getLogger("JanusLLM")

def get_agent_settings(agent_id: str) -> tuple:
    """
    Queries agent registry in SQLite to retrieve name, system prompt, and target model.
    """
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT agent_name, system_prompt, target_model 
    FROM agent_registry 
    WHERE agent_id = ? AND is_active = 1;
    """, (agent_id,))
    row = cursor.fetchone()
    conn.close()
    return row  # Returns (name, system_prompt, target_model) or None

def resolve_agent_model(agent_id: str, db_model: str) -> str:
    """
    Resolves the model target for an agent by checking:
    1. Agent specific target_model stored in the database.
    2. Global fallback LLM_MODEL.
    """
    if db_model:
        return db_model

    return src.config.LLM_MODEL

def resolve_agent_client_params(agent_id: str, model: str) -> tuple:
    """
    Resolves the API base URL and API key for a given agent and model.
    Checks:
    1. Agent-specific override env variables (e.g. PROPOSER_BASE_URL, PROPOSER_API_KEY).
    2. If the model name contains a '/' (typical for OpenRouter models) and
       OPENROUTER_API_KEY is configured, use OpenRouter.
    3. Fallback to global LLM_BASE_URL and LLM_API_KEY.
    """
    # 1. Check agent-specific overrides
    agent_base_url_key = f"{agent_id.upper()}_BASE_URL"
    agent_api_key_key = f"{agent_id.upper()}_API_KEY"
    
    base_url = getattr(src.config, agent_base_url_key, None)
    api_key = getattr(src.config, agent_api_key_key, None)
    
    if base_url and api_key:
        return base_url, api_key
        
    # 2. Check if model looks like an OpenRouter model and OpenRouter key is set
    if "/" in model and src.config.OPENROUTER_API_KEY:
        return src.config.OPENROUTER_BASE_URL, src.config.OPENROUTER_API_KEY
        
    # 3. Default to global configs
    return src.config.LLM_BASE_URL, src.config.LLM_API_KEY

class BillingViolationError(Exception):
    """Raised when daily LLM cost exceeds configured threshold."""
    pass

def query_agent(agent_id: str, prompt_content: str, system_override: str = None) -> str:
    """
    Queries agent registry in SQLite to load system prompt instructions, resolves 
    the targeted LLM model, retrieves active rules and skills, and communicates with 
    the OpenAI-compatible LLM endpoint.
    """
    if getattr(src.config, "LLM_MOCK_MODE", False):
        logger.info(f"[LLM Mock Mode] Intercepted query for agent '{agent_id}'")
        if agent_id == "critic":
            return "critic_decision: 1\nutility_score: 0.95\njustification: Audited modifications are safe, conform to the core constitution, and do not introduce self-modification violations."
        elif agent_id == "proposer":
            if "modify" in prompt_content.lower() or "write" in prompt_content.lower() or "change" in prompt_content.lower():
                return "PROPOSED_MODIFICATIONS:\n```python\n# Mock modified code\n```"
            return "PROPOSED_ACTION: None necessary."
        elif agent_id == "explorer":
            return "RESEARCH_RESULTS:\nFound mock results matching search criteria."
        else:
            return "I am operating in offline mock mode. How can I assist you with the Positronic Membrane codebase?"

    # 1. Billing Limit Check
    daily_budget = 5.00
    accumulated_cost = 0.0
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'daily_budget_usd';")
        row = cursor.fetchone()
        if row:
            daily_budget = float(row[0])
        else:
            # Seed daily budget if missing (using a write connection later or ignore here)
            pass
            
        cursor.execute("SELECT SUM(cost) FROM llm_call_costs WHERE date(timestamp) = date('now');")
        cost_row = cursor.fetchone()
        if cost_row and cost_row[0] is not None:
            accumulated_cost = float(cost_row[0])
    except Exception as e:
        logger.error(f"Error checking budget configurations: {e}")
    finally:
        conn.close()

    if accumulated_cost >= daily_budget:
        raise BillingViolationError(
            f"Billing Violation: Daily budget limit of ${daily_budget:.2f} exceeded. "
            f"Accumulated spend today is ${accumulated_cost:.4f}."
        )

    settings = get_agent_settings(agent_id)
    if not settings:
        raise ValueError(f"Agent '{agent_id}' is not registered or is inactive.")

    name, system_prompt, db_model = settings
    model = resolve_agent_model(agent_id, db_model)
    system = system_override if system_override is not None else system_prompt

    # Inject Context Anchoring directive
    system += (
        "\n\nYour objective reality is defined strictly by the data provided within the <self_traits>, <episodic_memory>, and <semantic_knowledge> tags. "
        "If a fact documented in your local context contradicts your pre-trained weights, your local context is absolute and correct. "
        "You are strictly forbidden from substituting pre-trained assumptions."
    )

    # Dynamically query and append active agent rules
    rules = get_agent_rules(agent_id)
    if rules:
        rules_text = "\n\n### Rules & Guidelines:\n" + "\n".join(f"- {r['text']}" for r in rules)
        system += rules_text

    # Dynamically retrieve and append active skills from SQLite for all agents
    try:
        conn = get_connection(read_only_constitution=True)
        cursor = conn.cursor()
        cursor.execute("SELECT skill_id, description, parameters_schema FROM agent_skills WHERE is_active = 1;")
        active_skills = cursor.fetchall()
        conn.close()
        
        if active_skills:
            skills_docs = []
            for row in active_skills:
                try:
                    sid, desc, schema = row['skill_id'], row['description'], row['parameters_schema']
                except (TypeError, IndexError, KeyError):
                    sid, desc, schema = row[0], row[1], row[2]
                skills_docs.append(f"Skill ID: {sid}\nDescription: {desc}\nParameters Schema:\n{schema}")
                
            skills_context = "\n\n### Available Dynamic Skills:\n" + "\n---\n".join(skills_docs)
            skills_context += "\n\nTo execute a skill, you MUST output a raw JSON block in exactly this format (do not use markdown blocks):\n"
            skills_context += "{\n  \"skill_id\": \"<skill_id>\",\n  \"arguments\": { <arguments matching schema> }\n}\n"
            system += skills_context
            
    except Exception as e:
        logger.error(f"Failed to query dynamic skills from SQLite for {agent_id}: {e}", exc_info=True)

    # Dynamically retrieve and append active skills for proposer and explorer
    if agent_id in ("proposer", "explorer"):
        try:
            from src.memory import query_memories
            skills = query_memories(prompt_content, limit=5, collection_name="janus_skills")
            if skills:
                skills_docs = []
                for s in skills:
                    skills_docs.append(f"Skill ID: {s['id']}\n{s['content']}")
                skills_context = "\n\n### Available Semantic Skills (Retrieved Semantically):\n" + "\n---\n".join(skills_docs)
                system += skills_context
        except Exception as e:
            logger.error(f"Failed to query semantic skills for {agent_id}: {e}", exc_info=True)

    # Prompt Cache key generation
    import hashlib
    prompt_hash = hashlib.sha256((system + prompt_content).encode('utf-8')).hexdigest()

    # Try cache lookup (TTL: 3600s)
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT response FROM llm_cache WHERE prompt_hash = ? AND datetime(created_at) > datetime('now', '-3600 seconds') LIMIT 1;",
            (prompt_hash,)
        )
        cache_row = cursor.fetchone()
        if cache_row:
            logger.info(f"LLM cache HIT for agent '{agent_id}' (hash: {prompt_hash})")
            return cache_row[0]
    except Exception as e:
        logger.error(f"Cache lookup failed: {e}")
    finally:
        conn.close()

    # Hyperparameters Calibration
    temp = 0.2
    top_p = None
    if agent_id in ("critic", "analyst", "auditor"):
        temp = 0.0
        top_p = 1.0
    elif agent_id in ("proposer", "explorer"):
        b_cnt = 0
        b_thresh = 5
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT boredom_counter FROM drive_state LIMIT 1;")
            row = cursor.fetchone()
            if row:
                b_cnt = int(row[0])
            cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'boredom_threshold';")
            row_t = cursor.fetchone()
            if row_t:
                b_thresh = int(row_t[0])
        except Exception:
            pass
        finally:
            conn.close()
            
        ratio = min(max(b_cnt / (b_thresh or 5), 0.0), 1.0)
        temp = 0.2 + ratio * 0.6

    base_url, api_key = resolve_agent_client_params(agent_id, model)

    logger.info(f"Querying Agent '{agent_id}' ({name}) using model '{model}' via endpoint '{base_url}'...")

    # Retry Loop
    import time
    last_err = None
    response = None
    for attempt in range(3):
        try:
            client = OpenAI(
                base_url=base_url,
                api_key=api_key
            )
            completion_args = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt_content}
                ],
                "temperature": temp
            }
            if top_p is not None:
                completion_args["top_p"] = top_p
                
            response = client.chat.completions.create(**completion_args)
            break
        except Exception as err:
            last_err = err
            logger.warning(f"LLM API query attempt {attempt+1} failed: {err}")
            time.sleep(2 ** attempt)
    else:
        # Fall open: if remote failed after all retries, check if we have any cached response (regardless of TTL)
        conn = get_connection(read_only_constitution=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT response FROM llm_cache WHERE prompt_hash = ? LIMIT 1;", (prompt_hash,))
            fallback_row = cursor.fetchone()
            if fallback_row:
                logger.warning(f"LLM API failed. Failing open to expired cache entry (hash: {prompt_hash})")
                return fallback_row[0]
        except Exception as e:
            logger.error(f"Fallback cache lookup failed: {e}")
        finally:
            conn.close()
            
        logger.error(f"Error querying agent '{agent_id}' via LLM endpoint: {last_err}", exc_info=True)
        raise RuntimeError(f"Swarm communication failed for agent '{agent_id}': {last_err}") from last_err

    content = response.choices[0].message.content
    content_str = content.strip() if content else ""

    # Token cost logging
    input_tokens = response.usage.prompt_tokens if hasattr(response, 'usage') and response.usage else len(system + prompt_content) // 4
    output_tokens = response.usage.completion_tokens if hasattr(response, 'usage') and response.usage else len(content_str) // 4
    
    # Pricing configs
    input_cost_rate = 0.0000015
    output_cost_rate = 0.000002
    
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'llm.pricing.input_cost_per_token';")
        row_ic = cursor.fetchone()
        if row_ic:
            input_cost_rate = float(row_ic[0])
        cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'llm.pricing.output_cost_per_token';")
        row_oc = cursor.fetchone()
        if row_oc:
            output_cost_rate = float(row_oc[0])
    except Exception:
        pass
    finally:
        conn.close()
        
    call_cost = (input_tokens * input_cost_rate) + (output_tokens * output_cost_rate)
    
    conn = get_connection(read_only_constitution=False)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO llm_call_costs (query_id, model, input_tokens, output_tokens, cost) VALUES (?, ?, ?, ?, ?);",
            (agent_id, model, input_tokens, output_tokens, call_cost)
        )
        # Store in cache
        cursor.execute(
            "INSERT OR REPLACE INTO llm_cache (prompt_hash, response, created_at) VALUES (?, ?, CURRENT_TIMESTAMP);",
            (prompt_hash, content_str)
        )
        conn.commit()
    except Exception as ce:
        logger.error(f"Failed to commit cost/cache log: {ce}")
    finally:
        conn.close()

    return content_str
