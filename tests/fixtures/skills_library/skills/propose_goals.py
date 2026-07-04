def propose_goals():
    """Generate subconscious goal proposals grounded in active curiosity context.

    Invoked by evaluate_drives when the boredom threshold fires (the same event
    that triggers a swarm reflection cycle), or manually via /runskill. Proposals
    are inserted via sdk['goals'].propose_goal() with status='proposed' and wait
    for human ratification (/goal approve|reject) — nothing is auto-activated.

    Budget guard: generation is skipped while goal_proposal.max_open_proposals
    (system_config, default 3) proposals are already pending review; 0 disables
    generation entirely.
    """
    import json
    import re

    VALID_TYPES = ("short", "long", "stretch", "aspirational")

    def _scalar(row, key):
        if isinstance(row, dict):
            return row.get(key)
        return row[0]

    # --- Rate/budget guard ---------------------------------------------------
    max_open = 3
    rows = sdk['db'].query(
        "SELECT config_value FROM system_config WHERE config_key = 'goal_proposal.max_open_proposals';"
    )
    if rows:
        try:
            max_open = int(_scalar(rows[0], "config_value"))
        except (TypeError, ValueError):
            pass

    if max_open <= 0:
        return "Goal proposal generation is disabled (goal_proposal.max_open_proposals <= 0)."

    pending_rows = sdk['db'].query("SELECT description FROM goal_proposals WHERE status = 'proposed';")
    open_count = len(pending_rows)
    if open_count >= max_open:
        return (
            f"Skipped goal proposal generation: {open_count} proposal(s) already "
            f"pending review (cap: {max_open})."
        )
    slots = max_open - open_count

    # --- Curiosity / context gathering ---------------------------------------
    try:
        curiosity = sdk['memory'].get_active_curiosity_topics(limit=5)
    except Exception as e:
        sdk['logger'].error(f"Failed to query semantic curiosity: {e}")
        curiosity = []
    if not curiosity:
        try:
            curiosity = sdk['drives'].get_curiosity_vector()
        except Exception:
            curiosity = []

    def _rows_to_lines(rows, fmt):
        lines = []
        for row in rows:
            try:
                lines.append(fmt(row))
            except Exception:
                continue
        return lines

    active_goals = _rows_to_lines(
        sdk['db'].query("SELECT type, description FROM goals WHERE status IN ('active', 'in_progress');"),
        lambda r: "- [{}] {}".format(
            r.get('type') if isinstance(r, dict) else r[0],
            r.get('description') if isinstance(r, dict) else r[1],
        ),
    )
    pending_proposals = _rows_to_lines(
        pending_rows,
        lambda r: "- {}".format(r.get('description') if isinstance(r, dict) else r[0]),
    )

    memory_summary = ""
    try:
        memories = sdk['memory'].get_recent_episodic_memories(limit=5)
        memory_summary = "\n".join(f"[{ts}] {spk}: {msg}" for spk, msg, ts in reversed(memories))
    except Exception as e:
        sdk['logger'].error(f"Failed to fetch recent episodic memories: {e}")

    prompt = f"""
    You are the Proposer. The system is idle and its boredom drive has fired. Review what we have
    actually been exploring and propose up to {slots} new candidate goals for human ratification.

    ACTIVE CURIOSITY TOPICS:
    {", ".join(str(t) for t in curiosity) if curiosity else "None recorded."}

    CURRENT ACTIVE GOALS (do not duplicate these):
    {chr(10).join(active_goals) if active_goals else "None."}

    PROPOSALS ALREADY PENDING REVIEW (do not duplicate these):
    {chr(10).join(pending_proposals) if pending_proposals else "None."}

    RECENT EPISODIC MEMORIES:
    {memory_summary if memory_summary else "None available."}

    Each goal must be grounded in the curiosity topics or recent activity above — do not invent
    unrelated ambitions. Respond ONLY with a JSON array, no prose before or after, in this format:
    [{{"type": "short|long|stretch|aspirational", "description": "<one-sentence goal>",
       "confidence": 0.0-1.0, "source_reason": "<which curiosity topic or observation motivated this>"}}]
    """

    try:
        response = sdk['swarm'].query_agent("proposer", prompt)
    except Exception as e:
        sdk['logger'].error(f"Goal proposal LLM call failed: {e}")
        return f"Goal proposal generation failed: LLM call error: {e}"

    # --- Parse & insert (tolerate malformed LLM output) -----------------------
    match = re.search(r"\[.*\]", response or "", re.DOTALL)
    if not match:
        sdk['logger'].warning(f"No JSON array found in proposer response: '{response}'")
        return "Goal proposal generation skipped: proposer response contained no parseable JSON array."
    try:
        candidates = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError) as e:
        sdk['logger'].warning(f"Failed to parse proposer JSON: {e}. Response: '{response}'")
        return "Goal proposal generation skipped: proposer response was not valid JSON."
    if isinstance(candidates, dict):
        candidates = [candidates]
    if not isinstance(candidates, list):
        return "Goal proposal generation skipped: proposer JSON was not an array."

    created = []
    skipped = 0
    for cand in candidates:
        if len(created) >= slots:
            break
        if not isinstance(cand, dict):
            skipped += 1
            continue
        gtype = str(cand.get("type", "")).strip().lower()
        description = str(cand.get("description", "")).strip()
        if gtype not in VALID_TYPES or not description:
            skipped += 1
            continue
        try:
            confidence = float(cand.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        source_reason = str(cand.get("source_reason", "")).strip() or "Subconscious proposal from idle reflection."
        try:
            pid = sdk['goals'].propose_goal(gtype, description, confidence, source_reason)
            created.append(pid)
        except Exception as e:
            sdk['logger'].error(f"Failed to insert goal proposal '{description}': {e}")
            skipped += 1

    if created:
        sdk['memory'].log_episodic_memory(
            speaker="system",
            message_content=(
                f"Subconscious goal proposal: generated {len(created)} candidate goal(s) "
                f"(proposal IDs: {created}) from active curiosity context. Awaiting "
                f"ratification via /goal approve|reject."
            ),
            context_type="background_thought",
        )
        return f"Generated {len(created)} goal proposal(s) (IDs: {created}); skipped {skipped} invalid candidate(s)."
    return f"No valid goal proposals generated ({skipped} candidate(s) rejected during validation)."
