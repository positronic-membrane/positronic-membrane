def spawn_agent(agent_id, name, prompt):
    sdk['swarm'].register_agent(agent_id.lower().strip(), name.strip(), prompt.strip())
    return f"Helper agent '{agent_id}' ({name}) successfully registered in agent_registry."
