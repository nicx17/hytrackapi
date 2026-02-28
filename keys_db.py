import sqlite3
import hashlib
import secrets
import os
from datetime import datetime
import logging
from passlib.context import CryptContext

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class APIKeyManager:
    """Manages secure generation and validation of API keys using SQLite."""

    def __init__(self, db_file="api_keys.db"):
        self.db_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), db_file)

    def _get_connection(self):
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        return conn

    def setup(self):
        """Initializes the API keys database schema."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    key_hash TEXT UNIQUE NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
            logger.info("API Keys database schema initialized.")

    def _hash_key(self, api_key: str) -> str:
        """Generates a bcrypt hash of the plaintext API key."""
        return pwd_context.hash(api_key)

    def generate_key(self, name: str) -> str:
        """
        Generates a new secure random API key, stores its hash,
        and returns the plaintext key to be displayed once to the user.
        Format returned is `id.plaintext_key`.
        """
        # Generate a secure 32-byte token
        raw_key = secrets.token_urlsafe(32)
        key_hash = self._hash_key(raw_key)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO keys (name, key_hash) VALUES (?, ?)", (name, key_hash)
            )
            key_id = cursor.lastrowid
            conn.commit()
            logger.info(f"New API key generated for name='{name}'")

        # Return the key combined with the ID for fast O(1) DB lookup
        return f"{key_id}.{raw_key}"

    def validate_key(self, api_key: str) -> bool:
        """
        Checks if the key ID exists and is active, then verifies
        the plaintext portion against the stored bcrypt hash.
        """
        if not api_key or "." not in api_key:
            return False

        try:
            key_id_str, raw_key = api_key.split(".", 1)
            key_id = int(key_id_str)
        except ValueError:
            return False

        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Fetch the hash for this specific ID if active
            cursor.execute(
                "SELECT key_hash FROM keys WHERE id = ? AND is_active = 1", (key_id,)
            )
            result = cursor.fetchone()

        if result:
            stored_hash = result["key_hash"]
            return pwd_context.verify(raw_key, stored_hash)

        return False

    def list_keys(self) -> list:
        """Returns metadata for all keys (excluding hashes/secrets)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, is_active, created_at FROM keys ORDER BY created_at DESC"
            )
            return [dict(row) for row in cursor.fetchall()]

    def revoke_key(self, key_id: int) -> bool:
        """Deactivates an API key by ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE keys SET is_active = 0 WHERE id = ?", (key_id,))
            conn.commit()
            success = cursor.rowcount > 0

            if success:
                logger.info(f"Revoked API Key id={key_id}")
            return success
