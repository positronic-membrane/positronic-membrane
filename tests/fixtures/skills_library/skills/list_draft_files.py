def list_draft_files() -> str:
    import os
    import src.config
    drafts_path = os.path.join(str(src.config.ROOT_DIR), "docs", "drafts")
    if not os.path.exists(drafts_path):
        return "Drafts directory does not exist."
    files = [f for f in os.listdir(drafts_path) if os.path.isfile(os.path.join(drafts_path, f))]
    if not files:
        return "No draft files found in docs/drafts/."
    return "Draft files:\n" + "\n".join([f"- {f}" for f in sorted(files)])
