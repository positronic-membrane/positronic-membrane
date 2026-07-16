import ast
import logging
import os
from pathlib import Path

import src.config
from src.memory import add_memory, get_collection, query_memories

logger = logging.getLogger("JanusCodebase")

def parse_python_structure(file_content: str) -> str:
    """
    Parses Python source code and extracts class names, methods,
    signatures, and top-level function names using Abstract Syntax Trees (AST).
    """
    try:
        tree = ast.parse(file_content)
    except SyntaxError:
        return "Python file containing syntax errors."

    summary = []
    module_doc = ast.get_docstring(tree)
    if module_doc:
        summary.append(f"Module Docstring: {module_doc.strip().splitlines()[0]}")

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            class_doc = ast.get_docstring(node)
            class_desc = f" - {class_doc.strip().splitlines()[0]}" if class_doc else ""
            summary.append(f"class {node.name}{class_desc}:")

            methods = []
            for subnode in node.body:
                if isinstance(subnode, ast.FunctionDef):
                    method_doc = ast.get_docstring(subnode)
                    method_desc = f" - {method_doc.strip().splitlines()[0]}" if method_doc else ""
                    try:
                        args_str = ast.unparse(subnode.args).strip()
                    except Exception:
                        args_str = "..."
                    methods.append(f"    * def {subnode.name}({args_str}){method_desc}")
            if methods:
                summary.extend(methods)
            else:
                summary.append("    * (no methods defined)")

        elif isinstance(node, ast.FunctionDef):
            func_doc = ast.get_docstring(node)
            func_desc = f" - {func_doc.strip().splitlines()[0]}" if func_doc else ""
            try:
                args_str = ast.unparse(node.args).strip()
            except Exception:
                args_str = "..."
            summary.append(f"def {node.name}({args_str}){func_desc}")

    return "\n".join(summary) if summary else "No classes or functions defined."

def generate_file_summary(file_path: Path) -> str:
    """
    Generates a structural or textual summary for a file.
    Uses AST for Python files and line snippets/headers for other files.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return f"Failed to read file: {e}"

    rel_path = file_path.relative_to(src.config.get_effective_workspace_root())

    if file_path.suffix == ".py":
        structure = parse_python_structure(content)
        return f"File: {rel_path}\nLanguage: Python\nStructure:\n{structure}"

    elif file_path.suffix in (".md", ".txt", ".css", ".json", ".env", ".example"):
        # For markdown/text, get the first 500 characters or lines
        snippet = content[:800].strip()
        return f"File: {rel_path}\nSnippet:\n{snippet}..."

    else:
        return f"File: {rel_path}\nBinary or unsupported text type ({file_path.suffix}). Size: {len(content)} bytes."

def index_codebase(workspace_dir: Path = None):
    """
    Recursively scans the workspace directory, generates file structural summaries,
    and indexes them into the 'janus_codebase' ChromaDB collection.
    """
    if workspace_dir is None:
        workspace_dir = src.config.get_effective_workspace_root()

    logger.info(f"Scanning and indexing codebase at: {workspace_dir} ...")

    ignored_dirs = {".git", ".venv", "venv", "__pycache__", "data", ".pytest_cache", ".janus_sandboxes", ".janus_snapshots"}
    ignored_files = {".DS_Store", "janus.db", "janus.db-journal", "janus.db-wal", "janus.db-shm"}
    # Extensions that produce no useful summary and would trigger expensive embedding calls
    ignored_extensions = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".db-journal",
                          ".jpg", ".jpeg", ".png", ".gif", ".ico", ".svg", ".woff", ".woff2", ".ttf", ".eot",
                          ".zip", ".tar", ".gz", ".lock", ".bin"}

    indexed_count = 0
    current_ids = set()

    for root, dirs, files in os.walk(workspace_dir):
        # Prune ignored directories in-place
        dirs[:] = [d for d in dirs if d not in ignored_dirs]

        for file in files:
            if file in ignored_files or Path(file).suffix in ignored_extensions:
                continue

            file_path = Path(root) / file
            rel_path = file_path.relative_to(workspace_dir)

            # Generate file structure summary
            summary_doc = generate_file_summary(file_path)

            # Save into janus_codebase vector DB
            memory_id = f"code_{rel_path.as_posix().replace('/', '_')}"
            metadata = {
                "file_path": rel_path.as_posix(),
                "file_name": file,
                "last_modified": os.path.getmtime(file_path)
            }

            # The file exists, so its entry must survive the prune below even if
            # this indexing attempt fails — a stale summary beats none.
            current_ids.add(memory_id)

            try:
                add_memory(
                    content=summary_doc,
                    metadata=metadata,
                    memory_id=memory_id,
                    collection_name="janus_codebase",
                    upsert=True
                )
                indexed_count += 1
            except Exception as e:
                logger.error(f"Failed to index codebase file {rel_path}: {e}")

    # Remove index entries for files that no longer exist in the workspace, so
    # self-inspection can't surface summaries of deleted code.
    pruned_count = 0
    try:
        collection = get_collection("janus_codebase")
        existing_ids = collection.get().get("ids") or []
        stale_ids = [i for i in existing_ids if i not in current_ids]
        if stale_ids:
            collection.delete(ids=stale_ids)
            pruned_count = len(stale_ids)
    except Exception as e:
        logger.error(f"Failed to prune stale codebase index entries: {e}")

    logger.info(
        f"Codebase indexing complete. Indexed {indexed_count} files in 'janus_codebase', "
        f"pruned {pruned_count} stale entries."
    )

def query_codebase_context(query_text: str, limit: int = 3) -> str:
    """
    Queries the codebase index for relevant file structures and signatures.
    Returns a unified context block.
    """
    try:
        matches = query_memories(query_text, limit=limit, collection_name="janus_codebase")
        if not matches:
            return "No matching codebase files found."

        context_blocks = []
        for match in matches:
            context_blocks.append(f"--- Codebase Context ---\n{match['content']}\n")
        return "\n".join(context_blocks)
    except Exception as e:
        logger.error(f"Failed to query codebase index: {e}")
        return f"Codebase context query error: {e}"
