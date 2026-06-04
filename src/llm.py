import logging
from openai import OpenAI
import src.config
from src.database import get_connection

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
    2. Role specific overrides in config (.env) (e.g. PROPOSER_MODEL, CRITIC_MODEL).
    3. Global fallback LLM_MODEL.
    """
    if db_model:
        return db_model

    # Check env config overrides
    env_override_key = f"{agent_id.upper()}_MODEL"
    override = getattr(src.config, env_override_key, None)
    if override:
        return override

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

def query_agent(agent_id: str, prompt_content: str, system_override: str = None) -> str:
    """
    Queries an agent dynamically from the database registry and calls
    the OpenAI-compatible LLM endpoint.
    """
    settings = get_agent_settings(agent_id)
    if not settings:
        raise ValueError(f"Agent '{agent_id}' is not registered or is inactive.")

    name, system_prompt, db_model = settings
    model = resolve_agent_model(agent_id, db_model)
    system = system_override if system_override is not None else system_prompt

    base_url, api_key = resolve_agent_client_params(agent_id, model)

    logger.info(f"Querying Agent '{agent_id}' ({name}) using model '{model}' via endpoint '{base_url}'...")

    try:
        # Create OpenAI-compatible client
        client = OpenAI(
            base_url=base_url,
            api_key=api_key
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt_content}
            ],
            temperature=0.2  # Low temperature for stable and logical structured output
        )

        content = response.choices[0].message.content
        if content:
            return content.strip()
        return ""

    except Exception as e:
        logger.error(f"Error querying agent '{agent_id}' via LLM endpoint: {e}", exc_info=True)
        raise RuntimeError(f"Swarm communication failed for agent '{agent_id}': {e}") from e
