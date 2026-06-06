"""
Role Bootstrap — First-Run Party Initialization.

Handles the first-run ceremony that creates the root administrator party.
Prevents lock-out by ensuring exactly one boot sequence exists.
"""

import uuid
import secrets
import sys
from datetime import datetime
from typing import Optional, Tuple
from src.database import get_connection


class RoleBootstrap:
    """Manages the first-run party initialization ceremony."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path

    def _get_connection(self):
        return get_connection(self.db_path)

    def is_bootstrap_required(self) -> bool:
        """Check if the parties table is empty (first-run state)."""
        conn = self._get_connection()
        try:
            count = conn.execute('SELECT COUNT(*) as cnt FROM parties').fetchone()
            return count['cnt'] == 0
        finally:
            conn.close()

    def generate_enrollment_key(self) -> str:
        """Generate a secure random enrollment key."""
        return secrets.token_hex(32)

    def create_root_admin(self, name: str = 'root_admin',
                          enrollment_key: Optional[str] = None) -> Tuple[str, str]:
        """
        Create the first administrator party.
        Returns (party_id, enrollment_key).
        """
        if not self.is_bootstrap_required():
            raise RuntimeError("Bootstrap already completed: parties table is not empty.")

        party_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        key = enrollment_key or self.generate_enrollment_key()

        conn = self._get_connection()
        try:
            conn.execute(
                'INSERT INTO parties (id, name, role, created_at, public_key) '
                'VALUES (?, ?, ?, ?, ?)',
                (party_id, name, 'admin', now, key)
            )
            conn.commit()
        finally:
            conn.close()

        return party_id, key

    def bootstrap_from_cli(self) -> str:
        """
        Run the first-run bootstrap ceremony from the command line.
        Returns the generated party ID.
        """
        if not self.is_bootstrap_required():
            print("Bootstrap already completed. Parties table is populated.")
            return None

        print("\n=== Janus Multi-Party Setup Wizard ===")
        print("No parties found. Initializing first administrator...\n")

        enrollment_key = self.generate_enrollment_key()
        print(f"Your enrollment key: {enrollment_key}")
        print("(Save this key securely — it will be needed for future authentication)\n")

        name = input("Enter a name for the root administrator [root_admin]: ").strip()
        if not name:
            name = 'root_admin'

        party_id, _ = self.create_root_admin(name, enrollment_key)
        print(f"\nRoot administrator '{name}' created successfully!")
        print(f"Party ID: {party_id}")
        print("You can now register additional parties using the API.\n")

        return party_id

    def check_web_ui_bootstrap(self) -> dict:
        """
        Check bootstrap status for web UI.
        Returns a dict with status and instructions.
        """
        if self.is_bootstrap_required():
            return {
                'bootstrap_required': True,
                'message': 'Setup required. Please complete initialization via terminal.',
                'instructions': 'Run `python -m src.role_bootstrap` in your terminal to create the root administrator.'
            }
        return {
            'bootstrap_required': False,
            'message': 'System is ready.'
        }


if __name__ == '__main__':
    bootstrap = RoleBootstrap()
    bootstrap.bootstrap_from_cli()
