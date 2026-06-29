def evaluate_drives():
    rows = sdk['db'].query("SELECT config_value FROM system_config WHERE config_key = 'user_presence_status';")
    status = "idle"
    if rows:
        if isinstance(rows[0], dict):
            status = rows[0].get("config_value", "idle")
        elif isinstance(rows[0], (list, tuple)):
            status = rows[0][0]
            
    thresh_rows = sdk['db'].query("SELECT config_value FROM system_config WHERE config_key = 'boredom_threshold';")
    threshold = 5
    if thresh_rows:
        try:
            val = thresh_rows[0].get("config_value") if isinstance(thresh_rows[0], dict) else thresh_rows[0][0]
            threshold = int(val)
        except Exception:
            pass
            
    if status == "idle":
        b = sdk['drives'].increment("boredom", 1)
        if b >= threshold:
            sdk['drives'].set("boredom", 0)
            sdk['swarm'].trigger_reflection()
            return f"Boredom threshold met ({b}>={threshold}). Swarm reflection triggered."
        return f"Boredom incremented to {b}/{threshold}."
    else:
        return "User active. Boredom not incremented."
