def commit_draft_to_db(
    filename: str, doc_title: str, tags: list = None, purpose: str = "memory", metadata: dict = None
) -> str:
    import os
    import src.config
    basename = os.path.basename(filename)
    safe_path = os.path.join(str(src.config.ROOT_DIR), "docs", "drafts", basename)
    if not os.path.exists(safe_path):
        return f"[Error] Local draft file '{safe_path}' not found."
    with open(safe_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    sdk['documents'].upsert(doc_title, content, tags, purpose=purpose, metadata=metadata)
    chars = len(content)
    return f"Successfully committed draft '{basename}' to DB document '{doc_title}' as '{purpose}' ({chars} chars)."
