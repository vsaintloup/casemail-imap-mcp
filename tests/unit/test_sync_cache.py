from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from casemail_imap_mcp.cache import NotSyncedError, PlainSyncStore
from casemail_imap_mcp.imap_client import ImapFetchedMessage
from casemail_imap_mcp.models import Attachment, MessageDetail, MessageRefPayload, Participant
from casemail_imap_mcp.security import build_message_ref
from casemail_imap_mcp.service import CaseMailService
from casemail_imap_mcp.sync_service import SyncService


def _detail(settings, *, folder: str = "Client/ABC", uid: int = 1, uidvalidity: int = 123) -> MessageDetail:
    message_ref = build_message_ref(
        MessageRefPayload(folder=folder, uid=uid, uidvalidity=uidvalidity, account=settings.account_fingerprint),
        settings.message_ref_secret,
    )
    attachment = Attachment(
        attachment_id="2",
        filename="note.txt",
        mime_type="text/plain",
        size_bytes=11,
        sha256="abc",
        extracted_text_available=True,
        extracted_text_excerpt="hello world",
    )
    return MessageDetail(
        message_ref=message_ref,
        folder=folder,
        uid=uid,
        direction="received",
        imap_internal_date_iso="2026-02-01T10:00:00+00:00",
        header_date_iso="2026-02-01T10:00:00+00:00",
        subject="Matter update",
        normalized_subject="matter update",
        from_=Participant(name=None, email="client@example.com", raw="client@example.com"),
        sender_email="client@example.com",
        to=[Participant(name=None, email="lawyer@example.com", raw="lawyer@example.com")],
        cc=[],
        bcc=[],
        reply_to=[],
        message_id="<m1@example.test>",
        in_reply_to=None,
        references=[],
        snippet="hello world",
        body_text="hello world",
        body_text_truncated=False,
        has_attachments=True,
        attachment_count=1,
        attachment_names=["note.txt"],
        attachments=[attachment],
        thread_key_candidates=["<m1@example.test>", "matter update"],
        related_thread_keys=["<m1@example.test>", "matter update"],
        relevance_notes=[],
        parsing_warnings=[],
    )


def test_selected_folder_persistence(settings) -> None:
    store = PlainSyncStore(settings)
    store.set_selected_folders(["Client/ABC", "Sent"])

    assert store.list_selected_folders() == ["Client/ABC", "Sent"]


def test_plain_message_and_attachment_storage(settings) -> None:
    store = PlainSyncStore(settings)
    detail = _detail(settings)
    attachment = detail.attachments[0]

    store.set_selected_folders(["Client/ABC"])
    store.save_message(
        detail,
        [
            {
                "attachment": attachment.model_dump(),
                "raw_bytes": b"hello world",
                "extracted_text": "hello world",
                "warnings": [],
                "skipped_reason": None,
            }
        ],
    )

    cached = store.get_message_detail("Client/ABC", 123, 1)
    cached_attachment = store.get_attachment("Client/ABC", 123, 1, "2")

    assert cached.body_text == "hello world"
    assert cached.attachments[0].filename == "note.txt"
    assert cached_attachment["raw_bytes_cached"] is True
    assert cached_attachment["extracted_text"] == "hello world"


def test_cache_only_service_fails_closed_for_unsynced_message(settings) -> None:
    store = PlainSyncStore(settings)
    store.set_selected_folders(["Client/ABC"])
    service = CaseMailService(settings, store)
    message_ref = build_message_ref(
        MessageRefPayload(folder="Client/ABC", uid=99, uidvalidity=123, account=settings.account_fingerprint),
        settings.message_ref_secret,
    )

    with pytest.raises(NotSyncedError):
        service.read_message(message_ref)


class FakeSyncClient:
    calls: list[tuple[str, tuple]] = []

    def __init__(self, settings) -> None:
        self.settings = settings

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def search_uids(self, folder: str):
        self.calls.append(("search_uids", (folder,)))
        return 777, [1, 2]

    def fetch_message(self, folder: str, uid: int, uidvalidity: int):
        self.calls.append(("fetch_message", (folder, uid, uidvalidity)))
        return ImapFetchedMessage(
            uid=uid,
            uidvalidity=uidvalidity,
            internal_date_iso="2026-02-01T10:00:00+00:00",
            raw_bytes=_message_bytes(uid),
        )


def _message_bytes(uid: int) -> bytes:
    message = EmailMessage()
    message["Subject"] = f"Message {uid}"
    message["From"] = "client@example.com"
    message["To"] = "lawyer@example.com"
    message["Date"] = datetime(2026, 2, uid, 10, 0, tzinfo=UTC).strftime("%a, %d %b %Y %H:%M:%S %z")
    message["Message-ID"] = f"<m{uid}@example.test>"
    message.set_content(f"Body {uid}")
    message.add_attachment(f"Attachment {uid}".encode(), maintype="text", subtype="plain", filename=f"note-{uid}.txt")
    return message.as_bytes()


def test_sync_downloads_only_missing_messages_and_attachments(monkeypatch, settings) -> None:
    store = PlainSyncStore(settings)
    store.set_selected_folders(["Client/ABC"])
    FakeSyncClient.calls = []
    monkeypatch.setattr("casemail_imap_mcp.sync_service.ReadOnlyImapClient", FakeSyncClient)

    first = SyncService(settings, store).sync_selected_folders()
    second = SyncService(settings, store).sync_selected_folders()

    fetch_calls = [call for call in FakeSyncClient.calls if call[0] == "fetch_message"]
    assert first["state"] == "completed"
    assert second["state"] == "completed"
    assert len(fetch_calls) == 2
    assert len(store.list_message_summaries(["Client/ABC"])) == 2
    attachment = store.get_attachment("Client/ABC", 777, 1, "2")
    assert attachment["raw_bytes_cached"] is True
    assert "Attachment 1" in attachment["extracted_text"]


def test_sync_skips_oversized_attachment_bytes(monkeypatch, settings) -> None:
    settings.max_attachment_bytes = 4
    store = PlainSyncStore(settings)
    store.set_selected_folders(["Client/ABC"])
    FakeSyncClient.calls = []
    monkeypatch.setattr("casemail_imap_mcp.sync_service.ReadOnlyImapClient", FakeSyncClient)

    SyncService(settings, store).sync_selected_folders()

    attachment = store.get_attachment("Client/ABC", 777, 1, "2")
    assert attachment["raw_bytes_cached"] is False
    assert attachment["skipped_reason"]

