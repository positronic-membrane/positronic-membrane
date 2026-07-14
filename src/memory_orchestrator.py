"""
Memory Orchestrator — Extended for Multi-Party Support.

Provides party-scoped memory operations with namespace isolation.
All party memories are prefixed with 'party:{party_id}:' to ensure
context isolation per GEMINI.md privacy rules.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.database import get_connection


class MemoryOrchestrator:
    """Manages memory operations with optional party scoping."""

    def _get_connection(self):
        """Get a database connection with dict-like row access (every method
        in this class reads columns by name, e.g. row['value'])."""
        conn = get_connection()
        conn.row_factory = sqlite3.Row
        return conn

    def set_memory(self, party_id: Optional[str], key: str, value: Any,
                   namespace: str = 'global') -> str:
        """
        Store a memory value scoped to a party and namespace.
        If party_id is None, stores in the global namespace (backward compatible).
        Returns the memory record ID.
        """
        memory_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        value_json = json.dumps(value)

        # If no party_id, use a sentinel 'global' party or store without party
        effective_party = party_id if party_id else '__global__'

        conn = self._get_connection()
        try:
            # Upsert: insert or update existing record
            existing = conn.execute(
                'SELECT id FROM memories WHERE party_id = ? AND namespace = ? AND key = ?',
                (effective_party, namespace, key)
            ).fetchone()

            if existing:
                conn.execute(
                    'UPDATE memories SET value = ?, updated_at = ? WHERE id = ?',
                    (value_json, now, existing['id'])
                )
                memory_id = existing['id']
            else:
                conn.execute(
                    'INSERT INTO memories (id, party_id, key, value, created_at, updated_at, namespace) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (memory_id, effective_party, key, value_json, now, now, namespace)
                )
            conn.commit()
        finally:
            conn.close()

        return memory_id

    def get_memory(self, party_id: Optional[str], key: str,
                   namespace: str = 'global') -> Optional[Any]:
        """
        Retrieve a memory value scoped to a party and namespace.
        Returns None if not found.
        """
        effective_party = party_id if party_id else '__global__'

        conn = self._get_connection()
        try:
            row = conn.execute(
                'SELECT value FROM memories WHERE party_id = ? AND namespace = ? AND key = ?',
                (effective_party, namespace, key)
            ).fetchone()
            if row:
                return json.loads(row['value'])
            return None
        finally:
            conn.close()

    def get_all_keys(self, party_id: Optional[str],
                     namespace: str = 'global') -> List[str]:
        """List all memory keys for a given party and namespace."""
        effective_party = party_id if party_id else '__global__'

        conn = self._get_connection()
        try:
            rows = conn.execute(
                'SELECT key FROM memories WHERE party_id = ? AND namespace = ?',
                (effective_party, namespace)
            ).fetchall()
            return [row['key'] for row in rows]
        finally:
            conn.close()

    def delete_memory(self, party_id: Optional[str], key: str,
                      namespace: str = 'global') -> bool:
        """Delete a memory record. Returns True if deleted, False if not found."""
        effective_party = party_id if party_id else '__global__'

        conn = self._get_connection()
        try:
            cursor = conn.execute(
                'DELETE FROM memories WHERE party_id = ? AND namespace = ? AND key = ?',
                (effective_party, namespace, key)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def get_party_memories(self, party_id: str) -> Dict[str, Dict[str, Any]]:
        """Get all memories for a party, organized by namespace."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                'SELECT namespace, key, value FROM memories WHERE party_id = ?',
                (party_id,)
            ).fetchall()
            result = {}
            for row in rows:
                ns = row['namespace']
                if ns not in result:
                    result[ns] = {}
                result[ns][row['key']] = json.loads(row['value'])
            return result
        finally:
            conn.close()

    def log_episodic_memory(self, party_id: Optional[str], session_id: Optional[str],
                            message_content: str, speaker: str = 'user',
                            context_type: str = 'user_visible') -> int:
        """
        Log an episodic memory (chat message or background thought) scoped to a party.
        Uses real schema columns: message_content, speaker, timestamp, context_type.
        Returns the auto-generated record ID (INTEGER PRIMARY KEY).
        """
        # Space-separated, no microseconds — matches the format SQLite's
        # CURRENT_TIMESTAMP default produces for rows written via
        # src.database.log_episodic_memory. episodic_memory.timestamp is a
        # plain TEXT column read back with lexicographic comparisons (e.g.
        # src.memory's age-based compression cutoff), so a differently
        # formatted timestamp in the same column would sort incorrectly.
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

        conn = self._get_connection()
        try:
            cursor = conn.execute(
                'INSERT INTO episodic_memory (message_content, speaker, timestamp, party_id, session_id, context_type) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (message_content, speaker, now, party_id, session_id, context_type)
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_episodic_memories(self, party_id: str,
                              limit: int = 50,
                              context_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve recent episodic memories for a party, optionally filtered by context_type."""
        conn = self._get_connection()
        try:
            if context_type:
                rows = conn.execute(
                    'SELECT id, message_content, speaker, timestamp, session_id, context_type '
                    'FROM episodic_memory WHERE party_id = ? AND context_type = ? '
                    'ORDER BY timestamp DESC LIMIT ?',
                    (party_id, context_type, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    'SELECT id, message_content, speaker, timestamp, session_id, context_type '
                    'FROM episodic_memory WHERE party_id = ? '
                    'ORDER BY timestamp DESC LIMIT ?',
                    (party_id, limit)
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
