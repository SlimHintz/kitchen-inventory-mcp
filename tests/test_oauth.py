"""End-to-end coverage of the minimal OAuth wrapper: dynamic client
registration -> authorize (wrong token rejected, correct token issues a
code) -> PKCE-verified token exchange -> the resulting access token works
against /mcp and a wrong PKCE verifier or reused code does not."""

import base64
import hashlib
import secrets
import urllib.parse

import pytest
from starlette.testclient import TestClient

import kitchen_inventory_mcp.db as db_module
from kitchen_inventory_mcp.models import Base
from kitchen_inventory_mcp.server import build_app

TOKEN = "s3cret-household-token"
PUBLIC_URL = "https://kitchen.example.com"


@pytest.fixture(autouse=True)
def _reset_db_singleton():
    db_module._engine = None
    db_module._SessionLocal = None
    yield
    db_module._engine = None
    db_module._SessionLocal = None


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("KITCHEN_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("KITCHEN_MCP_PUBLIC_URL", PUBLIC_URL)
    db_path = tmp_path / "t.db"
    engine = db_module.make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("KITCHEN_MCP_DB_PATH", str(db_path))

    with TestClient(build_app()) as c:
        yield c


def _pkce_pair():
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def test_metadata_endpoints(client):
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == PUBLIC_URL
    assert meta["authorization_endpoint"] == f"{PUBLIC_URL}/oauth/authorize"
    assert meta["token_endpoint"] == f"{PUBLIC_URL}/oauth/token"
    assert meta["registration_endpoint"] == f"{PUBLIC_URL}/oauth/register"
    assert meta["code_challenge_methods_supported"] == ["S256"]

    resource = client.get("/.well-known/oauth-protected-resource").json()
    assert resource["resource"] == f"{PUBLIC_URL}/mcp"


def test_metadata_endpoints_require_public_url_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("KITCHEN_MCP_TOKEN", TOKEN)
    monkeypatch.delenv("KITCHEN_MCP_PUBLIC_URL", raising=False)
    db_path = tmp_path / "t.db"
    engine = db_module.make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("KITCHEN_MCP_DB_PATH", str(db_path))

    with TestClient(build_app()) as c:
        response = c.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 500


def test_register_client(client):
    response = client.post(
        "/oauth/register",
        json={"client_name": "Test Client", "redirect_uris": ["https://client.example/callback"]},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["client_id"]
    assert body["redirect_uris"] == ["https://client.example/callback"]
    assert body["token_endpoint_auth_method"] == "none"


def test_authorize_rejects_unknown_client(client):
    response = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "nonexistent",
            "redirect_uri": "https://client.example/callback",
            "state": "xyz",
            "code_challenge": "abc",
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 400


def test_authorize_rejects_unregistered_redirect_uri(client):
    client_id = client.post(
        "/oauth/register", json={"redirect_uris": ["https://client.example/callback"]}
    ).json()["client_id"]

    response = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://evil.example.com/callback",
            "state": "xyz",
            "code_challenge": "abc",
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 400


def test_authorize_wrong_token_does_not_issue_code(client):
    redirect_uri = "https://client.example/callback"
    client_id = client.post("/oauth/register", json={"redirect_uris": [redirect_uri]}).json()[
        "client_id"
    ]
    _, challenge = _pkce_pair()

    response = client.post(
        "/oauth/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "token": "wrong-token",
        },
    )
    assert response.status_code == 401
    assert "Incorrect token" in response.text


def test_full_authorize_and_token_exchange_flow(client):
    redirect_uri = "https://client.example/callback"
    client_id = client.post(
        "/oauth/register", json={"client_name": "Test Client", "redirect_uris": [redirect_uri]}
    ).json()["client_id"]
    verifier, challenge = _pkce_pair()

    form_page = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert form_page.status_code == 200
    assert "Test Client wants to access" in form_page.text

    authorize_response = client.post(
        "/oauth/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "token": TOKEN,
        },
        follow_redirects=False,
    )
    assert authorize_response.status_code == 302
    location = authorize_response.headers["location"]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    assert query["state"] == ["xyz"]
    code = query["code"][0]

    # Wrong PKCE verifier is rejected.
    bad_exchange = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": "not-the-right-verifier",
        },
    )
    assert bad_exchange.status_code == 400

    # Correct verifier succeeds.
    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token_response.status_code == 200
    body = token_response.json()
    assert body["token_type"] == "Bearer"
    access_token = body["access_token"]

    # The code is single-use -- exchanging it again fails.
    replay = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert replay.status_code == 400

    # The issued access token works against /mcp, same as the static token.
    mcp_response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert mcp_response.status_code != 401
