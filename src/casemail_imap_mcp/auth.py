from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .config import Settings


_AUTH_COOKIE = "casemail_access_token"
_PROTECTED_PREFIXES = ("/admin", "/admin/api", "/mcp", "/readyz")
_PUBLIC_PREFIXES = ("/healthz",)
_PATH_TOKEN_PREFIX = "/casemail"


class AccessTokenMiddleware(BaseHTTPMiddleware):
    """Protect local admin and MCP endpoints when CASEMAIL_ACCESS_TOKEN is set."""

    def __init__(self, app, settings_provider: Callable[[], Settings]) -> None:  # noqa: ANN001
        super().__init__(app)
        self._settings_provider = settings_provider

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path_token = _rewrite_path_token_scope(request)
        if not _requires_auth(request.url.path):
            return await call_next(request)

        settings = self._settings_provider()
        configured_token = settings.casemail_access_token.strip()
        if not configured_token:
            if settings.casemail_auth_required:
                return _unauthorized(request, "CASEMAIL_ACCESS_TOKEN is required but not configured.", status_code=503)
            return await call_next(request)

        supplied_token = _extract_token(request, path_token=path_token)
        if not supplied_token or not hmac.compare_digest(supplied_token, configured_token):
            return _unauthorized(request, "Missing or invalid CaseMail access token.")

        _normalize_authenticated_mcp_host(request, settings)
        response = await call_next(request)
        if request.query_params.get("access_token") == supplied_token and request.url.path.startswith("/admin"):
            # Security-sensitive: a valid one-time query token bootstraps the
            # local admin cookie so fetch() calls do not need to keep secrets in URLs.
            response.set_cookie(
                _AUTH_COOKIE,
                supplied_token,
                httponly=True,
                samesite="lax",
                max_age=settings.casemail_auth_cookie_max_age_seconds,
            )
        return response


def _requires_auth(path: str) -> bool:
    if any(path == prefix or path.startswith(f"{prefix}/") for prefix in _PUBLIC_PREFIXES):
        return False
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in _PROTECTED_PREFIXES)


def _rewrite_path_token_scope(request: Request) -> str | None:
    path = request.scope.get("path", "")
    prefix = f"{_PATH_TOKEN_PREFIX}/"
    if not path.startswith(prefix):
        return None
    parts = path.split("/", 3)
    if len(parts) < 3 or not parts[2]:
        return None
    token = parts[2]
    # Security-sensitive: the path token is a compatibility fallback for
    # clients that cannot reliably attach custom API-key headers. If only the
    # tokenized base URL is provided, map it to the MCP endpoint instead of
    # exposing any broader routing behavior.
    if len(parts) < 4 or not parts[3]:
        rewritten = "/mcp/"
    else:
        rewritten = "/" + parts[3]
    request.scope["path"] = rewritten
    request.scope["raw_path"] = rewritten.encode("ascii", "ignore")
    return token


def _extract_token(request: Request, *, path_token: str | None = None) -> str | None:
    if path_token:
        return path_token.strip()
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    for header_name in ("x-casemail-access-token", "x-api-key", "api-key"):
        header_token = request.headers.get(header_name)
        if header_token:
            return header_token.strip()
    query_token = request.query_params.get("access_token")
    if query_token:
        return query_token.strip()
    cookie_token = request.cookies.get(_AUTH_COOKIE)
    if cookie_token:
        return cookie_token.strip()
    return None


def _normalize_authenticated_mcp_host(request: Request, settings: Settings) -> None:
    path = request.scope.get("path", "")
    if path != "/mcp" and not path.startswith("/mcp/"):
        return

    # Security-sensitive: FastMCP enables DNS-rebinding host checks for local
    # servers. Public tunnels legitimately use their own Host header, so after
    # our token check succeeds we normalize only MCP requests back to the local
    # host value accepted by FastMCP. Unauthenticated requests still fail closed.
    replacement = f"127.0.0.1:{settings.app_port}".encode("ascii")
    headers = []
    replaced = False
    for key, value in request.scope.get("headers", []):
        if key.lower() == b"host":
            headers.append((key, replacement))
            replaced = True
        else:
            headers.append((key, value))
    if not replaced:
        headers.append((b"host", replacement))
    request.scope["headers"] = headers


def _unauthorized(request: Request, message: str, *, status_code: int = 401) -> Response:
    if request.url.path.startswith("/admin") and not request.url.path.startswith("/admin/api"):
        return HTMLResponse(
            (
                "<!doctype html><title>CaseMail locked</title>"
                "<h1>CaseMail access token required</h1>"
                "<p>Open this page with <code>?access_token=YOUR_TOKEN</code>, "
                "or send an <code>Authorization: Bearer</code> header.</p>"
            ),
            status_code=status_code,
        )
    return JSONResponse({"error": message}, status_code=status_code)
