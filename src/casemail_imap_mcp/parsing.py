from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.header import decode_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
import hashlib
import html
import io
import re
from typing import Iterator

from bs4 import BeautifulSoup

from .models import Attachment, Participant
from .security import detect_prompt_injection_warnings


@dataclass(slots=True)
class ParsedEmail:
    subject: str
    normalized_subject: str
    from_participant: Participant
    sender_email: str | None
    to: list[Participant]
    cc: list[Participant]
    bcc: list[Participant]
    reply_to: list[Participant]
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    header_date_iso: str | None
    body_text: str
    snippet: str
    attachments: list[Attachment]
    thread_key_candidates: list[str]
    parsing_warnings: list[str]


def parse_message_bytes(message_bytes: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(message_bytes)


def decode_rfc2047(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    fragments: list[str] = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            encoding = charset or "utf-8"
            try:
                fragments.append(chunk.decode(encoding, "replace"))
            except LookupError:
                fragments.append(chunk.decode("utf-8", "replace"))
        else:
            fragments.append(chunk)
    return "".join(fragments).strip()


def normalize_subject(subject: str) -> str:
    normalized = decode_rfc2047(subject or "")
    normalized = re.sub(r"^\s+", "", normalized)
    normalized = re.sub(r"^(?:(?:re|fw|fwd)\s*(?:\[[0-9]+\])?:\s*)+", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip().lower()


def parse_participants(value: str | None) -> list[Participant]:
    participants: list[Participant] = []
    for name, email in getaddresses([value or ""]):
        raw = ", ".join(part for part in [name, f"<{email}>" if email else None] if part) or email or ""
        participants.append(
            Participant(
                name=decode_rfc2047(name) or None,
                email=email.lower() if email else None,
                raw=raw.strip(),
            )
        )
    return participants


def parse_single_participant(value: str | None) -> Participant:
    participants = parse_participants(value)
    if participants:
        return participants[0]
    return Participant(name=None, email=None, raw=decode_rfc2047(value or ""))


def normalize_datetime(value: str | datetime | None) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def html_to_text(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def get_body_text(message: EmailMessage) -> tuple[str, list[str]]:
    warnings: list[str] = []
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            disposition = (part.get_content_disposition() or "").lower()
            if disposition == "attachment":
                continue
            content_type = part.get_content_type().lower()
            if content_type == "text/plain":
                plain_parts.append(decode_payload(part))
            elif content_type == "text/html":
                html_parts.append(html_to_text(decode_payload(part)))
    else:
        content_type = message.get_content_type().lower()
        if content_type == "text/plain":
            plain_parts.append(decode_payload(message))
        elif content_type == "text/html":
            html_parts.append(html_to_text(decode_payload(message)))

    body_text = "\n\n".join(part for part in plain_parts if part.strip()).strip()
    if not body_text:
        body_text = "\n\n".join(part for part in html_parts if part.strip()).strip()
    if not body_text:
        warnings.append("No body text could be extracted from the message.")
    return body_text, warnings


def build_snippet(value: str, max_chars: int) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "..."


def iter_attachment_parts(message: EmailMessage) -> Iterator[tuple[str, Message]]:
    def walk(part: Message, path: list[int]) -> Iterator[tuple[str, Message]]:
        if part.is_multipart():
            for index, child in enumerate(part.iter_parts(), start=1):
                yield from walk(child, [*path, index])
            return
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disposition == "attachment" or filename:
            attachment_id = ".".join(str(item) for item in path) or "1"
            yield attachment_id, part

    yield from walk(message, [])


def extract_attachment_metadata(message: EmailMessage, max_excerpt_chars: int) -> list[Attachment]:
    attachments: list[Attachment] = []
    for attachment_id, part in iter_attachment_parts(message):
        payload = part.get_payload(decode=True) or b""
        filename = decode_rfc2047(part.get_filename()) or None
        mime_type = part.get_content_type().lower()
        attachments.append(
            Attachment(
                attachment_id=attachment_id,
                filename=filename,
                mime_type=mime_type,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                extracted_text_available=False,
                extracted_text_excerpt=None,
                content_id=part.get("Content-ID"),
                content_disposition=part.get("Content-Disposition"),
            )
        )
    return attachments


def build_thread_key_candidates(
    message_id: str | None,
    in_reply_to: str | None,
    references: Iterable[str],
    normalized_subject: str,
) -> list[str]:
    values = []
    for item in [message_id, in_reply_to, *references, normalized_subject]:
        if item and item not in values:
            values.append(item)
    return values


def parse_references(value: str | None) -> list[str]:
    if not value:
        return []
    return re.findall(r"<[^>]+>", value)


def parse_email_message(message: EmailMessage, max_snippet_chars: int) -> ParsedEmail:
    subject = decode_rfc2047(message.get("Subject"))
    normalized_subject = normalize_subject(subject)
    from_participant = parse_single_participant(message.get("From"))
    sender_email = from_participant.email
    to = parse_participants(message.get("To"))
    cc = parse_participants(message.get("Cc"))
    bcc = parse_participants(message.get("Bcc"))
    reply_to = parse_participants(message.get("Reply-To"))
    message_id = decode_rfc2047(message.get("Message-ID")) or None
    in_reply_to = decode_rfc2047(message.get("In-Reply-To")) or None
    references = parse_references(decode_rfc2047(message.get("References")) or None)
    header_date_iso = normalize_datetime(message.get("Date"))
    body_text, warnings = get_body_text(message)
    warnings.extend(detect_prompt_injection_warnings(body_text))
    attachments = extract_attachment_metadata(message, max_snippet_chars)
    thread_key_candidates = build_thread_key_candidates(message_id, in_reply_to, references, normalized_subject)
    snippet = build_snippet(body_text, max_snippet_chars)

    return ParsedEmail(
        subject=subject,
        normalized_subject=normalized_subject,
        from_participant=from_participant,
        sender_email=sender_email,
        to=to,
        cc=cc,
        bcc=bcc,
        reply_to=reply_to,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        header_date_iso=header_date_iso,
        body_text=body_text,
        snippet=snippet,
        attachments=attachments,
        thread_key_candidates=thread_key_candidates,
        parsing_warnings=warnings,
    )


def parse_imap_list_line(value: bytes) -> tuple[list[str], str | None, str]:
    text = value.decode("utf-8", "replace")
    match = re.match(r"^\((?P<flags>[^)]*)\)\s+(?P<delimiter>NIL|\"[^\"]*\")\s+(?P<name>.+)$", text)
    if not match:
        raise ValueError(f"Unable to parse LIST response: {text}")
    flags = [flag for flag in match.group("flags").split() if flag]
    delimiter_token = match.group("delimiter")
    delimiter = None if delimiter_token == "NIL" else delimiter_token.strip('"')
    raw_name = match.group("name").strip()
    if raw_name.startswith('"') and raw_name.endswith('"'):
        raw_name = raw_name[1:-1]
    return flags, delimiter, decode_imap_utf7(raw_name)


def encode_imap_utf7(value: str) -> str:
    def _encode_chunk(chunk: str) -> str:
        if not chunk:
            return ""
        utf16 = chunk.encode("utf-16-be")
        token = io.BytesIO()
        import base64

        token.write(base64.b64encode(utf16).replace(b"/", b",").rstrip(b"="))
        return "&" + token.getvalue().decode("ascii") + "-"

    result: list[str] = []
    buffer: list[str] = []
    for char in value:
        codepoint = ord(char)
        if 0x20 <= codepoint <= 0x7E and char != "&":
            if buffer:
                result.append(_encode_chunk("".join(buffer)))
                buffer.clear()
            result.append(char)
        elif char == "&":
            if buffer:
                result.append(_encode_chunk("".join(buffer)))
                buffer.clear()
            result.append("&-")
        else:
            buffer.append(char)
    if buffer:
        result.append(_encode_chunk("".join(buffer)))
    return "".join(result)


def decode_imap_utf7(value: str) -> str:
    import base64

    def _decode_match(match: re.Match[str]) -> str:
        token = match.group(1)
        if token == "":
            return "&"
        token = token.replace(",", "/")
        padding = "=" * (-len(token) % 4)
        decoded = base64.b64decode(token + padding)
        return decoded.decode("utf-16-be")

    return re.sub(r"&([^-]*)-", _decode_match, value)
