from datetime import date

from sqlalchemy.orm import Session

from kitchen_inventory_mcp.models import CanonicalItem, PantryItem


def make_canonical(
    session: Session,
    canonical_name: str,
    item_class: str = "countable",
    category: str = "pantry",
    display_name: str | None = None,
) -> CanonicalItem:
    ci = CanonicalItem(
        canonical_name=canonical_name,
        display_name=display_name or canonical_name.title(),
        category=category,
        item_class=item_class,
    )
    session.add(ci)
    session.flush()
    return ci


def make_pantry_item(
    session: Session,
    canonical: CanonicalItem,
    quantity: int | None = None,
    state: str | None = None,
    location: str = "pantry",
    expiration_date: date | None = None,
    raw_name: str | None = None,
    source: str = "manual",
) -> PantryItem:
    pi = PantryItem(
        canonical_item_id=canonical.id,
        raw_name=raw_name or canonical.display_name,
        quantity=quantity,
        state=state,
        location=location,
        expiration_date=expiration_date,
        source=source,
        review_status="ok",
    )
    session.add(pi)
    session.flush()
    return pi
