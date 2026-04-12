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
- `cache.py`: encrypted SQLite cache for normalized body text and extracted attachment text.
- `threading_utils.py`: thread reconstruction and sent-mail correlation heuristics.
- `service.py`: orchestration layer used by the MCP tools.
- `server.py`: FastMCP registration, tool metadata, and Starlette app assembly.

## Safety model

Matter scoping is enforced by tool parameters, not by conversational context. Every content-retrieval tool requires either:

- `case_folder`, validated against the configured allowlist, or
- a signed `message_ref` that already embeds folder context plus account fingerprint and UID validity.

The IMAP layer uses only read-only operations such as `LIST`, `STATUS`, `EXAMINE`, `UID SEARCH`, `UID FETCH`, `NOOP`, and `LOGOUT`. The service never persists raw RFC822 messages or binary attachment payloads. If caching is enabled, only normalized text is stored, encrypted at rest with a local Fernet key.

Email bodies and attachment content are treated as untrusted data. The server never executes attachment content, never follows instructions embedded in emails, and annotates suspicious prompt-injection-like strings in `parsing_warnings`.

## Search and thread reconstruction

Search is intentionally conservative:

1. IMAP narrows candidates by folder and date.
2. The service fetches bounded candidate messages.
3. Local filtering applies subject/query, participants, direction, and attachment predicates.
4. Optional sent-folder correlation runs separately and merges results with careful de-duplication.

Thread reconstruction prefers explicit mail headers:

1. `Message-ID`, `In-Reply-To`, `References`
2. normalized subject
3. participant overlap
4. bounded date-window heuristics

Each returned relationship includes a linkage explanation so downstream analysis can distinguish strong links from heuristics.

## Operational choices

- Transport: Streamable HTTP over `/mcp`
- Auth: none in v1, local-only usage
- Cache: enabled by default, encrypted, TTL-bound, text-only
- Deployment: bare Python or Docker, with optional HTTPS tunneling for ChatGPT developer mode

