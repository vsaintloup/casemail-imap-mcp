from __future__ import annotations

from casemail_imap_mcp.models import MessageSummary, Participant
from casemail_imap_mcp.threading_utils import build_thread


def _summary(
    *,
    message_ref: str,
    subject: str,
    normalized_subject: str,
    sender: str,
    to: str,
    message_id: str,
    in_reply_to: str | None,
    references: list[str],
    header_date_iso: str,
    direction: str = "received",
    attachment_count: int = 0,
) -> MessageSummary:
    return MessageSummary(
        message_ref=message_ref,
        folder="Client/ABC",
        uid=1,
        direction=direction,
        imap_internal_date_iso=header_date_iso,
        header_date_iso=header_date_iso,
        subject=subject,
        normalized_subject=normalized_subject,
        from_=Participant(name=None, email=sender, raw=sender),
        sender_email=sender,
        to=[Participant(name=None, email=to, raw=to)],
        cc=[],
        bcc=[],
        reply_to=[],
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        snippet=subject,
        has_attachments=attachment_count > 0,
        attachment_count=attachment_count,
        attachment_names=["draft.docx"] if attachment_count else [],
        thread_key_candidates=[item for item in [message_id, in_reply_to, normalized_subject] if item],
        relevance_notes=[],
        parsing_warnings=[],
    )


def test_build_thread_prefers_header_linkage() -> None:
    seed = _summary(
        message_ref="seed",
        subject="Re: Motion Record",
        normalized_subject="motion record",
        sender="client@example.com",
        to="lawyer@example.com",
        message_id="<m1>",
        in_reply_to=None,
        references=[],
        header_date_iso="2026-02-01T10:00:00+00:00",
    )
    reply = _summary(
        message_ref="reply",
        subject="Re: Motion Record",
        normalized_subject="motion record",
        sender="lawyer@example.com",
        to="client@example.com",
        message_id="<m2>",
        in_reply_to="<m1>",
        references=["<m1>"],
        header_date_iso="2026-02-01T12:00:00+00:00",
        direction="sent",
        attachment_count=1,
    )

    thread = build_thread(seed, [seed, reply], depth=10)

    assert [entry.message_ref for entry in thread] == ["seed", "reply"]
    assert thread[1].linkage_basis == "headers"
    assert thread[1].attachments_summary == ["draft.docx"]


def test_build_thread_uses_subject_and_participants_when_headers_missing() -> None:
    seed = _summary(
        message_ref="seed",
        subject="Call prep",
        normalized_subject="call prep",
        sender="client@example.com",
        to="lawyer@example.com",
        message_id="<m3>",
        in_reply_to=None,
        references=[],
        header_date_iso="2026-02-05T10:00:00+00:00",
    )
    follow_up = _summary(
        message_ref="follow",
        subject="Re: Call Prep",
        normalized_subject="call prep",
        sender="lawyer@example.com",
        to="client@example.com",
        message_id="<m4>",
        in_reply_to=None,
        references=[],
        header_date_iso="2026-02-06T10:00:00+00:00",
        direction="sent",
    )

    thread = build_thread(seed, [seed, follow_up], depth=10)

    assert len(thread) == 2
    assert thread[1].linkage_basis == "subject"

