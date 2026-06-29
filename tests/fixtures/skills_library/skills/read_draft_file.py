def read_draft_file(filename: str) -> str:
    import os
    import src.config
    basename = os.path.basename(filename)
    safe_path = os.path.join(str(src.config.ROOT_DIR), "docs", "drafts", basename)
    if not os.path.exists(safe_path):
        return f"[Error] Draft file '{safe_path}' does not exist."
    with open(safe_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()
