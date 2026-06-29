def document_memory(
    action: str, title: str = None, tag_filter: str = None, purpose: str = None
) -> str:
    NL = chr(10)
    if action == "get":
        if not title:
            raise ValueError("title is required for action get.")
        doc = sdk['documents'].get(title)
        if not doc:
            return f"[Error] No document found with title '{title}'."
        tags_str = ", ".join(doc["tags"]) if doc["tags"] else "none"
        header = f"### {doc['title']}{NL}"
        meta = (
            f"**Purpose:** {doc['purpose']} | **Tags:** {tags_str} | "
            f"**Created:** {doc['created_at']} | **Updated:** {doc['updated_at']}{NL}{NL}"
        )
        return header + meta + doc["content"]
    elif action == "list":
        docs = sdk['documents'].list(tag_filter=tag_filter, purpose=purpose)
        if not docs:
            return "No documents stored yet."
        output = ["### Janus Documents", "| Title | Purpose | Tags | Updated |", "| --- | --- | --- | --- |"]
        for doc in docs:
            tags_str = ", ".join(doc["tags"]) if doc["tags"] else "-"
            output.append(f"| {doc['title']} | {doc['purpose']} | {tags_str} | {doc['updated_at']} |")
        return NL.join(output)
    else:
        raise ValueError(f"Unknown document action: '{action}'. Supported: get, list.")
