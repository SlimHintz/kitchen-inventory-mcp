"""Correction 5: /health must be reachable without a bearer token (or
Cloudflare Tunnel health checks 401 forever), and must return no
inventory data -- status and version only.

Also covers: the auth middleware now also has to check issued OAuth access
tokens against the DB, so these tests need real schema in place (mirroring
a real deployment, where migrations always run before the server starts)
rather than a bare, unmigrated tmp_path file.
"""

import pytest
from starlette.testclient import TestClient

import kitchen_inventory_mcp.db as db_module
from kitchen_inventory_mcp.models import Base
from kitchen_inventory_mcp.server import build_app


@pytest.fixture(autouse=True)
def _reset_db_singleton():
    """db.py caches its engine/session on first use for the life of the
    process -- fine for a real server (one fixed DB path per run), but
    across tests with different monkeypatched KITCHEN_MCP_DB_PATH values
    it would silently keep pointing at whichever DB was touched first."""
    db_module._engine = None
    db_module._SessionLocal = None
    yield
    db_module._engine = None
    db_module._SessionLocal = None


def _migrated_db_path(tmp_path):
    db_path = tmp_path / "t.db"
    engine = db_module.make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return db_path


def test_health_bypasses_auth_and_returns_only_status(monkeypatch, tmp_path):
    monkeypatch.setenv("KITCHEN_MCP_TOKEN", "s3cret")
    monkeypatch.setenv("KITCHEN_MCP_DB_PATH", str(_migrated_db_path(tmp_path)))

    client = TestClient(build_app())
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"status", "version"}
    assert body["status"] == "ok"


def test_mcp_endpoint_requires_bearer_token(monkeypatch, tmp_path):
    monkeypatch.setenv("KITCHEN_MCP_TOKEN", "s3cret")
    monkeypatch.setenv("KITCHEN_MCP_DB_PATH", str(_migrated_db_path(tmp_path)))

    with TestClient(build_app()) as client:
        no_auth = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert no_auth.status_code == 401
        assert "WWW-Authenticate" not in no_auth.headers  # KITCHEN_MCP_PUBLIC_URL unset in this test

        wrong = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer nope"},
        )
        assert wrong.status_code == 401

        right = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": "Bearer s3cret",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert right.status_code != 401


def test_mcp_endpoint_accepts_oauth_access_token(monkeypatch, tmp_path):
    monkeypatch.setenv("KITCHEN_MCP_TOKEN", "s3cret")
    monkeypatch.setenv("KITCHEN_MCP_DB_PATH", str(_migrated_db_path(tmp_path)))
    monkeypatch.setenv("KITCHEN_MCP_PUBLIC_URL", "https://kitchen.example.com")

    from kitchen_inventory_mcp import oauth

    session = db_module.new_session()
    try:
        session.add(
            oauth.OAuthAccessToken(
                token="issued-token-abc",
                client_id="some-client",
                expires_at=oauth.utcnow() + oauth.ACCESS_TOKEN_TTL,
            )
        )
        session.commit()
    finally:
        session.close()

    with TestClient(build_app()) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": "Bearer issued-token-abc",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert response.status_code != 401

        no_auth = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert no_auth.status_code == 401
        assert "oauth-protected-resource" in no_auth.headers["WWW-Authenticate"]
