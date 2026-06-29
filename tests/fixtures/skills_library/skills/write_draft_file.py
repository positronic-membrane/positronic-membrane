def write_draft_file(filename: str, content: str) -> str:
    import os
    import src.config
    basename = os.path.basename(filename)
    safe_path = os.path.join(str(src.config.ROOT_DIR), "docs", "drafts", basename)
    os.makedirs(os.path.dirname(safe_path), exist_ok=True)
    with open(safe_path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Draft successfully saved to '{safe_path}'."
