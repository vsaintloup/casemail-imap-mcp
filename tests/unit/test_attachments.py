from __future__ import annotations

from email.message import EmailMessage

from casemail_imap_mcp.attachments import extract_text
from casemail_imap_mcp.models import Attachment
from casemail_imap_mcp.parsing import extract_attachment_metadata, parse_message_bytes


def _attachment(filename: str, mime_type: str) -> Attachment:
    return Attachment(
        attachment_id="1",
        filename=filename,
        mime_type=mime_type,
        size_bytes=0,
        sha256="x",
        extracted_text_available=False,
    )


def test_attachment_metadata_extraction() -> None:
    message = EmailMessage()
    message["Subject"] = "Attachment test"
    message["From"] = "lawyer@example.com"
    message["To"] = "client@example.com"
    message.set_content("See attachment")
    message.add_attachment(b"hello", maintype="text", subtype="plain", filename="note.txt")

    parsed_message = parse_message_bytes(message.as_bytes())
    attachments = extract_attachment_metadata(parsed_message, 100)

    assert len(attachments) == 1
    assert attachments[0].filename == "note.txt"
    assert attachments[0].mime_type == "text/plain"


def test_text_attachment_extraction() -> None:
    attachment = _attachment("note.txt", "text/plain")
    result = extract_text(attachment, b"Line one\nLine two", max_bytes=1024, max_chars=100, mode="supported")
    assert result.text == "Line one\nLine two"


def test_docx_attachment_extraction(sample_docx_bytes: bytes) -> None:
    attachment = _attachment("evidence.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    result = extract_text(attachment, sample_docx_bytes, max_bytes=1024 * 1024, max_chars=1000, mode="supported")
    assert "DOCX evidence line" in (result.text or "")


def test_xlsx_attachment_extraction(sample_xlsx_bytes: bytes) -> None:
    attachment = _attachment("worklog.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    result = extract_text(attachment, sample_xlsx_bytes, max_bytes=1024 * 1024, max_chars=1000, mode="supported")
    assert "Draft motion" in (result.text or "")


def test_pptx_attachment_extraction(sample_pptx_bytes: bytes) -> None:
    attachment = _attachment("slides.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    result = extract_text(attachment, sample_pptx_bytes, max_bytes=1024 * 1024, max_chars=1000, mode="supported")
    assert "Hearing prep checklist" in (result.text or "")


def test_attachment_extraction_respects_size_limit() -> None:
    attachment = _attachment("note.txt", "text/plain")
    result = extract_text(attachment, b"A" * 20, max_bytes=5, max_chars=100, mode="supported")
    assert result.text is None
    assert result.warnings

