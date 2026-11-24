import uvicorn

from . import config
from .server import build_app


def main() -> None:
    if not config.auth_token():
        raise SystemExit("KITCHEN_MCP_TOKEN must be set before starting the server")
    uvicorn.run(build_app(), host=config.host(), port=config.port())


if __name__ == "__main__":
    main()
