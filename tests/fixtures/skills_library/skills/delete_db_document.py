def delete_db_document(doc_title: str) -> str:
    success = sdk['documents'].delete(doc_title)
    if not success:
        return f"[Error] Database document '{doc_title}' not found."
    return f"Successfully deleted database document '{doc_title}'."
