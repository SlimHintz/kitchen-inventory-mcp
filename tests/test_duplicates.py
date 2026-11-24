from datetime import date

from factories import make_canonical, make_pantry_item

from kitchen_inventory_mcp.models import ItemAlias, ItemHistory, PantryItem
from kitchen_inventory_mcp.schemas import (
    AddInventoryItemsInput,
    ItemInput,
    ResolveDuplicateInput,
    ResolveOrderBatchInput,
)
from kitchen_inventory_mcp.tools.inventory import (
    add_inventory_items,
    resolve_duplicate,
    resolve_order_batch,
)


def _item(raw_name, **kw):
    return ItemInput(
        raw_name=raw_name,
        quantity=kw.pop("quantity", 1),
        category=kw.pop("category", "pantry"),
        item_class=kw.pop("item_class", "countable"),
        location=kw.pop("location", "pantry"),
        source=kw.pop("source", "manual"),
        **kw,
    )


# --- Correction 3: resolve_duplicate must never hard-delete -----------------


def test_merge_soft_deletes_losing_row_and_never_orphans_history(session):
    canonical = make_canonical(session, "canned tomato", item_class="countable")
    target = make_pantry_item(session, canonical, quantity=2)
    loser = make_pantry_item(session, canonical, quantity=1, raw_name="canned tomatoe")

    result = resolve_duplicate(
        session,
        ResolveDuplicateInput(pantry_item_id=loser.id, action="merge", target_item_id=target.id, actor="alex"),
    )
    assert result["merged_into"] == target.id

    session.refresh(loser)
    session.refresh(target)
    assert target.quantity == 3
    assert loser.quantity == 0
    assert loser.removed_at is not None
    assert loser.superseded_by == target.id

    # The row still exists -- item_history's FK to it is never dangling.
    assert session.get(PantryItem, loser.id) is not None
    history = session.query(ItemHistory).filter(ItemHistory.pantry_item_id == loser.id).all()
    assert len(history) == 1
    assert history[0].quantity_delta == -1

    target_history = session.query(ItemHistory).filter(ItemHistory.pantry_item_id == target.id).all()
    assert len(target_history) == 1
    assert target_history[0].quantity_delta == 1


def test_merge_writes_alias(session):
    canonical = make_canonical(session, "canned tomato", item_class="countable")
    target = make_pantry_item(session, canonical, quantity=2)
    loser = make_pantry_item(session, canonical, quantity=1, raw_name="canned tomatoe weird spelling")

    resolve_duplicate(
        session,
        ResolveDuplicateInput(pantry_item_id=loser.id, action="merge", target_item_id=target.id, actor="alex"),
    )
    alias = (
        session.query(ItemAlias)
        .filter(ItemAlias.alias == "canned tomatoe weird spelling")
        .first()
    )
    assert alias is not None
    assert alias.canonical_item_id == canonical.id


def test_discard_soft_deletes_never_hard_deletes(session):
    canonical = make_canonical(session, "canned tomato", item_class="countable")
    row = make_pantry_item(session, canonical, quantity=1)

    result = resolve_duplicate(
        session, ResolveDuplicateInput(pantry_item_id=row.id, action="discard", actor="alex")
    )
    assert result["action"] == "discard"

    session.refresh(row)
    assert session.get(PantryItem, row.id) is not None  # still exists
    assert row.quantity == 0
    assert row.removed_at is not None
    history = session.query(ItemHistory).filter(ItemHistory.pantry_item_id == row.id).all()
    assert len(history) == 1
    assert history[0].change_type == "removed"


def test_keep_separate_clears_flag_without_touching_data(session):
    canonical = make_canonical(session, "chicken broth", item_class="countable")
    row = make_pantry_item(session, canonical, quantity=1)
    row.review_status = "possible_duplicate"
    row.review_reason = "alias_confidence"
    session.commit()

    resolve_duplicate(
        session, ResolveDuplicateInput(pantry_item_id=row.id, action="keep_separate", actor="alex")
    )
    session.refresh(row)
    assert row.review_status == "ok"
    assert row.review_reason is None
    assert row.quantity == 1
    assert row.removed_at is None


# --- Correction 2: batch resolution for order replay ------------------------


def test_order_replay_flags_whole_batch_and_batch_discard_removes_only_that_batch(session):
    make_canonical(session, "canned tomato", item_class="countable")
    make_canonical(session, "onion", item_class="countable")

    original = add_inventory_items(
        session,
        AddInventoryItemsInput(
            items=[_item("canned tomato", quantity=3), _item("onion", quantity=2)],
            actor="alex",
            order_dedup_key="kroger-order-12345",
        ),
    )
    assert original["summary"]["order_replay_detected"] is False
    original_ids = {r["pantry_item_id"] for r in original["results"]}

    replay = add_inventory_items(
        session,
        AddInventoryItemsInput(
            items=[_item("canned tomato", quantity=3), _item("onion", quantity=2)],
            actor="alex",
            order_dedup_key="kroger-order-12345",
        ),
    )
    assert replay["summary"]["order_replay_detected"] is True
    for r in replay["results"]:
        assert r["review_status"] == "possible_duplicate"
        assert r["review_reason"] == "order_replay"
    replay_batch_id = replay["results"][0]["order_batch_id"]
    assert replay_batch_id is not None

    # One resolve_order_batch call clears the whole replay -- not 2 (or 30)
    # individual resolve_duplicate calls.
    batch_result = resolve_order_batch(
        session, ResolveOrderBatchInput(order_batch_id=replay_batch_id, action="discard", actor="alex")
    )
    assert set(batch_result["affected_rows"]) == {r["pantry_item_id"] for r in replay["results"]}

    for pid in original_ids:
        row = session.get(PantryItem, pid)
        assert row.removed_at is None  # original submission untouched

    for r in replay["results"]:
        row = session.get(PantryItem, r["pantry_item_id"])
        assert row.removed_at is not None
        assert row.quantity == 0


def test_order_replay_keep_separate_clears_flags_on_whole_batch(session):
    make_canonical(session, "canned tomato", item_class="countable")

    add_inventory_items(
        session,
        AddInventoryItemsInput(
            items=[_item("canned tomato", quantity=1)], actor="alex", order_dedup_key="order-9"
        ),
    )
    replay = add_inventory_items(
        session,
        AddInventoryItemsInput(
            items=[_item("canned tomato", quantity=1)], actor="alex", order_dedup_key="order-9"
        ),
    )
    batch_id = replay["results"][0]["order_batch_id"]

    resolve_order_batch(
        session, ResolveOrderBatchInput(order_batch_id=batch_id, action="keep_separate", actor="alex")
    )
    for r in replay["results"]:
        row = session.get(PantryItem, r["pantry_item_id"])
        assert row.review_status == "ok"
        assert row.review_reason is None
        assert row.removed_at is None
        assert row.quantity == 1  # untouched, both purchases stand


# --- Within-batch collapsing (client-collapses, server backstops) ----------


def test_within_batch_identical_lines_are_silently_summed(session):
    make_canonical(session, "canned tomato", item_class="countable")

    result = add_inventory_items(
        session,
        AddInventoryItemsInput(
            items=[_item("canned tomato", quantity=1), _item("canned tomato", quantity=1)],
            actor="alex",
        ),
    )
    assert len(result["results"]) == 1
    assert result["results"][0]["collapsed_duplicate_lines"] == 1
    row = session.get(PantryItem, result["results"][0]["pantry_item_id"])
    assert row.quantity == 2


def test_within_batch_lines_differing_by_expiration_stay_separate_unflagged(session):
    make_canonical(session, "canned tomato", item_class="countable")

    result = add_inventory_items(
        session,
        AddInventoryItemsInput(
            items=[
                _item("canned tomato", quantity=1, expiration_date=date(2026, 9, 1)),
                _item("canned tomato", quantity=1, expiration_date=date(2026, 12, 1)),
            ],
            actor="alex",
        ),
    )
    assert len(result["results"]) == 2
    for r in result["results"]:
        assert r["review_status"] == "ok"


# --- possible_stock_duplicate detection -------------------------------------


def test_possible_stock_duplicate_flagged_when_same_item_added_twice_quickly(session):
    make_canonical(session, "canned tomato", item_class="countable")

    first = add_inventory_items(
        session, AddInventoryItemsInput(items=[_item("canned tomato", quantity=2)], actor="alex")
    )
    assert first["results"][0]["review_status"] == "ok"

    second = add_inventory_items(
        session,
        AddInventoryItemsInput(items=[_item("canned tomato", quantity=1)], actor="alex"),
    )
    assert second["results"][0]["review_status"] == "possible_duplicate"
    assert second["results"][0]["review_reason"] == "possible_stock_duplicate"

    row = session.get(PantryItem, second["results"][0]["pantry_item_id"])
    assert row.possible_duplicate_of == first["results"][0]["pantry_item_id"]
