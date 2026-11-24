"""Environment-driven configuration. No secrets have defaults."""

import os
from pathlib import Path

DEFAULT_DB_PATH = str(Path.home() / "kitchen-inventory-data" / "kitchen.db")


def db_path() -> str:
    return os.environ.get("KITCHEN_MCP_DB_PATH", DEFAULT_DB_PATH)


def db_url() -> str:
    return f"sqlite:///{db_path()}"


def auth_token() -> str | None:
    return os.environ.get("KITCHEN_MCP_TOKEN")


def log_path() -> str:
    return os.environ.get("KITCHEN_MCP_LOG_PATH", str(Path.home() / "kitchen-inventory-data" / "auth.log"))


def host() -> str:
    return os.environ.get("KITCHEN_MCP_HOST", "127.0.0.1")


def port() -> int:
    return int(os.environ.get("KITCHEN_MCP_PORT", "8811"))


def planning_recipe_limit() -> int:
    return int(os.environ.get("KITCHEN_MCP_PLANNING_RECIPE_LIMIT", "40"))


def public_url() -> str | None:
    """The server's own public HTTPS URL (e.g. https://kitchen.example.com),
    used to build OAuth discovery metadata. The server sits behind a
    Cloudflare Tunnel terminating TLS, so it can't reliably infer this from
    the incoming request -- must be set explicitly for OAuth to work."""
    url = os.environ.get("KITCHEN_MCP_PUBLIC_URL")
    return url.rstrip("/") if url else None
