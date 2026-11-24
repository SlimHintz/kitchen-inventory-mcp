"""Minimal OAuth 2.1 + PKCE wrapper so remote MCP clients' account-level custom
connector flow (web, mobile, and Desktop's GUI connector dialog) has a
handshake to perform. There is no separate user/password system underneath:
/oauth/authorize just checks the same KITCHEN_MCP_TOKEN and, on a match,
issues an access token scoped to whichever client dynamically registered.

Deliberately out of scope: refresh tokens (access tokens just expire after
ACCESS_TOKEN_TTL and the client re-runs the authorize flow), and a real
consent/anti-phishing screen beyond showing the requesting client's name
and redirect target.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, timedelta

from sqlalchemy.orm import Session

from .models import OAuthAccessToken, OAuthAuthorizationCode, OAuthClient, utcnow

AUTH_CODE_TTL = timedelta(minutes=10)
ACCESS_TOKEN_TTL = timedelta(days=30)


def _expired(expires_at) -> bool:
    """SQLite's SQLAlchemy dialect silently drops tzinfo on DateTime
    (timezone=True) round-trips -- a value read back from the DB compares
    naive, even though everything we write is utcnow(). Every value this
    app writes is UTC by convention, so a naive read-back is treated as UTC
    rather than (incorrectly) compared against an aware "now"."""
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at < utcnow()


def verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    if method != "S256" or not code_verifier:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, code_challenge)


def register_client(session: Session, client_name: str | None, redirect_uris: list[str]) -> dict:
    client_id = secrets.token_urlsafe(24)
    session.add(OAuthClient(id=client_id, redirect_uris=redirect_uris, client_name=client_name))
    session.commit()
    return {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }


def get_client(session: Session, client_id: str) -> OAuthClient | None:
    return session.get(OAuthClient, client_id)


def issue_authorization_code(
    session: Session,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
) -> str:
    code = secrets.token_urlsafe(32)
    session.add(
        OAuthAuthorizationCode(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            expires_at=utcnow() + AUTH_CODE_TTL,
        )
    )
    session.commit()
    return code


def exchange_code_for_token(
    session: Session, code: str, client_id: str, redirect_uri: str, code_verifier: str
) -> tuple[str | None, str | None]:
    """Returns (access_token, error_code)."""
    row = session.get(OAuthAuthorizationCode, code)
    if row is None or row.used or _expired(row.expires_at):
        return None, "invalid_grant"
    if row.client_id != client_id or row.redirect_uri != redirect_uri:
        return None, "invalid_grant"
    if not verify_pkce(code_verifier, row.code_challenge, row.code_challenge_method):
        return None, "invalid_grant"

    row.used = True
    token = secrets.token_urlsafe(32)
    session.add(
        OAuthAccessToken(token=token, client_id=client_id, expires_at=utcnow() + ACCESS_TOKEN_TTL)
    )
    session.commit()
    return token, None


def is_valid_access_token(session: Session, token: str) -> bool:
    row = session.get(OAuthAccessToken, token)
    return row is not None and not _expired(row.expires_at)


def render_authorize_form(
    *, client_name: str | None, redirect_uri: str, hidden_fields: dict[str, str], error: str | None
) -> str:
    hidden_html = "\n".join(
        f'<input type="hidden" name="{name}" value="{value}">'
        for name, value in hidden_fields.items()
    )
    error_html = f'<p class="error">{error}</p>' if error else ""
    client_label = client_name or "An application"
    return f"""
<!doctype html>
<html>
<head>
<title>Kitchen Inventory - Authorize</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, sans-serif; background: #1c1c1e; color: #eee;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
  .card {{ background: #2c2c2e; padding: 2rem; border-radius: 12px; max-width: 360px; width: 90%; }}
  h1 {{ font-size: 1.1rem; margin-top: 0; }}
  p.sub {{ color: #aaa; font-size: 0.9rem; }}
  input[type=password] {{ width: 100%; padding: 0.6rem; border-radius: 8px; border: 1px solid #555;
                           background: #1c1c1e; color: #eee; box-sizing: border-box; margin-top: 0.5rem; }}
  button {{ width: 100%; padding: 0.7rem; margin-top: 1rem; border-radius: 8px; border: none;
            background: #0a84ff; color: white; font-size: 1rem; }}
  .error {{ color: #ff6b6b; font-size: 0.9rem; }}
</style>
</head>
<body>
  <div class="card">
    <h1>{client_label} wants to access your kitchen inventory</h1>
    <p class="sub">Redirecting to: {redirect_uri}</p>
    {error_html}
    <form method="post">
      {hidden_html}
      <label for="token">Household token</label>
      <input type="password" name="token" id="token" autofocus required>
      <button type="submit">Authorize</button>
    </form>
  </div>
</body>
</html>
"""
