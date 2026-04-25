from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime
import logging

from .attachments import extract_text
from .cache import PlainSyncStore, utc_now_iso
from .config import Settings
from .imap_client import ImapFetchedMessage, ReadOnlyImapClient
from .models import Attachment, Direction, MessageDetail, MessageRefPayload
from .parsing import iter_attachment_parts, parse_email_message, parse_message_bytes
from .security import FolderAccessController, build_message_ref

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncFolderResult:
    folder: str
    uidvalidity: int
    remote_message_count: int
    messages_downloaded: int = 0
    messages_skipped: int = 0
    attachments_downloaded: int = 0
    attachments_skipped: int = 0
    bytes_downloaded: int = 0
    errors: list[str] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "folder": self.folder,
            "uidvalidity": self.uidvalidity,
            "remote_message_count": self.remote_message_count,
            "messages_downloaded": self.messages_downloaded,
            "messages_skipped": self.messages_skipped,
            "attachments_downloaded": self.attachments_downloaded,
            "attachments_skipped": self.attachments_skipped,
            "bytes_downloaded": self.bytes_downloaded,
            "errors": self.errors or [],
        }


class SyncService:
    def __init__(self, settings: Settings, store: PlainSyncStore | None = None) -> None:
        self.settings = settings
        self.store = store or PlainSyncStore(settings)
        self.access = FolderAccessController(settings)

    def sync_selected_folders(self, since_months: int | None = None) -> dict[str, object]:
        since = _months_ago_utc(since_months) if since_months is not None else None
        selected_folders = self.store.list_selected_folders()
        status: dict[str, object] = {
            "state": "running",
            "started_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "selected_folders": selected_folders,
            "since_months": since_months,
            "since_iso": since.isoformat() if since else None,
            "folders": [],
            "errors": [],
            "current": None,
        }
        self.store.set_sync_status(status)

        if not selected_folders:
            status.update({"state": "idle", "updated_at": utc_now_iso(), "errors": ["No folders selected."]})
            self.store.set_sync_status(status)
            return status

        total_bytes = 0
        folder_results: list[dict[str, object]] = []
        errors: list[str] = []

        with ReadOnlyImapClient(self.settings) as client:
            for folder_index, folder in enumerate(selected_folders, start=1):
                try:
                    result = self._sync_folder(
                        client,
                        folder,
                        total_bytes,
                        since=since,
                        status=status,
                        folder_index=folder_index,
                        total_folders=len(selected_folders),
                        folder_results=folder_results,
                        errors=errors,
                    )
                except Exception as exc:
                    # Security-sensitive: isolate folder failures so one bad or
                    # non-selectable folder cannot derail other scoped syncs.
                    logger.warning("Folder sync failed for folder=%s: %s", folder, str(exc))
                    result = SyncFolderResult(
                        folder=folder,
                        uidvalidity=0,
                        remote_message_count=0,
                        errors=[str(exc)],
                    )
                total_bytes += result.bytes_downloaded
                folder_results.append(result.as_dict())
                if result.errors:
                    errors.extend(f"{folder}: {error}" for error in result.errors)
                status.update(
                    {
                        "updated_at": utc_now_iso(),
                        "folders": folder_results,
                        "errors": errors,
                        "bytes_downloaded": total_bytes,
                        "current": {
                            "folder": folder,
                            "folder_index": folder_index,
                            "total_folders": len(selected_folders),
                            "state": "done_with_errors" if result.errors else "done",
                            "messages_processed": result.messages_downloaded + result.messages_skipped,
                            "messages_to_download": result.messages_downloaded,
                            "remote_message_count": result.remote_message_count,
                            "progress_percent": 100,
                        },
                    }
                )
                self.store.set_sync_status(status)
                if total_bytes >= self.settings.max_total_sync_bytes_per_run:
                    errors.append("MAX_TOTAL_SYNC_BYTES_PER_RUN reached; remaining folders were not synced.")
                    break

        status.update(
            {
                "state": "completed_with_errors" if errors else "completed",
                "finished_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
                "folders": folder_results,
                "errors": errors,
                "bytes_downloaded": total_bytes,
                "current": None,
            }
        )
        self.store.set_sync_status(status)
        return status

    def _sync_folder(
        self,
        client: ReadOnlyImapClient,
        folder: str,
        starting_bytes: int,
        *,
        since: datetime | None,
        status: dict[str, object],
        folder_index: int,
        total_folders: int,
        folder_results: list[dict[str, object]],
        errors: list[str],
    ) -> SyncFolderResult:
        uidvalidity, remote_uids = client.search_uids(folder, since=since)
        self.store.mark_folder_stale(folder, uidvalidity)
        missing = self.store.missing_uids(folder, uidvalidity, remote_uids)
        result = SyncFolderResult(
            folder=folder,
            uidvalidity=uidvalidity,
            remote_message_count=len(remote_uids),
            messages_skipped=len(remote_uids) - len(missing),
            errors=[],
        )
        self._publish_folder_progress(
            status,
            result,
            folder_index=folder_index,
            total_folders=total_folders,
            starting_bytes=starting_bytes,
            messages_to_download=len(missing),
            messages_processed=0,
            folder_results=folder_results,
            errors=errors,
        )

        for index, uid in enumerate(missing, start=1):
            if starting_bytes + result.bytes_downloaded >= self.settings.max_total_sync_bytes_per_run:
                result.errors.append("Sync byte budget reached before all messages were downloaded.")
                break
            try:
                fetched = client.fetch_message(folder, uid, uidvalidity)
                detail, attachment_rows, downloaded_bytes = self._build_cached_message(folder, fetched)
                self.store.save_message(detail, attachment_rows)
                result.messages_downloaded += 1
                result.bytes_downloaded += downloaded_bytes
                result.attachments_downloaded += sum(1 for row in attachment_rows if row.get("raw_bytes") is not None)
                result.attachments_skipped += sum(1 for row in attachment_rows if row.get("skipped_reason"))
            except Exception as exc:  # pragma: no cover - defensive per-message isolation
                logger.warning("Message sync failed for folder=%s uid=%s: %s", folder, uid, str(exc))
                result.errors.append(f"UID {uid}: {exc}")
            self._publish_folder_progress(
                status,
                result,
                folder_index=folder_index,
                total_folders=total_folders,
                starting_bytes=starting_bytes,
                messages_to_download=len(missing),
                messages_processed=index,
                folder_results=folder_results,
                errors=errors,
            )

        self.store.update_folder_sync_result(
            folder,
            uidvalidity=uidvalidity,
            message_count=len(remote_uids),
            messages_downloaded=result.messages_downloaded,
            attachments_downloaded=result.attachments_downloaded,
            attachments_skipped=result.attachments_skipped,
            error="; ".join(result.errors) if result.errors else None,
        )
        return result

    def _publish_folder_progress(
        self,
        status: dict[str, object],
        result: SyncFolderResult,
        *,
        folder_index: int,
        total_folders: int,
        starting_bytes: int,
        messages_to_download: int,
        messages_processed: int,
        folder_results: list[dict[str, object]],
        errors: list[str],
    ) -> None:
        progress_percent = 100 if messages_to_download == 0 else round((messages_processed / messages_to_download) * 100, 1)
        status.update(
            {
                "updated_at": utc_now_iso(),
                "folders": [*folder_results, result.as_dict()],
                "errors": errors,
                "bytes_downloaded": starting_bytes + result.bytes_downloaded,
                "current": {
                    "folder": result.folder,
                    "folder_index": folder_index,
                    "total_folders": total_folders,
                    "uidvalidity": result.uidvalidity,
                    "remote_message_count": result.remote_message_count,
                    "messages_to_download": messages_to_download,
                    "messages_processed": messages_processed,
                    "messages_downloaded": result.messages_downloaded,
                    "messages_skipped": result.messages_skipped,
                    "attachments_downloaded": result.attachments_downloaded,
                    "attachments_skipped": result.attachments_skipped,
                    "bytes_downloaded": starting_bytes + result.bytes_downloaded,
                    "progress_percent": progress_percent,
                },
            }
        )
        self.store.set_sync_status(status)

    def _build_cached_message(
        self,
        folder: str,
        fetched: ImapFetchedMessage,
    ) -> tuple[MessageDetail, list[dict[str, object]], int]:
        message = parse_message_bytes(fetched.raw_bytes)
        parsed = parse_email_message(message, self.settings.max_snippet_chars)
        message_ref = build_message_ref(
            MessageRefPayload(
                folder=folder,
                uid=fetched.uid,
                uidvalidity=fetched.uidvalidity,
                account=self.settings.account_fingerprint,
            ),
            self.settings.message_ref_secret,
        )
        body_text = parsed.body_text
        body_truncated = False
        if len(body_text) > self.settings.max_body_chars:
            body_text = body_text[: self.settings.max_body_chars].rstrip() + "..."
            body_truncated = True

        attachment_rows: list[dict[str, object]] = []
        attachment_models: list[Attachment] = []
        downloaded_bytes = 0
        parts_by_id = {attachment_id: part for attachment_id, part in iter_attachment_parts(message)}

        for attachment in parsed.attachments:
            payload_bytes = parts_by_id[attachment.attachment_id].get_payload(decode=True) or b""
            warnings = list(parsed.parsing_warnings)
            raw_bytes = None
            extracted_text = None
            skipped_reason = None
            if len(payload_bytes) > self.settings.max_attachment_bytes:
                skipped_reason = "Attachment exceeded MAX_ATTACHMENT_BYTES and raw bytes were not cached."
                warnings.append(skipped_reason)
            else:
                raw_bytes = payload_bytes
                downloaded_bytes += len(payload_bytes)
                extraction = extract_text(
                    attachment=attachment,
                    payload=payload_bytes,
                    max_bytes=self.settings.max_attachment_bytes,
                    max_chars=self.settings.max_attachment_extract_chars,
                    mode="all_small",
                )
                extracted_text = extraction.text
                warnings.extend(extraction.warnings)
                if extracted_text:
                    attachment.extracted_text_available = True
                    attachment.extracted_text_excerpt = extracted_text[: self.settings.max_snippet_chars]

            attachment_models.append(attachment)
            attachment_rows.append(
                {
                    "attachment": attachment.model_dump(),
                    "raw_bytes": raw_bytes,
                    "extracted_text": extracted_text,
                    "warnings": warnings,
                    "skipped_reason": skipped_reason,
                }
            )

        detail = MessageDetail(
            message_ref=message_ref,
            folder=folder,
            uid=fetched.uid,
            direction=self._infer_direction(folder, parsed.sender_email),
            imap_internal_date_iso=fetched.internal_date_iso,
            header_date_iso=parsed.header_date_iso,
            subject=parsed.subject,
            normalized_subject=parsed.normalized_subject,
            from_=parsed.from_participant,
            sender_email=parsed.sender_email,
            to=parsed.to,
            cc=parsed.cc,
            bcc=parsed.bcc,
            reply_to=parsed.reply_to,
            message_id=parsed.message_id,
            in_reply_to=parsed.in_reply_to,
            references=parsed.references,
            snippet=parsed.snippet,
            body_text=body_text,
            body_text_truncated=body_truncated,
            has_attachments=bool(parsed.attachments),
            attachment_count=len(parsed.attachments),
            attachment_names=[attachment.filename or attachment.attachment_id for attachment in parsed.attachments],
            attachments=attachment_models,
            thread_key_candidates=parsed.thread_key_candidates,
            related_thread_keys=parsed.thread_key_candidates,
            relevance_notes=[],
            parsing_warnings=parsed.parsing_warnings,
        )
        return detail, attachment_rows, downloaded_bytes

    def _infer_direction(self, folder: str, sender_email: str | None) -> Direction:
        if self.access.is_sent_folder_allowed(folder):
            return "sent"
        if sender_email and sender_email.lower() == self.settings.imap_username.lower():
            return "sent"
        return "received"


def _months_ago_utc(months: int) -> datetime:
    if months <= 0:
        raise ValueError("since_months must be a positive integer")
    now = datetime.now(tz=UTC)
    month_index = (now.year * 12 + now.month - 1) - months
    year = month_index // 12
    month = month_index % 12 + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day)
