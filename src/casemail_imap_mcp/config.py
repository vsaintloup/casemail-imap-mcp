from __future__ import annotations

from functools import cached_property
from pathlib import Path
import re

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    imap_host: str = Field(default="", alias="IMAP_HOST")
    imap_port: int = Field(default=993, alias="IMAP_PORT")
    imap_username: str = Field(default="", alias="IMAP_USERNAME")
    imap_password: str = Field(default="", alias="IMAP_PASSWORD")
    imap_use_ssl: bool = Field(default=True, alias="IMAP_USE_SSL")
    imap_timeout_seconds: int = Field(default=30, alias="IMAP_TIMEOUT_SECONDS")
    imap_retry_count: int = Field(default=1, alias="IMAP_RETRY_COUNT")

    case_folder_allowlist_regex: str = Field(default=r".+", alias="CASE_FOLDER_ALLOWLIST_REGEX")
    sent_folder_allowlist_regex: str = Field(default=r"^(Sent|Sent Items)$", alias="SENT_FOLDER_ALLOWLIST_REGEX")
    default_sent_folders: str = Field(default="Sent,Sent Items", alias="DEFAULT_SENT_FOLDERS")
    allow_global_search: bool = Field(default=False, alias="ALLOW_GLOBAL_SEARCH")

    max_results: int = Field(default=50, alias="MAX_RESULTS")
    max_return_bytes: int = Field(default=262144, alias="MAX_RETURN_BYTES")
    max_search_scan: int = Field(default=500, alias="MAX_SEARCH_SCAN")
    max_thread_scan: int = Field(default=400, alias="MAX_THREAD_SCAN")
    max_attachment_bytes: int = Field(default=10485760, alias="MAX_ATTACHMENT_BYTES")
    max_attachment_extract_chars: int = Field(default=20000, alias="MAX_ATTACHMENT_EXTRACT_CHARS")
    max_body_chars: int = Field(default=50000, alias="MAX_BODY_CHARS")
    max_snippet_chars: int = Field(default=400, alias="MAX_SNIPPET_CHARS")
    max_total_sync_bytes_per_run: int = Field(default=1073741824, alias="MAX_TOTAL_SYNC_BYTES_PER_RUN")

    message_ref_secret: str = Field(default="local-dev-message-ref-secret", alias="MESSAGE_REF_SECRET")
    cache_enabled: bool = Field(default=True, alias="CACHE_ENABLED")
    cache_db_path: Path = Field(default=Path(".cache/casemail_cache.sqlite3"), alias="CACHE_DB_PATH")
    cache_key_path: Path = Field(default=Path(".cache/casemail_cache.key"), alias="CACHE_KEY_PATH")
    cache_ttl_hours: float = Field(default=168.0, alias="CACHE_TTL_HOURS")

    @field_validator(
        "app_port",
        "imap_port",
        "imap_timeout_seconds",
        "imap_retry_count",
        "max_results",
        "max_return_bytes",
        "max_search_scan",
        "max_thread_scan",
        "max_attachment_bytes",
        "max_attachment_extract_chars",
        "max_body_chars",
        "max_snippet_chars",
        "max_total_sync_bytes_per_run",
    )
    @classmethod
    def _must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("value must be positive")
        return value

    @field_validator("message_ref_secret")
    @classmethod
    def _secret_strength(cls, value: str) -> str:
        if len(value.strip()) < 16:
            raise ValueError("MESSAGE_REF_SECRET must be at least 16 characters")
        return value

    @cached_property
    def case_folder_regex(self) -> re.Pattern[str]:
        return re.compile(self.case_folder_allowlist_regex, re.IGNORECASE)

    @cached_property
    def sent_folder_regex(self) -> re.Pattern[str]:
        return re.compile(self.sent_folder_allowlist_regex, re.IGNORECASE)

    @cached_property
    def default_sent_folder_list(self) -> list[str]:
        return [item.strip() for item in self.default_sent_folders.split(",") if item.strip()]

    @cached_property
    def account_fingerprint(self) -> str:
        return f"{self.imap_username.lower()}@{self.imap_host.lower()}:{self.imap_port}"

    def ensure_cache_parent_dirs(self) -> None:
        self.cache_db_path.parent.mkdir(parents=True, exist_ok=True)
