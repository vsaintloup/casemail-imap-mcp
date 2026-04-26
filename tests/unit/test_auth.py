from __future__ import annotations

from starlette.testclient import TestClient

from casemail_imap_mcp.server import create_app


def test_auth_disabled_by_default_allows_admin(settings) -> None:
    settings.casemail_access_token = ""
    settings.casemail_auth_required = False
    client = TestClient(create_app(settings))

    response = client.get("/admin")

    assert response.status_code == 200


def test_access_token_protects_admin_and_mcp(settings) -> None:
    settings.casemail_access_token = "test-access-token"
    client = TestClient(create_app(settings))

    assert client.get("/admin").status_code == 401
    assert client.post("/mcp").status_code == 401
    assert client.get("/healthz").status_code == 200


def test_bearer_token_allows_protected_routes(settings) -> None:
    settings.casemail_access_token = "test-access-token"
    client = TestClient(create_app(settings))

    response = client.get("/admin", headers={"Authorization": "Bearer test-access-token"})

    assert response.status_code == 200


def test_api_key_header_allows_protected_routes(settings) -> None:
    settings.casemail_access_token = "test-access-token"
    client = TestClient(create_app(settings))

    response = client.get("/admin", headers={"X-API-Key": "test-access-token"})

    assert response.status_code == 200


def test_query_token_sets_admin_cookie(settings) -> None:
    settings.casemail_access_token = "test-access-token"
    client = TestClient(create_app(settings))

    first = client.get("/admin?access_token=test-access-token")
    second = client.get("/admin")

    assert first.status_code == 200
    assert "casemail_access_token" in first.headers["set-cookie"]
    assert second.status_code == 200


def test_path_token_rewrites_to_protected_route(settings) -> None:
    settings.casemail_access_token = "test-access-token"
    client = TestClient(create_app(settings))

    response = client.get("/casemail/test-access-token/admin")

    assert response.status_code == 200


def test_path_token_base_url_rewrites_to_mcp(settings) -> None:
    settings.casemail_access_token = "test-access-token"

    with TestClient(create_app(settings)) as client:
        response = client.post("/casemail/test-access-token")

    assert response.status_code != 401


def test_authenticated_tunnel_host_reaches_mcp(settings) -> None:
    settings.casemail_access_token = "test-access-token"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "0"},
        },
    }

    with TestClient(create_app(settings), base_url="https://example.trycloudflare.com") as client:
        response = client.post("/mcp/", headers={"X-API-Key": "test-access-token"}, json=body)

    assert response.status_code != 421


def test_auth_required_without_token_fails_closed(settings) -> None:
    settings.casemail_access_token = ""
    settings.casemail_auth_required = True
    client = TestClient(create_app(settings))

    response = client.get("/admin")

    assert response.status_code == 503
