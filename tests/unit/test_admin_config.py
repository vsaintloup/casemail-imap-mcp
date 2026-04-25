from __future__ import annotations

from pathlib import Path

from casemail_imap_mcp.admin import _config_payload, _read_env, _write_env
from casemail_imap_mcp.cache import PlainSyncStore


def test_env_update_preserves_unrelated_keys_and_password(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("IMAP_PASSWORD=existing-secret\nUNRELATED=value\n", encoding="utf-8")
    existing = _read_env(env_path)

    _write_env(
        env_path,
        existing,
        {
            "IMAP_HOST": "mail.example.com",
            "IMAP_PORT": "993",
            "IMAP_USERNAME": "lawyer@example.com",
            "IMAP_PASSWORD": existing["IMAP_PASSWORD"],
            "IMAP_USE_SSL": "true",
            "MESSAGE_REF_SECRET": "local-secret-value",
        },
    )

    updated = _read_env(env_path)
    assert updated["IMAP_PASSWORD"] == "existing-secret"
    assert updated["UNRELATED"] == "value"
    assert updated["IMAP_HOST"] == "mail.example.com"


def test_config_payload_masks_password(settings) -> None:
    store = PlainSyncStore(settings)
    payload = _config_payload(settings, store)

    assert payload["imap_password_configured"] is True
    assert "imap_password" not in payload

