#!/usr/bin/env python3
"""Idempotent seed script for canonical_items/item_aliases.

Safe to re-run: existing canonical items are left alone (aside from
picking up any new aliases added to seed_data.json since the last run),
and nothing is ever overwritten. Run once after the first
`alembic upgrade head`, and again any time seed_data.json gains entries.

Usage: uv run python scripts/seed_canonical_items.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from kitchen_inventory_mcp.db import new_session
from kitchen_inventory_mcp.models import CanonicalItem, ItemAlias


def main() -> None:
    fixture_path = Path(__file__).parent / "seed_data.json"
    entries = json.loads(fixture_path.read_text())

    session = new_session()
    created_items = 0
    created_aliases = 0
    try:
        for entry in entries:
            canonical = session.execute(
                select(CanonicalItem).where(CanonicalItem.canonical_name == entry["canonical_name"])
            ).scalar_one_or_none()

            if canonical is None:
                canonical = CanonicalItem(
                    canonical_name=entry["canonical_name"],
                    display_name=entry["display_name"],
                    category=entry["category"],
                    item_class=entry["item_class"],
                    default_unit=entry.get("default_unit"),
                    default_container_size=entry.get("default_container_size"),
                    default_location=entry.get("default_location"),
                )
                session.add(canonical)
                session.flush()
                created_items += 1

            for alias in entry.get("aliases", []):
                exists = session.execute(
                    select(ItemAlias.id).where(ItemAlias.alias == alias)
                ).scalar_one_or_none()
                if exists is None:
                    session.add(ItemAlias(canonical_item_id=canonical.id, alias=alias))
                    created_aliases += 1

        session.commit()
    finally:
        session.close()

    print(f"Seed complete: {created_items} new canonical items, {created_aliases} new aliases.")


if __name__ == "__main__":
    main()
