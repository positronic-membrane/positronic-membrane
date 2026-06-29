def cleanup_episodic_memory():
    # 1. Fetch memory.retention_days
    config_row = sdk['db'].query("SELECT config_value FROM system_config WHERE config_key = 'memory.retention_days';")
    retention_days = 30
    if config_row:
        try:
            val = config_row[0].get('config_value') if isinstance(config_row[0], dict) else config_row[0][0]
            retention_days = int(val)
        except Exception:
            pass

    # 2. Prevent daily duplicates by checking last run time
    last_run_row = sdk['db'].query("SELECT config_value FROM system_config WHERE config_key = 'memory.last_cleanup_time';")
    import datetime
    now_str = datetime.datetime.utcnow().isoformat()
    if last_run_row:
        try:
            last_run_val = last_run_row[0].get('config_value') if isinstance(last_run_row[0], dict) else last_run_row[0][0]
            if last_run_val:
                last_run_time = datetime.datetime.fromisoformat(last_run_val)
                if (datetime.datetime.utcnow() - last_run_time).total_seconds() < 86400:
                    return f"Episodic memory cleanup skipped. Last run was at {last_run_val}."
        except Exception:
            pass

    # 3. Purge expired memories
    sdk['db'].query(
        "DELETE FROM episodic_memory WHERE timestamp < datetime('now', '-' || ? || ' days');",
        (retention_days,)
    )

    # 4. Save last run time
    sdk['db'].query(
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) VALUES ('memory.last_cleanup_time', ?, 1);",
        (now_str,)
    )
    return f"Episodic memory cleanup complete. Purged memories older than {retention_days} days."
