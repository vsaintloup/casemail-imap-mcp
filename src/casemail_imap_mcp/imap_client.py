from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
import imaplib
import logging
import re
import socket
from typing import Iterable

from .config import Settings
from .parsing import decode_imap_utf7, encode_imap_utf7, parse_imap_list_line

logger = logging.getLogger(__name__)


class ImapError(RuntimeError):
    """Raised for IMAP transport or protocol failures."""


@dataclass(slots=True)
class ImapFetchedMessage:
    uid: int
    uidvalidity: int
    internal_date_iso: str | None
    raw_bytes: bytes


@dataclass(slots=True)
class ImapFolder:
    name: str
    delimiter: str | None
    flags: list[str]
    message_count: int | None
    uidvalidity: int | None


class ReadOnlyImapClient(AbstractContextManager["ReadOnlyImapClient"]):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._imap: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "ReadOnlyImapClient":
        self._connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._disconnect()
        return None

    def _connect(self) -> None:
        socket.setdefaulttimeout(self._settings.imap_timeout_seconds)
        if self._settings.imap_use_ssl:
            self._imap = imaplib.IMAP4_SSL(self._settings.imap_host, self._settings.imap_port)
        else:
            self._imap = imaplib.IMAP4(self._settings.imap_host, self._settings.imap_port)
        self._imap.login(self._settings.imap_username, self._settings.imap_password)

    def _disconnect(self) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("IMAP logout failed during cleanup.")
        self._imap = None

    def reconnect_folder(self, folder: str) -> int:
        self._disconnect()
        self._connect()
        return self.examine_folder(folder)

    @property
    def raw(self) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
        if self._imap is None:
            raise ImapError("IMAP connection is not open")
        return self._imap

    def noop(self) -> None:
        status, _ = self.raw.noop()
        self._ensure_ok(status, "NOOP")

    def list_folders(self, include_counts: bool = True) -> list[ImapFolder]:
        status, data = self.raw.list()
        self._ensure_ok(status, "LIST")
        folders: list[ImapFolder] = []
        for line in data or []:
            if not line:
                continue
            flags, delimiter, name = parse_imap_list_line(line)
            message_count = None
            uidvalidity = None
            if include_counts and not any(flag.lower() == r"\noselect" for flag in flags):
                try:
                    status_data = self.get_folder_status(name)
                    message_count = status_data.get("MESSAGES")
                    uidvalidity = status_data.get("UIDVALIDITY")
                except ImapError as exc:
                    logger.debug("Skipping folder STATUS for %s: %s", name, exc)
            folders.append(
                ImapFolder(
                    name=name,
                    delimiter=delimiter,
                    flags=flags,
                    message_count=message_count,
                    uidvalidity=uidvalidity,
                )
            )
        return folders

    def get_folder_status(self, folder: str) -> dict[str, int]:
        encoded_folder = encode_imap_utf7(folder)
        status, data = self.raw.status(_quote_mailbox(encoded_folder), "(MESSAGES UIDVALIDITY)")
        self._ensure_ok(status, "STATUS")
        line = (data or [b""])[0]
        text = line.decode("utf-8", "replace")
        values: dict[str, int] = {}
        for key, value in re.findall(r"(MESSAGES|UIDVALIDITY)\s+(\d+)", text):
            values[key] = int(value)
        return values

    def search_uids(self, folder: str, since: datetime | None = None, until: datetime | None = None) -> tuple[int, list[int]]:
        uidvalidity = self.examine_folder(folder)
        criteria: list[str] = ["ALL"]
        if since is not None:
            criteria.extend(["SINCE", since.strftime("%d-%b-%Y")])
        if until is not None:
            criteria.extend(["BEFORE", until.strftime("%d-%b-%Y")])
        status, data = self.raw.uid("SEARCH", None, *criteria)
        self._ensure_ok(status, "UID SEARCH")
        raw_uids = (data or [b""])[0]
        uids = [int(item) for item in raw_uids.decode("ascii", "ignore").split() if item]
        return uidvalidity, uids

    def fetch_message(
        self,
        folder: str,
        uid: int,
        uidvalidity: int | None = None,
        *,
        assume_folder_selected: bool = False,
    ) -> ImapFetchedMessage:
        current_uidvalidity = uidvalidity if assume_folder_selected and uidvalidity is not None else self.examine_folder(folder)
        if uidvalidity is not None and uidvalidity != current_uidvalidity:
            raise ImapError("UIDVALIDITY mismatch for requested message")
        # Security-sensitive: BODY.PEEK keeps reads side-effect free and avoids setting \Seen.
        status, data = self.raw.uid("FETCH", str(uid), "(UID INTERNALDATE BODY.PEEK[])")
        self._ensure_ok(status, "UID FETCH")
        header: bytes | None = None
        payload: bytes | None = None
        for item in data or []:
            if isinstance(item, tuple) and len(item) == 2:
                header = item[0]
                payload = item[1]
                break
        if payload is None:
            raise ImapError(f"Message UID {uid} not found in folder {folder}")
        internal_date_iso = None
        if header:
            header_text = header.decode("utf-8", "replace")
            match = re.search(r'INTERNALDATE "([^"]+)"', header_text)
            if match:
                internal_date_iso = self._normalize_internaldate(match.group(1))
        return ImapFetchedMessage(
            uid=uid,
            uidvalidity=current_uidvalidity,
            internal_date_iso=internal_date_iso,
            raw_bytes=payload,
        )

    def examine_folder(self, folder: str) -> int:
        encoded_folder = encode_imap_utf7(folder)
        # Security-sensitive: imaplib exposes EXAMINE via select(..., readonly=True).
        # This sends the read-only EXAMINE command, not a mutating SELECT.
        status, _ = self.raw.select(_quote_mailbox(encoded_folder), readonly=True)
        self._ensure_ok(status, "EXAMINE")
        response = self.raw.response("UIDVALIDITY")
        values = response[1] if response else None
        if not values:
            raise ImapError(f"UIDVALIDITY missing for folder {folder}")
        raw_value = values[0]
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode("ascii", "ignore")
        return int(raw_value)

    def _ensure_ok(self, status: str, command: str) -> None:
        if status != "OK":
            raise ImapError(f"{command} failed with status {status}")

    def _normalize_internaldate(self, value: str) -> str | None:
        try:
            return datetime.strptime(value, "%d-%b-%Y %H:%M:%S %z").astimezone(UTC).isoformat()
        except ValueError:
            return None


def _quote_mailbox(folder: str) -> str:
    escaped = folder.replace("\\", "\\\\").replace('"', r"\"")
    return f'"{escaped}"'
