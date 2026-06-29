def checkout_db_to_draft(doc_title: str, filename: str) -> str:
    import os
    import src.config
    basename = os.path.basename(filename)
    safe_path = os.path.join(str(src.config.ROOT_DIR), "docs", "drafts", basename)
    
    doc = sdk['documents'].get(doc_title)
    if not doc:
        return f"[Error] Database document '{doc_title}' not found."
    
    os.makedirs(os.path.dirname(safe_path), exist_ok=True)
    with open(safe_path, "w", encoding="utf-8") as f:
        f.write(doc['content'])
    return f"Successfully checked out DB document '{doc_title}' to local file '{safe_path}'."
