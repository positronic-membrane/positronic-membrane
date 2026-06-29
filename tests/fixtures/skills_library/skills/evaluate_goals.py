def evaluate_goals():
    rows = sdk['db'].query("SELECT id, type, status, description FROM goals WHERE status IN ('active', 'in_progress');")
    if not rows:
        return "No active goals to evaluate."

    updated = []
    for row in rows:
        gid = row.get('id') if isinstance(row, dict) else row[0]
        gtype = row.get('type') if isinstance(row, dict) else row[1]
        gdesc = row.get('description') if isinstance(row, dict) else row[3]
        
        # Don't auto-complete aspirational goals
        if gtype == 'aspirational':
            continue
            
        # Check checkpoints for this goal
        cps = sdk['db'].query("SELECT id, achieved FROM goal_checkpoints WHERE goal_id = ?;", (gid,))
        if cps:
            # If all are achieved
            all_done = True
            for cp in cps:
                ach = cp.get('achieved') if isinstance(cp, dict) else cp[1]
                if not ach:
                    all_done = False
                    break
            
            if all_done:
                sdk['db'].query("UPDATE goals SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (gid,))
                # Log episodic memory
                sdk['db'].query(
                    "INSERT INTO episodic_memory (speaker, message_content, context_type) "
                    "VALUES ('system', ?, 'background_thought');",
                    (f"Autonomous Goal Achievement: Goal [{gid}] '{gdesc}' has been completed.",)
                )
                updated.append(f"Goal [{gid}]")

    if updated:
        return f"Evaluated goals. Completed: {', '.join(updated)}"
    return "Evaluated goals. No status transitions occurred."
