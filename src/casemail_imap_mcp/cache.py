from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
import sqlite3

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings

logger = logging.getLogger(__name__)


class EncryptedCache:
    """Stores normalized message and attachment text only, never raw MIME blobs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._enabled = settings.cache_enabled
        self._fernet: Fernet | None = None
        if self._enabled:
            settings.ensure_cache_parent_dirs()
            self._fernet = Fernet(self._load_or_create_key(settings.cache_key_path))
            self._initialize_db()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _load_or_create_key(self, path: Path) -> bytes:
        if path.exists():
            return path.read_bytes().strip()
        key = Fernet.generate_key()
        path.write_bytes(key)
        return key

    @contextmanager
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._settings.cache_db_path)
        try:
            yield connection
        finally:
            connection.close()

    def _initialize_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS message_cache (
                    cache_key TEXT PRIMARY KEY,
                    ciphertext BLOB NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attachment_cache (
                    cache_key TEXT PRIMARY KEY,
                    ciphertext BLOB NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
            connection.commit()

    def _encrypt(self, value: str) -> bytes:
        if not self._fernet:
            raise RuntimeError("cache encryption is unavailable")
        return self._fernet.encrypt(value.encode("utf-8"))

    def _decrypt(self, value: bytes) -> str | None:
        if not self._fernet:
            return None
        try:
            return self._fernet.decrypt(value).decode("utf-8")
        except InvalidToken:
            logger.warning("Encrypted cache entry could not be decrypted.")
            return None

    def _expires_at(self) -> str:
        return (datetime.now(tz=UTC) + timedelta(hours=self._settings.cache_ttl_hours)).isoformat()

    def _get(self, table: str, cache_key: str) -> str | None:
        if not self._enabled:
            return None
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT ciphertext, expires_at FROM {table} WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            ciphertext, expires_at = row
            if datetime.fromisoformat(expires_at) <= datetime.now(tz=UTC):
                connection.execute(f"DELETE FROM {table} WHERE cache_key = ?", (cache_key,))
                connection.commit()
                return None
            return self._decrypt(ciphertext)

    def _put(self, table: str, cache_key: str, value: str) -> None:
        if not self._enabled:
            return
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO {table} (cache_key, ciphertext, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    ciphertext = excluded.ciphertext,
                    expires_at = excluded.expires_at
                """,
                (cache_key, self._encrypt(value), self._expires_at()),
            )
            connection.commit()

    def get_message_body(self, cache_key: str) -> str | None:
        return self._get("message_cache", cache_key)

    def put_message_body(self, cache_key: str, value: str) -> None:
        self._put("message_cache", cache_key, value)

    def get_attachment_text(self, cache_key: str) -> str | None:
        return self._get("attachment_cache", cache_key)

    def put_attachment_text(self, cache_key: str, value: str) -> None:
        self._put("attachment_cache", cache_key, value)

