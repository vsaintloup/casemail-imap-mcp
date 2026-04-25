# CaseMail IMAP v1 Technical Design

## Goals

CaseMail IMAP is a local-only, read-only MCP server for matter-scoped IMAP review. The server is designed to let ChatGPT inspect one legal matter folder at a time, correlate replies from configured sent folders, and return structured evidence for timeline reconstruction and billing review.

The server does not send, delete, move, flag, or mutate mail. It also does not perform legal analysis or billing calculations.

## Architecture

The implementation is split into small modules:

- `config.py`: validated environment-driven settings and safe defaults.
- `security.py`: folder allowlists, signed `message_ref`, prompt-injection warnings, and redaction helpers.
- `imap_client.py`: a narrow read-only IMAP adapter that exposes only safe commands.
- `parsing.py`: RFC 2047 decoding, participant parsing, HTML to text, date normalization, and MIME traversal.
- `attachments.py`: bounded attachment text extraction for supported formats.
- `cache.py`: plain SQLite store for selected folders, synced message data, extracted attachment text, and cached attachment bytes.
- `sync_service.py`: read-only folder synchronization for selected folders.
- `admin.py`: local admin UI and API for configuration, folder selection, and sync.
- `threading_utils.py`: thread reconstruction and sent-mail correlation heuristics.
- `service.py`: orchestration layer used by the MCP tools.
- `server.py`: FastMCP registration, tool metadata, and Starlette app assembly.

## Safety model

Matter scoping is enforced by tool parameters, not by conversational context. Every content-retrieval tool requires either:

- `case_folder`, validated against the configured allowlist, or
- a signed `message_ref` that already embeds folder context plus account fingerprint and UID validity.

The IMAP layer uses only read-only operations such as `LIST`, `STATUS`, `EXAMINE`, `UID SEARCH`, `UID FETCH`, `NOOP`, and `LOGOUT`. The service never persists raw RFC822 messages. During explicit sync, it stores parsed message data and attachment bytes in a plain local SQLite database. The expected protection model is the user's encrypted personal computer, not app-level encryption.

Email bodies and attachment content are treated as untrusted data. The server never executes attachment content, never follows instructions embedded in emails, and annotates suspicious prompt-injection-like strings in `parsing_warnings`.

## Search and thread reconstruction

Search is cache-only:

1. The local admin UI selects folders to expose.
2. Sync downloads missing messages and attachments from those folders.
3. MCP tools query only the synced local SQLite store.
4. If a message or attachment is not synced locally, the tool fails closed.

Thread reconstruction prefers explicit mail headers:

1. `Message-ID`, `In-Reply-To`, `References`
2. normalized subject
3. participant overlap
4. bounded date-window heuristics

Each returned relationship includes a linkage explanation so downstream analysis can distinguish strong links from heuristics.

## Operational choices

- Transport: Streamable HTTP over `/mcp`
- Auth: none in v1, local-only usage
- Admin UI: local operational page at `/admin`
- Cache: enabled by default, plain SQLite, selected-folder scoped, includes attachment bytes up to configured limits
- Deployment: bare Python or Docker, with optional HTTPS tunneling for ChatGPT developer mode
