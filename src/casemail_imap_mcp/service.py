from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import logging
from typing import Iterable

from .attachments import extract_text
from .cache import EncryptedCache
from .config import Settings
from .imap_client import ImapFetchedMessage, ReadOnlyImapClient
from .models import (
    Attachment,
    AttachmentReadMode,
    Direction,
    ExtractionMode,
    FolderInfo,
    MessageDetail,
    MessageRefPayload,
    MessageSummary,
    SearchDirection,
    TimelineEntry,
)
from .parsing import iter_attachment_parts, parse_email_message, parse_message_bytes
from .security import (
    AccessDeniedError,
    FolderAccessController,
    InvalidMessageRefError,
    build_content_hash,
    build_message_ref,
    parse_message_ref,
)
from .threading_utils import build_thread, candidate_from_summary, classify_linkage

logger = logging.getLogger(__name__)


class CaseMailService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.access = FolderAccessController(settings)
        self.cache = EncryptedCache(settings)

    def list_folders(self, include_counts: bool = True, folder_pattern: str | None = None) -> dict[str, object]:
        pattern = folder_pattern.lower() if folder_pattern else None
        with self._open_client() as client:
            folders = []
            for folder in client.list_folders(include_counts=include_counts):
                if not self.access.is_any_allowed(folder.name):
                    continue
                if pattern and pattern not in folder.name.lower():
                    continue
                folders.append(
                    FolderInfo(
                        name=folder.name,
                        delimiter=folder.delimiter,
                        flags=folder.flags,
                        message_count=folder.message_count if include_counts else None,
                        is_sent_candidate=self.access.is_sent_folder_allowed(folder.name),
                    ).model_dump()
                )
        return {"folders": folders}

    def search_messages(
        self,
        case_folder: str,
        include_sent: bool = False,
        sent_folders: list[str] | None = None,
        query: str | None = None,
        correspondents: list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        has_attachments: bool | None = None,
        direction: SearchDirection = "any",
        limit: int = 50,
    ) -> dict[str, object]:
        case_folder = self.access.ensure_case_folder(case_folder)
        capped_limit = min(limit, self.settings.max_results)
        since_dt = _coerce_start_datetime(since)
        until_dt = _coerce_end_datetime(until)

        with self._open_client() as client:
            accessible_names = [folder.name for folder in client.list_folders(include_counts=False) if self.access.is_any_allowed(folder.name)]
            resolved_sent_folders = self.access.resolve_sent_folders(sent_folders, accessible_names)
            target_folders = [case_folder]
            if include_sent and direction in {"any", "sent"}:
                target_folders.extend(folder for folder in resolved_sent_folders if folder != case_folder)

            summaries = self._collect_summaries(
                client=client,
                folders=target_folders,
                since_dt=since_dt,
                until_dt=until_dt,
                query=query,
                correspondents=correspondents or [],
                has_attachments=has_attachments,
                direction=direction,
                limit=capped_limit,
            )

        return {
            "case_folder": case_folder,
            "include_sent": include_sent,
            "sent_folders": resolved_sent_folders if include_sent else [],
            "messages": [summary.model_dump(by_alias=True) for summary in summaries[:capped_limit]],
        }

    def read_message(
        self,
        message_ref: str,
        include_body: bool = True,
        include_attachment_metadata: bool = True,
        extract_attachment_text: ExtractionMode = "supported",
    ) -> dict[str, object]:
        payload = self._validate_message_ref(message_ref)
        with self._open_client() as client:
            fetched = client.fetch_message(payload.folder, payload.uid, payload.uidvalidity)
            detail = self._build_message_detail(
                payload.folder,
                fetched,
                include_body=include_body,
                include_attachment_metadata=include_attachment_metadata,
                extract_attachment_text=extract_attachment_text,
            )
        return {"message": detail.model_dump(by_alias=True)}

    def get_thread(
        self,
        case_folder: str,
        seed_message_ref: str,
        include_sent: bool = True,
        sent_folders: list[str] | None = None,
        depth: int = 50,
    ) -> dict[str, object]:
        case_folder = self.access.ensure_case_folder(case_folder)
        payload = self._validate_message_ref(seed_message_ref)
        capped_depth = min(depth, self.settings.max_results)
        with self._open_client() as client:
            seed_fetched = client.fetch_message(payload.folder, payload.uid, payload.uidvalidity)
            seed_summary = self._build_message_summary(payload.folder, seed_fetched)
            seed_dt = _coerce_any_datetime(seed_summary.header_date_iso or seed_summary.imap_internal_date_iso)
            since_dt = seed_dt - timedelta(days=180) if seed_dt else None
            until_dt = seed_dt + timedelta(days=180) if seed_dt else None

            accessible_names = [folder.name for folder in client.list_folders(include_counts=False) if self.access.is_any_allowed(folder.name)]
            resolved_sent_folders = self.access.resolve_sent_folders(sent_folders, accessible_names)
            folders = [case_folder]
            if include_sent:
                folders.extend(folder for folder in resolved_sent_folders if folder != case_folder)
            candidates = self._collect_summaries(
                client=client,
                folders=folders,
                since_dt=since_dt,
                until_dt=until_dt,
                query=None,
                correspondents=[],
                has_attachments=None,
                direction="any",
                limit=min(self.settings.max_thread_scan, capped_depth * 4),
            )

        thread_entries = build_thread(seed_summary, candidates, capped_depth)
        return {
            "case_folder": case_folder,
            "seed_message_ref": seed_message_ref,
            "include_sent": include_sent,
            "thread": [entry.model_dump() for entry in thread_entries],
        }

    def find_related_sent(
        self,
        case_folder: str,
        sent_folders: list[str] | None = None,
        parties: list[str] | None = None,
        subject: str | None = None,
        since: str | None = None,
        until: str | None = None,
        date_window_days: int = 60,
        limit: int = 50,
    ) -> dict[str, object]:
        case_folder = self.access.ensure_case_folder(case_folder)
        capped_limit = min(limit, self.settings.max_results)
        since_dt = _coerce_start_datetime(since)
        until_dt = _coerce_end_datetime(until)

        with self._open_client() as client:
            accessible_names = [folder.name for folder in client.list_folders(include_counts=False) if self.access.is_any_allowed(folder.name)]
            resolved_sent_folders = self.access.resolve_sent_folders(sent_folders, accessible_names)
            case_summaries = self._collect_summaries(
                client=client,
                folders=[case_folder],
                since_dt=since_dt,
                until_dt=until_dt,
                query=subject,
                correspondents=parties or [],
                has_attachments=None,
                direction="any",
                limit=self.settings.max_search_scan,
            )
            sent_summaries = self._collect_summaries(
                client=client,
                folders=resolved_sent_folders,
                since_dt=(since_dt - timedelta(days=date_window_days)) if since_dt else None,
                until_dt=(until_dt + timedelta(days=date_window_days)) if until_dt else None,
                query=subject,
                correspondents=parties or [],
                has_attachments=None,
                direction="sent",
                limit=self.settings.max_search_scan,
            )

        matches: list[dict[str, object]] = []
        case_candidates = [candidate_from_summary(summary) for summary in case_summaries]
        for sent_summary in sent_summaries:
            sent_candidate = candidate_from_summary(sent_summary)
            notes: list[str] = []
            best_rank = -1
            best_basis = None
            for case_candidate in case_candidates:
                basis = classify_linkage(case_candidate, sent_candidate, case_candidate.header_keys, date_window_days)
                if basis is None:
                    continue
                rank = {"headers": 4, "subject": 3, "participant_heuristic": 2, "date_window": 1}[basis]
                if rank > best_rank:
                    best_rank = rank
                    best_basis = basis
            if best_basis is None:
                continue
            notes.append(f"Linked to case folder {case_folder} using {best_basis}.")
            if parties:
                notes.append("Participant filter matched one or more supplied parties.")
            if subject:
                notes.append("Subject filter contributed to candidate selection.")
            matches.append(
                {
                    "message": sent_summary.model_dump(by_alias=True),
                    "linkage_basis": best_basis,
                    "explanation": notes,
                }
            )

        matches.sort(key=lambda item: item["message"]["header_date_iso"] or item["message"]["imap_internal_date_iso"] or "", reverse=True)
        return {
            "case_folder": case_folder,
            "sent_folders": resolved_sent_folders,
            "matches": matches[:capped_limit],
        }

    def read_attachment(
        self,
        message_ref: str,
        attachment_id: str,
        extraction_mode: AttachmentReadMode = "text",
    ) -> dict[str, object]:
        payload = self._validate_message_ref(message_ref)
        with self._open_client() as client:
            fetched = client.fetch_message(payload.folder, payload.uid, payload.uidvalidity)
            message = parse_message_bytes(fetched.raw_bytes)
            parsed = parse_email_message(message, self.settings.max_snippet_chars)
            attachments_by_id = {attachment.attachment_id: attachment for attachment in parsed.attachments}
            parts_by_id = {item_id: part for item_id, part in iter_attachment_parts(message)}

        if attachment_id not in attachments_by_id or attachment_id not in parts_by_id:
            raise ValueError(f"attachment_id not found: {attachment_id}")

        attachment = attachments_by_id[attachment_id]
        payload_bytes = parts_by_id[attachment_id].get_payload(decode=True) or b""
        extracted_text = None
        warnings = list(parsed.parsing_warnings)
        if extraction_mode != "raw_metadata":
            extracted_text = self._extract_attachment_text(
                payload.folder,
                fetched.uidvalidity,
                fetched.uid,
                attachment,
                payload_bytes,
                "ocr" if extraction_mode == "ocr" else "supported",
            )
            if extracted_text:
                attachment.extracted_text_available = True
                attachment.extracted_text_excerpt = extracted_text[: self.settings.max_snippet_chars]
        return {
            "attachment": {
                **attachment.model_dump(),
                "extracted_text": extracted_text,
                "parsing_warnings": warnings,
            }
        }

    def case_timeline(
        self,
        case_folder: str,
        include_sent: bool = True,
        sent_folders: list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 200,
    ) -> dict[str, object]:
        case_folder = self.access.ensure_case_folder(case_folder)
        capped_limit = min(limit, self.settings.max_thread_scan)
        since_dt = _coerce_start_datetime(since)
        until_dt = _coerce_end_datetime(until)

        with self._open_client() as client:
            accessible_names = [folder.name for folder in client.list_folders(include_counts=False) if self.access.is_any_allowed(folder.name)]
            resolved_sent_folders = self.access.resolve_sent_folders(sent_folders, accessible_names)
            folders = [case_folder]
            if include_sent:
                folders.extend(folder for folder in resolved_sent_folders if folder != case_folder)
            summaries = self._collect_summaries(
                client=client,
                folders=folders,
                since_dt=since_dt,
                until_dt=until_dt,
                query=None,
                correspondents=[],
                has_attachments=None,
                direction="any",
                limit=capped_limit,
            )

        timeline: list[TimelineEntry] = []
        for summary in sorted(summaries, key=lambda item: item.header_date_iso or item.imap_internal_date_iso or ""):
            timeline.append(
                TimelineEntry(
                    message_ref=summary.message_ref,
                    folder=summary.folder,
                    direction=summary.direction,
                    date=summary.header_date_iso or summary.imap_internal_date_iso,
                    subject=summary.subject,
                    normalized_subject=summary.normalized_subject,
                    participants=self._collect_unique_participants(summary),
                    attachment_names=summary.attachment_names,
                    thread_linkage_hints=summary.thread_key_candidates,
                    snippet=summary.snippet,
                )
            )
        return {
            "case_folder": case_folder,
            "include_sent": include_sent,
            "timeline": [entry.model_dump() for entry in timeline[:capped_limit]],
        }

    def _open_client(self) -> ReadOnlyImapClient:
        return ReadOnlyImapClient(self.settings)

    def _validate_message_ref(self, message_ref: str) -> MessageRefPayload:
        payload = parse_message_ref(message_ref, self.settings.message_ref_secret, self.settings.account_fingerprint)
        self.access.ensure_any_folder(payload.folder)
        return payload

    def _collect_summaries(
        self,
        client: ReadOnlyImapClient,
        folders: Iterable[str],
        since_dt: datetime | None,
        until_dt: datetime | None,
        query: str | None,
        correspondents: list[str],
        has_attachments: bool | None,
        direction: SearchDirection,
        limit: int,
    ) -> list[MessageSummary]:
        results: list[MessageSummary] = []
        seen_keys: set[str] = set()
        for folder in folders:
            uidvalidity, uids = client.search_uids(folder, since_dt, until_dt)
            for uid in reversed(uids[-self.settings.max_search_scan :]):
                fetched = client.fetch_message(folder, uid, uidvalidity)
                summary = self._build_message_summary(folder, fetched)
                if not self._matches_filters(summary, query, correspondents, has_attachments, direction):
                    continue
                dedupe_key = summary.message_id or build_content_hash(
                    [summary.folder, str(summary.uid), summary.normalized_subject, summary.snippet, summary.header_date_iso or ""]
                )
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                results.append(summary)
                if len(results) >= limit:
                    return sorted(results, key=lambda item: item.header_date_iso or item.imap_internal_date_iso or "", reverse=True)
        return sorted(results, key=lambda item: item.header_date_iso or item.imap_internal_date_iso or "", reverse=True)

    def _build_message_summary(self, folder: str, fetched: ImapFetchedMessage) -> MessageSummary:
        message = parse_message_bytes(fetched.raw_bytes)
        parsed = parse_email_message(message, self.settings.max_snippet_chars)
        payload = MessageRefPayload(
            folder=folder,
            uid=fetched.uid,
            uidvalidity=fetched.uidvalidity,
            account=self.settings.account_fingerprint,
        )
        message_ref = build_message_ref(payload, self.settings.message_ref_secret)
        return MessageSummary(
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
            has_attachments=bool(parsed.attachments),
            attachment_count=len(parsed.attachments),
            attachment_names=[attachment.filename or attachment.attachment_id for attachment in parsed.attachments],
            thread_key_candidates=parsed.thread_key_candidates,
            relevance_notes=[],
            parsing_warnings=parsed.parsing_warnings,
        )

    def _build_message_detail(
        self,
        folder: str,
        fetched: ImapFetchedMessage,
        include_body: bool,
        include_attachment_metadata: bool,
        extract_attachment_text: ExtractionMode,
    ) -> MessageDetail:
        message = parse_message_bytes(fetched.raw_bytes)
        parsed = parse_email_message(message, self.settings.max_snippet_chars)
        summary = self._build_message_summary(folder, fetched)
        cache_key = self._message_cache_key(folder, fetched.uidvalidity, fetched.uid)
        body_text = None
        body_text_truncated = False
        if include_body:
            body_text = self.cache.get_message_body(cache_key) if self.cache.enabled else None
            if body_text is None:
                body_text = parsed.body_text
                if self.cache.enabled and body_text:
                    self.cache.put_message_body(cache_key, body_text)
            if body_text and len(body_text) > self.settings.max_body_chars:
                body_text = body_text[: self.settings.max_body_chars].rstrip() + "..."
                body_text_truncated = True

        attachments: list[Attachment] = []
        if include_attachment_metadata:
            attachments = [Attachment.model_validate(item.model_dump()) for item in parsed.attachments]
            if extract_attachment_text != "none":
                part_lookup = {attachment_id: part for attachment_id, part in iter_attachment_parts(message)}
                for attachment in attachments:
                    part = part_lookup.get(attachment.attachment_id)
                    if part is None:
                        continue
                    payload_bytes = part.get_payload(decode=True) or b""
                    extracted = self._extract_attachment_text(
                        folder,
                        fetched.uidvalidity,
                        fetched.uid,
                        attachment,
                        payload_bytes,
                        extract_attachment_text,
                    )
                    if extracted:
                        attachment.extracted_text_available = True
                        attachment.extracted_text_excerpt = extracted[: self.settings.max_snippet_chars]

        return MessageDetail(
            **summary.model_dump(),
            body_text=body_text,
            body_text_truncated=body_text_truncated,
            attachments=attachments,
            related_thread_keys=parsed.thread_key_candidates,
            parsing_warnings=parsed.parsing_warnings,
        )

    def _extract_attachment_text(
        self,
        folder: str,
        uidvalidity: int,
        uid: int,
        attachment: Attachment,
        payload_bytes: bytes,
        mode: str,
    ) -> str | None:
        cache_key = self._attachment_cache_key(folder, uidvalidity, uid, attachment.attachment_id)
        cached = self.cache.get_attachment_text(cache_key) if self.cache.enabled else None
        if cached is not None:
            return cached
        extraction_mode = "all_small" if mode == "all_small" else mode
        result = extract_text(
            attachment=attachment,
            payload=payload_bytes,
            max_bytes=self.settings.max_attachment_bytes,
            max_chars=self.settings.max_attachment_extract_chars,
            mode=extraction_mode,
        )
        if result.text and self.cache.enabled:
            self.cache.put_attachment_text(cache_key, result.text)
        return result.text

    def _infer_direction(self, folder: str, sender_email: str | None) -> Direction:
        if self.access.is_sent_folder_allowed(folder):
            return "sent"
        if sender_email and sender_email.lower() == self.settings.imap_username.lower():
            return "sent"
        return "received"

    def _matches_filters(
        self,
        summary: MessageSummary,
        query: str | None,
        correspondents: list[str],
        has_attachments: bool | None,
        direction: SearchDirection,
    ) -> bool:
        if direction != "any" and summary.direction != direction:
            return False
        if has_attachments is not None and summary.has_attachments != has_attachments:
            return False
        if query:
            q = query.lower()
            haystacks = [
                summary.subject.lower(),
                summary.normalized_subject.lower(),
                summary.snippet.lower(),
            ]
            if not any(q in haystack for haystack in haystacks):
                return False
        if correspondents:
            needles = [item.lower() for item in correspondents]
            participants = self._collect_unique_participants(summary)
            searchable = []
            for participant in participants:
                searchable.extend(filter(None, [participant.name.lower() if participant.name else None, participant.email, participant.raw.lower()]))
            if not any(any(needle in value for value in searchable) for needle in needles):
                return False
        return True

    def _collect_unique_participants(self, summary: MessageSummary) -> list:
        seen = set()
        participants = []
        for participant in [summary.from_, *summary.to, *summary.cc, *summary.reply_to]:
            key = (participant.email, participant.raw)
            if key in seen:
                continue
            seen.add(key)
            participants.append(participant)
        return participants

    def _message_cache_key(self, folder: str, uidvalidity: int, uid: int) -> str:
        return build_content_hash([self.settings.account_fingerprint, folder, str(uidvalidity), str(uid)])

    def _attachment_cache_key(self, folder: str, uidvalidity: int, uid: int, attachment_id: str) -> str:
        return build_content_hash([self.settings.account_fingerprint, folder, str(uidvalidity), str(uid), attachment_id])


def _coerce_any_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _coerce_start_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if "T" in value:
        return _coerce_any_datetime(value)
    parsed = date.fromisoformat(value)
    return datetime.combine(parsed, time.min, tzinfo=UTC)


def _coerce_end_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if "T" in value:
        return _coerce_any_datetime(value)
    parsed = date.fromisoformat(value) + timedelta(days=1)
    return datetime.combine(parsed, time.min, tzinfo=UTC)
