from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


Direction = Literal["received", "sent"]
ExtractionMode = Literal["none", "supported", "all_small"]
AttachmentReadMode = Literal["text", "ocr", "raw_metadata"]
SearchDirection = Literal["received", "sent", "any"]
LinkageBasis = Literal["headers", "subject", "participant_heuristic", "date_window"]


class Participant(BaseModel):
    name: str | None = None
    email: str | None = None
    raw: str


class Attachment(BaseModel):
    attachment_id: str
    filename: str | None = None
    mime_type: str
    size_bytes: int
    sha256: str
    extracted_text_available: bool
    extracted_text_excerpt: str | None = None
    content_id: str | None = None
    content_disposition: str | None = None


class MessageBase(BaseModel):
    message_ref: str
    folder: str
    uid: int
    direction: Direction
    imap_internal_date_iso: str | None = None
    header_date_iso: str | None = None
    subject: str
    normalized_subject: str
    from_: Participant = Field(alias="from")
    sender_email: str | None = None
    to: list[Participant] = Field(default_factory=list)
    cc: list[Participant] = Field(default_factory=list)
    bcc: list[Participant] = Field(default_factory=list)
    reply_to: list[Participant] = Field(default_factory=list)
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    snippet: str
    has_attachments: bool
    attachment_count: int
    attachment_names: list[str] = Field(default_factory=list)
    thread_key_candidates: list[str] = Field(default_factory=list)
    relevance_notes: list[str] = Field(default_factory=list)
    parsing_warnings: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class MessageSummary(MessageBase):
    pass


class MessageDetail(MessageBase):
    body_text: str | None = None
    body_text_truncated: bool = False
    attachments: list[Attachment] = Field(default_factory=list)
    related_thread_keys: list[str] = Field(default_factory=list)


class FolderInfo(BaseModel):
    name: str
    delimiter: str | None = None
    flags: list[str] = Field(default_factory=list)
    message_count: int | None = None
    is_sent_candidate: bool


class ThreadEntry(BaseModel):
    message_ref: str
    direction: Direction
    date: str | None = None
    subject: str
    participants: list[Participant] = Field(default_factory=list)
    snippet: str
    attachments_summary: list[str] = Field(default_factory=list)
    linkage_basis: LinkageBasis
    folder: str


class TimelineEntry(BaseModel):
    message_ref: str
    folder: str
    direction: Direction
    date: str | None = None
    subject: str
    normalized_subject: str
    participants: list[Participant] = Field(default_factory=list)
    attachment_names: list[str] = Field(default_factory=list)
    thread_linkage_hints: list[str] = Field(default_factory=list)
    snippet: str


class CachedValue(BaseModel):
    ciphertext: bytes
    created_at: datetime
    expires_at: datetime


class MessageRefPayload(BaseModel):
    version: int = 1
    folder: str
    uid: int
    uidvalidity: int
    account: str
