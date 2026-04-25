from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import logging

from .cache import NotSyncedError, PlainSyncStore
from .config import Settings
from .imap_client import ReadOnlyImapClient
from .models import (
    AttachmentReadMode,
    ExtractionMode,
    MessageDetail,
    MessageRefPayload,
    MessageSummary,
    SearchDirection,
    TimelineEntry,
)
from .security import FolderAccessController, parse_message_ref
from .threading_utils import build_thread, candidate_from_summary, classify_linkage

logger = logging.getLogger(__name__)


class CaseMailService:
    def __init__(self, settings: Settings, store: PlainSyncStore | None = None) -> None:
        self.settings = settings
        self.access = FolderAccessController(settings)
        self.store = store or PlainSyncStore(settings)

    def list_folders(self, include_counts: bool = True, folder_pattern: str | None = None) -> dict[str, object]:
        pattern = folder_pattern.lower() if folder_pattern else None
        folders = []
        for folder in self.store.list_cached_folders():
            if pattern and pattern not in folder.name.lower():
                continue
            data = folder.model_dump()
            if not include_counts:
                data["message_count"] = None
            folders.append(data)
        return {"folders": folders, "source": "synced_cache"}

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
        case_folder = self._ensure_synced_case_folder(case_folder)
        capped_limit = min(limit, self.settings.max_results)
        since_dt = _coerce_start_datetime(since)
        until_dt = _coerce_end_datetime(until)
        selected = self.store.list_selected_folders()
        resolved_sent_folders = self.access.resolve_sent_folders(sent_folders, selected)
        target_folders = [case_folder]
        if include_sent and direction in {"any", "sent"}:
            target_folders.extend(folder for folder in resolved_sent_folders if folder != case_folder)

        summaries = self._filter_summaries(
            self.store.list_message_summaries(target_folders),
            query=query,
            correspondents=correspondents or [],
            since_dt=since_dt,
            until_dt=until_dt,
            has_attachments=has_attachments,
            direction=direction,
        )
        return {
            "case_folder": case_folder,
            "include_sent": include_sent,
            "sent_folders": resolved_sent_folders if include_sent else [],
            "messages": [summary.model_dump(by_alias=True) for summary in summaries[:capped_limit]],
            "source": "synced_cache",
        }

    def read_message(
        self,
        message_ref: str,
        include_body: bool = True,
        include_attachment_metadata: bool = True,
        extract_attachment_text: ExtractionMode = "supported",
    ) -> dict[str, object]:
        payload = self._validate_message_ref(message_ref)
        detail = self.store.get_message_detail(payload.folder, payload.uidvalidity, payload.uid)
        if not include_body:
            detail.body_text = None
            detail.body_text_truncated = False
        if not include_attachment_metadata:
            detail.attachments = []
        if extract_attachment_text == "none":
            for attachment in detail.attachments:
                attachment.extracted_text_available = False
                attachment.extracted_text_excerpt = None
        return {"message": detail.model_dump(by_alias=True), "source": "synced_cache"}

    def get_thread(
        self,
        case_folder: str,
        seed_message_ref: str,
        include_sent: bool = True,
        sent_folders: list[str] | None = None,
        depth: int = 50,
    ) -> dict[str, object]:
        case_folder = self._ensure_synced_case_folder(case_folder)
        payload = self._validate_message_ref(seed_message_ref)
        seed_detail = self.store.get_message_detail(payload.folder, payload.uidvalidity, payload.uid)
        seed_summary = MessageSummary.model_validate(seed_detail.model_dump())
        selected = self.store.list_selected_folders()
        resolved_sent_folders = self.access.resolve_sent_folders(sent_folders, selected)
        folders = [case_folder]
        if include_sent:
            folders.extend(folder for folder in resolved_sent_folders if folder != case_folder)
        candidates = self.store.list_message_summaries(folders)
        thread_entries = build_thread(seed_summary, candidates, min(depth, self.settings.max_results))
        return {
            "case_folder": case_folder,
            "seed_message_ref": seed_message_ref,
            "include_sent": include_sent,
            "thread": [entry.model_dump() for entry in thread_entries],
            "source": "synced_cache",
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
        case_folder = self._ensure_synced_case_folder(case_folder)
        capped_limit = min(limit, self.settings.max_results)
        since_dt = _coerce_start_datetime(since)
        until_dt = _coerce_end_datetime(until)
        selected = self.store.list_selected_folders()
        resolved_sent_folders = self.access.resolve_sent_folders(sent_folders, selected)

        case_summaries = self._filter_summaries(
            self.store.list_message_summaries([case_folder]),
            query=subject,
            correspondents=parties or [],
            since_dt=since_dt,
            until_dt=until_dt,
            has_attachments=None,
            direction="any",
        )
        sent_summaries = self._filter_summaries(
            self.store.list_message_summaries(resolved_sent_folders),
            query=subject,
            correspondents=parties or [],
            since_dt=(since_dt - timedelta(days=date_window_days)) if since_dt else None,
            until_dt=(until_dt + timedelta(days=date_window_days)) if until_dt else None,
            has_attachments=None,
            direction="sent",
        )

        matches: list[dict[str, object]] = []
        case_candidates = [candidate_from_summary(summary) for summary in case_summaries]
        for sent_summary in sent_summaries:
            sent_candidate = candidate_from_summary(sent_summary)
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
            notes = [f"Linked to case folder {case_folder} using {best_basis}."]
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
            "source": "synced_cache",
        }

    def read_attachment(
        self,
        message_ref: str,
        attachment_id: str,
        extraction_mode: AttachmentReadMode = "text",
    ) -> dict[str, object]:
        payload = self._validate_message_ref(message_ref)
        row = self.store.get_attachment(payload.folder, payload.uidvalidity, payload.uid, attachment_id)
        attachment = row["attachment"]
        extracted_text = None if extraction_mode == "raw_metadata" else row["extracted_text"]
        return {
            "attachment": {
                **attachment.model_dump(),
                "extracted_text": extracted_text,
                "raw_bytes_cached": row["raw_bytes_cached"],
                "skipped_reason": row["skipped_reason"],
                "parsing_warnings": row["warnings"],
            },
            "source": "synced_cache",
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
        case_folder = self._ensure_synced_case_folder(case_folder)
        capped_limit = min(limit, self.settings.max_thread_scan)
        since_dt = _coerce_start_datetime(since)
        until_dt = _coerce_end_datetime(until)
        selected = self.store.list_selected_folders()
        resolved_sent_folders = self.access.resolve_sent_folders(sent_folders, selected)
        folders = [case_folder]
        if include_sent:
            folders.extend(folder for folder in resolved_sent_folders if folder != case_folder)
        summaries = self._filter_summaries(
            self.store.list_message_summaries(folders),
            query=None,
            correspondents=[],
            since_dt=since_dt,
            until_dt=until_dt,
            has_attachments=None,
            direction="any",
        )

        entries = []
        for summary in sorted(summaries, key=lambda item: item.header_date_iso or item.imap_internal_date_iso or ""):
            entries.append(
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
            "timeline": [entry.model_dump() for entry in entries[:capped_limit]],
            "source": "synced_cache",
        }

    def _open_client(self) -> ReadOnlyImapClient:
        return ReadOnlyImapClient(self.settings)

    def _validate_message_ref(self, message_ref: str) -> MessageRefPayload:
        payload = parse_message_ref(message_ref, self.settings.message_ref_secret, self.settings.account_fingerprint)
        if payload.folder not in self.store.list_selected_folders():
            raise NotSyncedError("message folder is not selected for local sync")
        return payload

    def _ensure_synced_case_folder(self, folder: str) -> str:
        self.access.ensure_case_folder(folder)
        if folder not in self.store.list_selected_folders():
            raise NotSyncedError("case_folder is not selected for local sync")
        return folder

    def _filter_summaries(
        self,
        summaries: list[MessageSummary],
        *,
        query: str | None,
        correspondents: list[str],
        since_dt: datetime | None,
        until_dt: datetime | None,
        has_attachments: bool | None,
        direction: SearchDirection,
    ) -> list[MessageSummary]:
        results = []
        for summary in summaries:
            message_dt = _summary_datetime(summary)
            if since_dt and (message_dt is None or message_dt < since_dt):
                continue
            if until_dt and (message_dt is None or message_dt >= until_dt):
                continue
            if not self._matches_filters(summary, query, correspondents, has_attachments, direction):
                continue
            results.append(summary)
        return sorted(results, key=lambda item: item.header_date_iso or item.imap_internal_date_iso or "", reverse=True)

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
            haystacks = [summary.subject.lower(), summary.normalized_subject.lower(), summary.snippet.lower()]
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


def _summary_datetime(summary: MessageSummary) -> datetime | None:
    return _coerce_any_datetime(summary.header_date_iso or summary.imap_internal_date_iso)


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

