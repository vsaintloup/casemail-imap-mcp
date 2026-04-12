from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Iterable

from .models import LinkageBasis, MessageSummary, Participant, ThreadEntry


@dataclass(slots=True)
class ThreadCandidate:
    summary: MessageSummary
    participants: set[str] = field(default_factory=set)
    header_keys: set[str] = field(default_factory=set)
    date: datetime | None = None


def candidate_from_summary(summary: MessageSummary) -> ThreadCandidate:
    participants = participant_emails(summary)
    header_keys = {item for item in [summary.message_id, summary.in_reply_to, *summary.references] if item}
    date = parse_iso(summary.header_date_iso or summary.imap_internal_date_iso)
    return ThreadCandidate(summary=summary, participants=participants, header_keys=header_keys, date=date)


def participant_emails(summary: MessageSummary) -> set[str]:
    emails = set()
    for participant in [summary.from_, *summary.to, *summary.cc, *summary.reply_to]:
        if participant.email:
            emails.add(participant.email.lower())
    return emails


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def classify_linkage(
    seed: ThreadCandidate,
    candidate: ThreadCandidate,
    known_keys: set[str],
    date_window_days: int = 30,
) -> LinkageBasis | None:
    if candidate.summary.message_ref == seed.summary.message_ref:
        return "headers"
    if candidate.header_keys & known_keys:
        return "headers"
    if candidate.summary.normalized_subject and candidate.summary.normalized_subject == seed.summary.normalized_subject:
        if candidate.participants & seed.participants:
            return "subject"
    if candidate.participants & seed.participants:
        if _within_days(seed.date, candidate.date, 14):
            return "participant_heuristic"
    if candidate.summary.normalized_subject == seed.summary.normalized_subject and _within_days(seed.date, candidate.date, date_window_days):
        return "date_window"
    return None


def build_thread(
    seed: MessageSummary,
    candidates: Iterable[MessageSummary],
    depth: int,
) -> list[ThreadEntry]:
    seed_candidate = candidate_from_summary(seed)
    known_keys = set(seed_candidate.header_keys)
    linked: list[tuple[datetime | None, ThreadEntry]] = []

    for summary in candidates:
        candidate = candidate_from_summary(summary)
        linkage = classify_linkage(seed_candidate, candidate, known_keys)
        if linkage is None:
            continue
        known_keys.update(candidate.header_keys)
        linked.append(
            (
                candidate.date,
                ThreadEntry(
                    message_ref=summary.message_ref,
                    direction=summary.direction,
                    date=summary.header_date_iso or summary.imap_internal_date_iso,
                    subject=summary.subject,
                    participants=unique_participants(summary),
                    snippet=summary.snippet,
                    attachments_summary=summary.attachment_names
                    or ([f"{summary.attachment_count} attachment(s)"] if summary.attachment_count else []),
                    linkage_basis=linkage,
                    folder=summary.folder,
                ),
            )
        )

    linked.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=UTC))
    return [entry for _, entry in linked[:depth]]


def unique_participants(summary: MessageSummary) -> list[Participant]:
    seen: set[tuple[str | None, str]] = set()
    results: list[Participant] = []
    for participant in [summary.from_, *summary.to, *summary.cc]:
        key = (participant.email, participant.raw)
        if key in seen:
            continue
        seen.add(key)
        results.append(participant)
    return results


def _within_days(left: datetime | None, right: datetime | None, days: int) -> bool:
    if left is None or right is None:
        return False
    delta = abs(left - right)
    return delta.days <= days
