from datetime import date, timedelta

from factories import make_canonical, make_pantry_item

from kitchen_inventory_mcp.models import HouseholdPreference
from kitchen_inventory_mcp.schemas import (
    GetInventoryInput,
    RemoveInventoryItemInput,
    RemovePreferenceInput,
    SetPreferenceInput,
    SweepExpiredInput,
    UpdateInventoryItemFields,
    UpdateInventoryItemInput,
)
from kitchen_inventory_mcp.tools.inventory import (
    get_inventory,
    remove_inventory_item,
    sweep_expired,
    update_inventory_item,
)
from kitchen_inventory_mcp.tools.preferences import remove_preference, set_preference


def test_get_inventory_excludes_out_of_stock_by_default(session):
    canonical = make_canonical(session, "onion", item_class="countable")
    make_pantry_item(session, canonical, quantity=2)
    make_pantry_item(session, canonical, quantity=0)

    result = get_inventory(session, GetInventoryInput())
    assert result["total_items"] == 1

    result_with_out = get_inventory(session, GetInventoryInput(include_out=True))
    assert result_with_out["total_items"] == 2


def test_get_inventory_expiring_soon(session):
    canonical = make_canonical(session, "onion", item_class="countable")
    make_pantry_item(session, canonical, quantity=1, expiration_date=date.today() + timedelta(days=3))
    make_pantry_item(session, canonical, quantity=1, expiration_date=date.today() + timedelta(days=30))

    result = get_inventory(session, GetInventoryInput(expiring_within_days=7))
    assert len(result["expiring_soon"]) == 1


def test_update_inventory_item_writes_history(session):
    canonical = make_canonical(session, "onion", item_class="countable")
    row = make_pantry_item(session, canonical, quantity=2)

    update_inventory_item(
        session,
        UpdateInventoryItemInput(id=row.id, fields=UpdateInventoryItemFields(quantity=5), actor="alex"),
    )
    session.refresh(row)
    assert row.quantity == 5


def test_remove_inventory_item_soft_deletes(session):
    canonical = make_canonical(session, "onion", item_class="countable")
    row = make_pantry_item(session, canonical, quantity=2)

    remove_inventory_item(session, RemoveInventoryItemInput(id=row.id, reason="used_up", actor="alex"))
    session.refresh(row)
    assert row.quantity == 0
    assert row.removed_at is not None


def test_sweep_expired_dry_run_does_not_mutate(session):
    canonical = make_canonical(session, "onion", item_class="countable")
    row = make_pantry_item(session, canonical, quantity=2, expiration_date=date.today() - timedelta(days=1))

    result = sweep_expired(session, SweepExpiredInput(dry_run=True, actor="alex"))
    assert len(result["expired_items"]) == 1
    session.refresh(row)
    assert row.quantity == 2  # untouched

    result = sweep_expired(session, SweepExpiredInput(dry_run=False, actor="alex"))
    session.refresh(row)
    assert row.quantity == 0
    assert row.removed_at is not None


def test_set_preference_upserts_on_identity(session):
    set_preference(
        session,
        SetPreferenceInput(kind="dislike", subject="mushrooms", applies_to="kid", actor="alex"),
    )
    set_preference(
        session,
        SetPreferenceInput(
            kind="dislike", subject="mushrooms", applies_to="kid", notes="updated", actor="sam"
        ),
    )
    rows = session.query(HouseholdPreference).all()
    assert len(rows) == 1
    assert rows[0].notes == "updated"


def test_remove_preference_hard_deletes(session):
    result = set_preference(
        session, SetPreferenceInput(kind="favorite", subject="tacos", applies_to="household", actor="alex")
    )
    remove_preference(session, RemovePreferenceInput(id=result["id"], actor="alex"))
    assert session.query(HouseholdPreference).count() == 0
