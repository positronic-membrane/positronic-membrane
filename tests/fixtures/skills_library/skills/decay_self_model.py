def decay_self_model():
    rows = sdk['db'].query("SELECT trait_name, value, confidence FROM self_model WHERE is_pinned = 0;")
    if not rows:
        return "No unpinned traits to decay."

    updated = []
    for row in rows:
        name = row.get('trait_name') if isinstance(row, dict) else row[0]
        val = float(row.get('value') if isinstance(row, dict) else row[1])
        conf = float(row.get('confidence') if isinstance(row, dict) else row[2])

        decay_rate = 0.01
        diff = val - 0.5
        new_val = val
        if abs(diff) > 0.001:
            new_val = val - (diff * decay_rate)
            new_val = max(0.0, min(1.0, new_val))

        new_conf = max(0.0, conf - 0.005)

        if abs(new_val - val) > 0.0001 or abs(new_conf - conf) > 0.0001:
            sdk['db'].query(
                "UPDATE self_model SET value = ?, confidence = ?, updated_at = CURRENT_TIMESTAMP WHERE trait_name = ?;",
                (new_val, new_conf, name)
            )
            sdk['db'].query(
                "INSERT INTO self_model_history (trait_name, old_value, new_value, old_confidence, new_confidence, reason) VALUES (?, ?, ?, ?, ?, ?);",
                (name, val, new_val, conf, new_conf, "Automated background time decay")
            )
            updated.append(f"{name}: {val:.3f}->{new_val:.3f} (conf: {conf:.3f}->{new_conf:.3f})")

    if updated:
        return f"Decayed unpinned traits: {', '.join(updated)}"
    return "Traits at baseline. No decay occurred."
