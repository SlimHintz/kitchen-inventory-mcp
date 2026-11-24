"""Correction 4: tests otherwise use Base.metadata.create_all, so a drifted
Alembic migration would never be caught. This test actually runs
`alembic upgrade head` against an empty DB and diffs the result against
Base.metadata."""

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.command import upgrade
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from kitchen_inventory_mcp.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_alembic_head_matches_models(tmp_path, monkeypatch):
    db_path = tmp_path / "migration_check.db"
    # env.py reads KITCHEN_MCP_DB_PATH via kitchen_inventory_mcp.config, so
    # this is the one true way to point a real `alembic upgrade head` run
    # at a throwaway DB instead of the configured production one.
    monkeypatch.setenv("KITCHEN_MCP_DB_PATH", str(db_path))

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)

    assert diff == [], f"alembic head does not match models: {diff}"
