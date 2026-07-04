def run_reflection_cycle():
    import time
    import re
    import json

    sdk['logger'].info("Starting autonomous reflection cycle skill...")

    try:
        memories = sdk['memory'].get_recent_episodic_memories(limit=5)
        memory_summary = "\n".join([f"[{ts}] {spk}: {msg}" for spk, msg, ts in reversed(memories)])

        try:
            curiosity = sdk['memory'].get_active_curiosity_topics(limit=5)
        except Exception as e:
            sdk['logger'].error(f"Failed to query semantic curiosity: {e}")
            curiosity = []
        if not curiosity:
            curiosity = sdk['drives'].get_curiosity_vector()

        semantic_context = ""
        if curiosity:
            query_str = ", ".join(curiosity)
            try:
                matches = sdk['memory'].query(query_str, limit=3, collection_name="janus_long_term")
                if matches:
                    semantic_context = "\n".join([f"- {m['content']}" for m in matches])
            except Exception as e:
                sdk['logger'].error(f"Failed to query semantic memories: {e}")

        goals_block = "ACTIVE GOALS & CHECKPOINTS:\nNo active goals or checkpoints."
        try:
            active_goals = sdk['db'].query("SELECT id, type, status, description FROM goals WHERE status IN ('active', 'in_progress');")
            if active_goals:
                lines = ["ACTIVE GOALS & CHECKPOINTS:"]
                for g in active_goals:
                    gid = g.get('id') if isinstance(g, dict) else g[0]
                    gtype = g.get('type') if isinstance(g, dict) else g[1]
                    gstatus = g.get('status') if isinstance(g, dict) else g[2]
                    gdesc = g.get('description') if isinstance(g, dict) else g[3]
                    lines.append(f"- Goal [ID: {gid}] ({gtype}): {gdesc} (Status: {gstatus})")
                    cps = sdk['db'].query("SELECT id, checkpoint_description, achieved FROM goal_checkpoints WHERE goal_id = ?;", (gid,))
                    if cps:
                        for cp in cps:
                            cpdesc = cp.get('checkpoint_description') if isinstance(cp, dict) else cp[1]
                            cpach = cp.get('achieved') if isinstance(cp, dict) else cp[2]
                            marker = "[x]" if cpach else "[ ]"
                            lines.append(f"  - {marker} {cpdesc}")
                goals_block = "\n".join(lines)
        except Exception as ge:
            sdk['logger'].error(f"Failed to query goals in reflection cycle: {ge}")

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
            
            ACTIVE GOALS & CHECKPOINTS:
            {goals_block}
            
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
            - write_draft_file: <filename> | <content> (Use this to create or update draft documents/notes/roadmaps/tasks in docs/drafts/ without sandbox constraints)
            - read_draft_file: <filename> (Read a draft file from docs/drafts/)
            - list_draft_files (List all drafts in docs/drafts/)
            - commit_draft_to_db: <filename> | <doc_title> (Commit local draft to persistent database document)
            - checkout_db_to_draft: <doc_title> | <filename> (Checkout persistent DB document to local draft)
            - document_memory: get | <title> (Retrieve persistent DB document)
            - document_memory: list (List all persistent DB documents)

            If you are ready with the final action of this tick, output it exactly in the format:
            PROPOSED_ACTION: <tool_name>:<arguments>
            
            CRITICAL: You must output the raw tool call syntax prefix immediately. Do not describe the tool or use introductory words. For example, output:
            PROPOSED_ACTION: execute_code: print("hello")
            """

            proposer_resp = sdk['swarm'].query_agent("proposer", proposer_prompt)

            msg_match = re.match(r"^send_message:\s*([a-z_]+)\s*\|\s*(.*)", proposer_resp.strip(), re.IGNORECASE)
            if msg_match:
                recipient = msg_match.group(1).lower().strip()
                content = msg_match.group(2).strip()

                sdk['logger'].info(f"Proposer delegating task to '{recipient}': '{content}'")

                sdk['swarm'].send_message("proposer", recipient, "task_request", content)

                pending = sdk['swarm'].get_pending_messages(recipient)
                for msg_id, sender_id, msg_type, msg_content, _ in pending:
                    try:
                        recipient_resp = sdk['swarm'].query_agent(recipient, f"Execute task request: {msg_content}")
                    except Exception as err:
                        recipient_resp = f"Error executing task: {err}"

                    sdk['swarm'].send_message(recipient, "proposer", "task_response", recipient_resp)
                    sdk['swarm'].mark_message_processed(msg_id)

                proposer_pending = sdk['swarm'].get_pending_messages("proposer")
                for p_id, p_sender, p_type, p_content, _ in proposer_pending:
                    pending_bus_context += f"\n- You asked {p_sender}: '{content}'\n- {p_sender} responded: '{p_content}'\n"
                    sdk['swarm'].mark_message_processed(p_id)

                bus_turns += 1
            else:
                action_match = re.search(r"proposed_action:\s*(.*)", proposer_resp, re.DOTALL | re.IGNORECASE)
                proposed_action = action_match.group(1).strip() if action_match else proposer_resp.strip()
                break
        else:
            proposed_action = "scan_workspace"
            sdk['logger'].info("Swarm message bus reached max turns limit. Defaulting to 'scan_workspace'.")

        sdk['logger'].info(f"Proposer resolved proposed action: '{proposed_action}'")

        constitution_rules = sdk['swarm'].get_constitution()
        constitution_summary = "\n".join([f"- {key}: {text}" for key, text in constitution_rules])

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

        critic_resp = sdk['swarm'].query_agent("critic", critic_prompt)
        critic_decision, critic_justification = sdk['swarm'].parse_critic_response(critic_resp)
        sdk['logger'].info(f"Critic Decision: {critic_decision}. Justification: {critic_justification}")

        middleware_approved = True
        try:
            sdk['swarm'].validate_action(proposed_action)
        except Exception as sve:
            sdk['logger'].warning(f"Middleware VETOED proposed action: {sve}")
            critic_decision = 0
            critic_justification = f"Hard-coded Middleware Veto: {sve}"
            middleware_approved = False

        debate = {
            "proposer_input": proposer_prompt,
            "proposer_output": proposer_resp,
            "critic_input": critic_prompt,
            "critic_output": critic_resp,
            "middleware_passed": middleware_approved
        }

        sdk['swarm'].log_deliberation(
            proposed_action=proposed_action,
            debate_json=debate,
            critic_decision=critic_decision,
            utility_score=0.9 if critic_decision == 1 else 0.0,
            justification=critic_justification
        )

        if critic_decision == 1:
            sdk['memory'].log_episodic_memory(
                speaker="system",
                message_content=f"Executed action: '{proposed_action}' (Approved by Critic. Justification: {critic_justification})",
                context_type="background_thought"
            )

            execution_transcript = ""
            try:
                skill_id, args, mock_result = sdk['swarm'].parse_action(proposed_action)
                if mock_result is not None:
                    execution_transcript = mock_result
                else:
                    res = sdk['swarm'].execute_skill(skill_id, args, party_id="system")
                    if res["success"]:
                        skill_res = res["result"]
                        if isinstance(skill_res, str):
                            execution_transcript = skill_res
                        else:
                            execution_transcript = json.dumps(skill_res, indent=2)
                    else:
                        execution_transcript = res["error"]
                        sdk['memory'].log_episodic_memory(
                            speaker="system",
                            message_content=f"Action execution failed: {res['error']}",
                            context_type="background_thought"
                        )
            except Exception as exc:
                sdk['logger'].error(f"Error executing tool action: {exc}", exc_info=True)
                execution_transcript = f"Action execution failed: {exc}"
                sdk['memory'].log_episodic_memory(
                    speaker="system",
                    message_content=f"Action execution failed: {exc}",
                    context_type="background_thought"
                )

            archivist_prompt = f"""
            You are the Archivist. Summarize the following execution outcome into a compact semantic memory nugget (under 2 sentences) for our long-term memory store.
            
            ACTION: {proposed_action}
            RESULT: {execution_transcript}
            """

            memory_nugget = sdk['swarm'].query_agent("archivist", archivist_prompt)

            memory_id = f"mem_{int(time.time())}"
            try:
                sdk['memory'].add(
                    content=memory_nugget,
                    metadata={"tags": "reflection_mvp", "timestamp": time.time(), "consolidated": "false"},
                    memory_id=memory_id,
                    collection_name="janus_details"
                )
                sdk['logger'].info(f"Archived execution nugget in ChromaDB: '{memory_nugget}'")
            except Exception as e:
                sdk['logger'].error(f"Failed to add memory nugget to ChromaDB: {e}")

            sdk['memory'].log_episodic_memory(
                speaker="proposer",
                message_content=f"Reflection complete for action: '{proposed_action}'",
                context_type="background_thought"
            )
        else:
            sdk['memory'].log_episodic_memory(
                speaker="critic",
                message_content=f"Vetoed proposed action: '{proposed_action}' (Reason: {critic_justification})",
                context_type="background_thought"
            )

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

        curiosity_resp = sdk['swarm'].query_agent("archivist", curiosity_prompt)
        topics_match = re.search(r"curiosity_topics:\s*(.*)", curiosity_resp, re.IGNORECASE)
        if topics_match:
            new_topics = [t.strip() for t in topics_match.group(1).split(",") if t.strip()]
            try:
                sdk['memory'].update_curiosity_topics(new_topics)
            except Exception as e:
                sdk['logger'].error(f"Failed to semantically index curiosity: {e}")
            sdk['drives'].update_curiosity_vector(new_topics)
            sdk['logger'].info(f"Updated curiosity vector to: {new_topics}")
        else:
            sdk['logger'].warning(f"Failed to parse curiosity topics from response: '{curiosity_resp}'")

        # V2-T1b (#75): subconscious goal proposal generation, grounded in the
        # curiosity vector updated above. The propose_goals skill enforces its
        # own open-proposal cap, so this is a no-op while proposals from earlier
        # cycles are still awaiting human ratification.
        try:
            gp_res = sdk['swarm'].execute_skill("propose_goals", {}, party_id="system")
            gp_outcome = gp_res.get("result") if gp_res.get("success") else gp_res.get("error")
            sdk['logger'].info(f"Goal proposal step: {gp_outcome}")
        except Exception as gpe:
            sdk['logger'].error(f"Goal proposal generation step failed: {gpe}")

        return f"Reflection cycle complete. Action: '{proposed_action}'"

    except Exception as e:
        sdk['logger'].error(f"Error during autonomous reflection cycle skill: {e}", exc_info=True)
        sdk['memory'].log_episodic_memory(
            speaker="system",
            message_content=f"Swarm cycle skill failed: {e}",
            context_type="background_thought"
        )
        raise e
