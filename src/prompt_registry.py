import logging

from src.database import get_connection

logger = logging.getLogger("JanusPromptRegistry")

_COLUMNS = ["id", "name", "version", "content", "created_at", "created_by", "change_reason", "is_active"]


def _row_to_dict(row) -> dict:
    return dict(zip(_COLUMNS, row, strict=True))


def get_prompt(name: str, version: int = None) -> dict:
    """
    Returns the active prompt_templates row for `name`, or a specific historical
    `version` if given. Returns None if no matching row exists.
    """
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        if version is None:
            cursor.execute("""
            SELECT id, name, version, content, created_at, created_by, change_reason, is_active
            FROM prompt_templates WHERE name = ? AND is_active = 1 LIMIT 1;
            """, (name,))
        else:
            cursor.execute("""
            SELECT id, name, version, content, created_at, created_by, change_reason, is_active
            FROM prompt_templates WHERE name = ? AND version = ?;
            """, (name, version))
        row = cursor.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_prompt_names() -> list:
    """Returns the distinct set of registered prompt template names."""
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT name FROM prompt_templates ORDER BY name;")
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def list_versions(name: str) -> list:
    """Returns all versions of `name`, newest first, each including is_active."""
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT id, name, version, content, created_at, created_by, change_reason, is_active
        FROM prompt_templates WHERE name = ? ORDER BY version DESC;
        """, (name,))
        return [_row_to_dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def update_prompt(name: str, content: str, change_reason: str, created_by: str = "user") -> int:
    """
    Creates a new version of `name` with `content`, deactivating the previous
    active version. Returns the new version number.
    """
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(MAX(version), 0) FROM prompt_templates WHERE name = ?;", (name,))
        next_version = cursor.fetchone()[0] + 1
        cursor.execute("UPDATE prompt_templates SET is_active = 0 WHERE name = ? AND is_active = 1;", (name,))
        cursor.execute("""
        INSERT INTO prompt_templates (name, version, content, created_by, change_reason, is_active)
        VALUES (?, ?, ?, ?, ?, 1);
        """, (name, next_version, content, created_by, change_reason))
        conn.commit()
        return next_version
    finally:
        conn.close()


def rollback_prompt(name: str, version: int, created_by: str = "user") -> bool:
    """
    Reactivates a historical `version` of `name` and deactivates the current
    active version. Returns False if that version doesn't exist.
    """
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM prompt_templates WHERE name = ? AND version = ?;", (name, version))
        if cursor.fetchone() is None:
            return False
        cursor.execute("UPDATE prompt_templates SET is_active = 0 WHERE name = ?;", (name,))
        cursor.execute("UPDATE prompt_templates SET is_active = 1 WHERE name = ? AND version = ?;", (name, version))
        conn.commit()
        logger.info("Rolled back prompt '%s' to version %d (by %s)", name, version, created_by)
        return True
    finally:
        conn.close()
