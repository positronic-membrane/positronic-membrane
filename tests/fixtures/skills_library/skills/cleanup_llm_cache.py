def cleanup_llm_cache():
    # 1. Fetch llm_cache.ttl_days
    config_row = sdk['db'].query("SELECT config_value FROM system_config WHERE config_key = 'llm_cache.ttl_days';")
    ttl_days = 7
    if config_row:
        try:
            val = config_row[0].get('config_value') if isinstance(config_row[0], dict) else config_row[0][0]
            ttl_days = int(val)
        except Exception:
            pass

    # 2. Prevent daily duplicates by checking last run time
    last_run_row = sdk['db'].query(
        "SELECT config_value FROM system_config WHERE config_key = 'llm_cache.last_cleanup_time';"
    )
    import datetime
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if last_run_row:
        try:
            row0 = last_run_row[0]
            last_run_val = row0.get('config_value') if isinstance(row0, dict) else row0[0]
            if last_run_val:
                last_run_time = datetime.datetime.fromisoformat(last_run_val)
                if last_run_time.tzinfo is None:
                    last_run_time = last_run_time.replace(tzinfo=datetime.timezone.utc)
                if (datetime.datetime.now(datetime.timezone.utc) - last_run_time).total_seconds() < 86400:
                    return f"LLM cache cleanup skipped. Last run was at {last_run_val}."
        except Exception:
            pass

    # 3. Purge expired cache rows
    sdk['db'].query(
        "DELETE FROM llm_cache WHERE created_at < datetime('now', '-' || ? || ' days');",
        (ttl_days,)
    )

    # 4. Save last run time
    sdk['db'].query(
        "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) "
        "VALUES ('llm_cache.last_cleanup_time', ?, 1);",
        (now_str,)
    )
    return f"LLM cache cleanup complete. Purged cache entries older than {ttl_days} days."
