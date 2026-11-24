"""Bearer-token ASGI middleware.

Rejects any request without a matching Authorization: Bearer <token> header
(the static KITCHEN_MCP_TOKEN, or a valid access token issued by the
minimal OAuth wrapper in oauth.py) with a 401. GET /health and the
/oauth/* + /.well-known/* endpoints are exempt -- they're either a health
check that must never require auth, or part of establishing auth in the
first place.
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config
from .db import new_session

_logger: logging.Logger | None = None

_EXEMPT_PREFIXES = ("/oauth/", "/.well-known/")


def _auth_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger("kitchen_inventory_mcp.auth")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        log_file = Path(config.log_path())
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
    _logger = logger
    return logger


def log_auth_event(message: str, *args) -> None:
    _auth_logger().info(message, *args)


def _is_exempt(path: str, method: str) -> bool:
    if path == "/health" and method == "GET":
        return True
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _is_valid_oauth_access_token(token: str) -> bool:
    from . import oauth  # local import: avoids a hard dependency for callers that don't need it

    session = new_session()
    try:
        return oauth.is_valid_access_token(session, token)
    finally:
        session.close()


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, token: str | None = None):
        self.app = app
        self._token = token
        self._token_env_read = token is not None

    @property
    def token(self) -> str | None:
        if not self._token_env_read:
            return config.auth_token()
        return self._token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if _is_exempt(scope["path"], scope["method"]):
            await self.app(scope, receive, send)
            return

        token = self.token
        if not token:
            response = JSONResponse(
                {"error_code": "SERVER_MISCONFIGURED", "message": "KITCHEN_MCP_TOKEN is not set"},
                status_code=500,
            )
            await response(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        provided = auth_header[len("Bearer "):] if auth_header.startswith("Bearer ") else ""

        valid = hmac.compare_digest(provided, token) or (
            bool(provided) and _is_valid_oauth_access_token(provided)
        )

        if not valid:
            client = scope.get("client")
            source_ip = client[0] if client else "unknown"
            log_auth_event("auth failure source_ip=%s", source_ip)
            response_headers = {}
            base = config.public_url()
            if base:
                response_headers["WWW-Authenticate"] = (
                    f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
                )
            response = JSONResponse(
                {"error_code": "UNAUTHORIZED", "message": "invalid or missing bearer token"},
                status_code=401,
                headers=response_headers,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
