from __future__ import annotations

import logging
import re


_SENSITIVE_PATTERNS = [
    re.compile(r"(IMAP_PASSWORD=)([^\\s]+)", re.IGNORECASE),
    re.compile(r"(Authorization:\\s*Bearer\\s+)([^\\s]+)", re.IGNORECASE),
    re.compile(r"(token=)([^&\\s]+)", re.IGNORECASE),
]


def redact_text(value: str) -> str:
    redacted = value
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            safe_args = []
            for arg in record.args:
                safe_args.append(redact_text(arg) if isinstance(arg, str) else arg)
            record.args = tuple(safe_args)
        return True


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    has_filter = any(isinstance(item, RedactingFilter) for item in root.filters)
    if not has_filter:
        root.addFilter(RedactingFilter())

