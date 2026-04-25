from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from .config import Settings
from .models import Attachment, FolderInfo, MessageDetail, MessageSummary
from .security import safe_json_dumps


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class NotSyncedError(LookupError):
    """Raised when a requested message or attachment is not in the local sync cache."""


class PlainSyncStore:
    """Plain SQLite store for synced messages and attachments.

    This deliberately relies on the user's disk encryption instead of app-level
    encryption. It stores parsed content and attachment bytes, but not raw RFC822.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        settings.ensure_cache_parent_dirs()
        self._initialize_db()

    @contextmanager
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._settings.cache_db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def _initialize_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS synced_folders (
                    account TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 0,
                    delimiter TEXT,
                    flags_json TEXT NOT NULL DEFAULT '[]',
                    message_count INTEGER,
                    uidvalidity INTEGER,
                    last_sync_at TEXT,
                    last_error TEXT,
                    messages_downloaded INTEGER NOT NULL DEFAULT 0,
                    attachments_downloaded INTEGER NOT NULL DEFAULT 0,
                    attachments_skipped INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (account, folder)
                );

                CREATE TABLE IF NOT EXISTS synced_messages (
                    account TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    message_json TEXT NOT NULL,
                    header_date_iso TEXT,
                    imap_internal_date_iso TEXT,
                    subject TEXT NOT NULL,
                    normalized_subject TEXT NOT NULL,
                    sender_email TEXT,
                    message_id TEXT,
                    snippet TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    has_attachments INTEGER NOT NULL,
                    attachment_count INTEGER NOT NULL,
                    synced_at TEXT NOT NULL,
                    stale INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (account, folder, uidvalidity, uid)
                );

                CREATE INDEX IF NOT EXISTS idx_synced_messages_folder
                    ON synced_messages (account, folder, stale, header_date_iso);

                CREATE TABLE IF NOT EXISTS synced_attachments (
                    account TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    attachment_id TEXT NOT NULL,
                    attachment_json TEXT NOT NULL,
                    raw_bytes BLOB,
                    extracted_text TEXT,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    skipped_reason TEXT,
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY (account, folder, uidvalidity, uid, attachment_id)
                );

                CREATE TABLE IF NOT EXISTS sync_state (
                    account TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account, key)
                );
                """
            )
            connection.commit()

    @property
    def account(self) -> str:
        return self._settings.account_fingerprint

    def list_selected_folders(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT folder FROM synced_folders
                WHERE account = ? AND selected = 1
                ORDER BY lower(folder)
                """,
                (self.account,),
            ).fetchall()
        return [str(row["folder"]) for row in rows]

    def set_selected_folders(self, folders: Iterable[str]) -> None:
        selected = sorted(dict.fromkeys(folder for folder in folders if folder))
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute("UPDATE synced_folders SET selected = 0 WHERE account = ?", (self.account,))
            for folder in selected:
                connection.execute(
                    """
                    INSERT INTO synced_folders (account, folder, selected, last_sync_at)
                    VALUES (?, ?, 1, NULL)
                    ON CONFLICT(account, folder) DO UPDATE SET selected = 1
                    """,
                    (self.account, folder),
                )
            self._put_state(connection, "selected_folders_updated_at", {"updated_at": now})
            connection.commit()

    def upsert_folder_metadata(
        self,
        folder: str,
        *,
        delimiter: str | None,
        flags: list[str],
        message_count: int | None,
        uidvalidity: int | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO synced_folders (
                    account, folder, selected, delimiter, flags_json, message_count, uidvalidity
                )
                VALUES (?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(account, folder) DO UPDATE SET
                    delimiter = excluded.delimiter,
                    flags_json = excluded.flags_json,
                    message_count = excluded.message_count,
                    uidvalidity = COALESCE(excluded.uidvalidity, synced_folders.uidvalidity)
                """,
                (self.account, folder, delimiter, safe_json_dumps(flags), message_count, uidvalidity),
            )
            connection.commit()

    def list_cached_folders(self) -> list[FolderInfo]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT folder, delimiter, flags_json, message_count FROM synced_folders
                WHERE account = ? AND selected = 1
                ORDER BY lower(folder)
                """,
                (self.account,),
            ).fetchall()
        return [
            FolderInfo(
                name=str(row["folder"]),
                delimiter=row["delimiter"],
                flags=json.loads(row["flags_json"] or "[]"),
                message_count=row["message_count"],
                is_sent_candidate=self._settings.sent_folder_regex.search(str(row["folder"])) is not None,
            )
            for row in rows
        ]

    def mark_folder_stale(self, folder: str, uidvalidity: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE synced_messages SET stale = 1
                WHERE account = ? AND folder = ? AND uidvalidity != ?
                """,
                (self.account, folder, uidvalidity),
            )
            connection.execute(
                """
                UPDATE synced_folders SET uidvalidity = ?, last_error = NULL
                WHERE account = ? AND folder = ?
                """,
                (uidvalidity, self.account, folder),
            )
            connection.commit()

    def missing_uids(self, folder: str, uidvalidity: int, uids: Iterable[int]) -> list[int]:
        requested = list(dict.fromkeys(uids))
        if not requested:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT uid FROM synced_messages
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND stale = 0
                """,
                (self.account, folder, uidvalidity),
            ).fetchall()
        present = {int(row["uid"]) for row in rows}
        return [uid for uid in requested if uid not in present]

    def save_message(self, detail: MessageDetail, attachments: list[dict[str, object]]) -> None:
        now = utc_now_iso()
        message_data = detail.model_dump(by_alias=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO synced_messages (
                    account, folder, uidvalidity, uid, message_json, header_date_iso,
                    imap_internal_date_iso, subject, normalized_subject, sender_email,
                    message_id, snippet, direction, has_attachments, attachment_count,
                    synced_at, stale
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(account, folder, uidvalidity, uid) DO UPDATE SET
                    message_json = excluded.message_json,
                    header_date_iso = excluded.header_date_iso,
                    imap_internal_date_iso = excluded.imap_internal_date_iso,
                    subject = excluded.subject,
                    normalized_subject = excluded.normalized_subject,
                    sender_email = excluded.sender_email,
                    message_id = excluded.message_id,
                    snippet = excluded.snippet,
                    direction = excluded.direction,
                    has_attachments = excluded.has_attachments,
                    attachment_count = excluded.attachment_count,
                    synced_at = excluded.synced_at,
                    stale = 0
                """,
                (
                    self.account,
                    detail.folder,
                    self._uidvalidity_from_ref(detail.message_ref),
                    detail.uid,
                    safe_json_dumps(message_data),
                    detail.header_date_iso,
                    detail.imap_internal_date_iso,
                    detail.subject,
                    detail.normalized_subject,
                    detail.sender_email,
                    detail.message_id,
                    detail.snippet,
                    detail.direction,
                    1 if detail.has_attachments else 0,
                    detail.attachment_count,
                    now,
                ),
            )
            for item in attachments:
                attachment = Attachment.model_validate(item["attachment"])
                connection.execute(
                    """
                    INSERT INTO synced_attachments (
                        account, folder, uidvalidity, uid, attachment_id, attachment_json,
                        raw_bytes, extracted_text, warnings_json, skipped_reason, synced_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account, folder, uidvalidity, uid, attachment_id) DO UPDATE SET
                        attachment_json = excluded.attachment_json,
                        raw_bytes = excluded.raw_bytes,
                        extracted_text = excluded.extracted_text,
                        warnings_json = excluded.warnings_json,
                        skipped_reason = excluded.skipped_reason,
                        synced_at = excluded.synced_at
                    """,
                    (
                        self.account,
                        detail.folder,
                        self._uidvalidity_from_ref(detail.message_ref),
                        detail.uid,
                        attachment.attachment_id,
                        safe_json_dumps(attachment.model_dump()),
                        item.get("raw_bytes"),
                        item.get("extracted_text"),
                        safe_json_dumps(item.get("warnings", [])),
                        item.get("skipped_reason"),
                        now,
                    ),
                )
            connection.commit()

    def list_message_summaries(self, folders: Iterable[str] | None = None) -> list[MessageSummary]:
        folder_list = list(folders or self.list_selected_folders())
        if not folder_list:
            return []
        placeholders = ",".join("?" for _ in folder_list)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT message_json FROM synced_messages
                WHERE account = ? AND stale = 0 AND folder IN ({placeholders})
                ORDER BY COALESCE(header_date_iso, imap_internal_date_iso, synced_at) DESC
                """,
                (self.account, *folder_list),
            ).fetchall()
        return [MessageSummary.model_validate(json.loads(row["message_json"])) for row in rows]

    def get_message_detail(self, folder: str, uidvalidity: int, uid: int) -> MessageDetail:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT message_json FROM synced_messages
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND uid = ? AND stale = 0
                """,
                (self.account, folder, uidvalidity, uid),
            ).fetchone()
            if row is None:
                raise NotSyncedError("message is not synced locally")
            attachment_rows = connection.execute(
                """
                SELECT attachment_json, extracted_text, skipped_reason FROM synced_attachments
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                ORDER BY attachment_id
                """,
                (self.account, folder, uidvalidity, uid),
            ).fetchall()

        detail = MessageDetail.model_validate(json.loads(row["message_json"]))
        attachments: list[Attachment] = []
        for attachment_row in attachment_rows:
            attachment = Attachment.model_validate(json.loads(attachment_row["attachment_json"]))
            extracted_text = attachment_row["extracted_text"]
            if extracted_text:
                attachment.extracted_text_available = True
                attachment.extracted_text_excerpt = str(extracted_text)[: self._settings.max_snippet_chars]
            attachments.append(attachment)
        detail.attachments = attachments
        return detail

    def get_attachment(self, folder: str, uidvalidity: int, uid: int, attachment_id: str) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT attachment_json, raw_bytes, extracted_text, warnings_json, skipped_reason
                FROM synced_attachments
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND uid = ? AND attachment_id = ?
                """,
                (self.account, folder, uidvalidity, uid, attachment_id),
            ).fetchone()
        if row is None:
            raise NotSyncedError("attachment is not synced locally")
        attachment = Attachment.model_validate(json.loads(row["attachment_json"]))
        extracted_text = row["extracted_text"]
        if extracted_text:
            attachment.extracted_text_available = True
            attachment.extracted_text_excerpt = str(extracted_text)[: self._settings.max_snippet_chars]
        return {
            "attachment": attachment,
            "raw_bytes_cached": row["raw_bytes"] is not None,
            "extracted_text": extracted_text,
            "warnings": json.loads(row["warnings_json"] or "[]"),
            "skipped_reason": row["skipped_reason"],
        }

    def update_folder_sync_result(
        self,
        folder: str,
        *,
        uidvalidity: int,
        message_count: int,
        messages_downloaded: int,
        attachments_downloaded: int,
        attachments_skipped: int,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO synced_folders (
                    account, folder, selected, uidvalidity, message_count, last_sync_at,
                    last_error, messages_downloaded, attachments_downloaded, attachments_skipped
                )
                VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account, folder) DO UPDATE SET
                    selected = 1,
                    uidvalidity = excluded.uidvalidity,
                    message_count = excluded.message_count,
                    last_sync_at = excluded.last_sync_at,
                    last_error = excluded.last_error,
                    messages_downloaded = excluded.messages_downloaded,
                    attachments_downloaded = excluded.attachments_downloaded,
                    attachments_skipped = excluded.attachments_skipped
                """,
                (
                    self.account,
                    folder,
                    uidvalidity,
                    message_count,
                    utc_now_iso(),
                    error,
                    messages_downloaded,
                    attachments_downloaded,
                    attachments_skipped,
                ),
            )
            connection.commit()

    def set_sync_status(self, status: dict[str, object]) -> None:
        with self._connect() as connection:
            self._put_state(connection, "sync_status", status)
            connection.commit()

    def get_sync_status(self) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM sync_state WHERE account = ? AND key = 'sync_status'",
                (self.account,),
            ).fetchone()
            folders = connection.execute(
                """
                SELECT folder, selected, uidvalidity, last_sync_at, last_error, message_count,
                       messages_downloaded, attachments_downloaded, attachments_skipped
                FROM synced_folders
                WHERE account = ?
                ORDER BY lower(folder)
                """,
                (self.account,),
            ).fetchall()
        status = json.loads(row["value_json"]) if row else {"state": "idle", "updated_at": None}
        cached_folders = [dict(folder_row) for folder_row in folders]
        status.setdefault("folders", cached_folders)
        status["cached_folders"] = cached_folders
        return status

    def _put_state(self, connection: sqlite3.Connection, key: str, value: dict[str, object]) -> None:
        connection.execute(
            """
            INSERT INTO sync_state (account, key, value_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account, key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (self.account, key, safe_json_dumps(value), utc_now_iso()),
        )

    def _uidvalidity_from_ref(self, message_ref: str) -> int:
        from .security import parse_message_ref

        payload = parse_message_ref(
            message_ref,
            self._settings.message_ref_secret,
            self._settings.account_fingerprint,
        )
        return payload.uidvalidity
