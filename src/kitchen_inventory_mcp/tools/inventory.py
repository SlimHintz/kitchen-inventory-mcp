from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import matching
from ..enums import ChangeType, ReviewReason, ReviewStatus
from ..errors import KitchenError, NotFoundError
from ..models import CanonicalItem, ItemHistory, PantryItem, new_batch_id, utcnow
from ..schemas import (
    AddInventoryItemsInput,
    GetInventoryInput,
    GetItemHistoryInput,
    ItemInput,
    RemoveInventoryItemInput,
    ResolveDuplicateInput,
    ResolveOrderBatchInput,
    SweepExpiredInput,
    UpdateInventoryItemInput,
)

REPLAY_WINDOW_DAYS = 30
STOCK_DUPLICATE_WINDOW_MINUTES = 15


def _possible_stock_duplicate(
    session: Session,
    canonical_item_id: int,
    location: str,
    quantity: int | None,
    expiration_date,
    container_size: str | None,
) -> PantryItem | None:
    """Physical-stock duplicate detection: same canonical item, same
    location, same expiration/container metadata, added very recently.
    Only meaningful for countables -- and only run when canonical
    resolution was already confident, since stacking a stock-duplicate
    concern on an already-uncertain name match would just be confusing.

    Requiring expiration_date/container_size to also match keeps this from
    firing on two rows that are legitimately distinct stock (e.g. two
    cans with different best-by dates) -- a difference in either field is
    itself the signal that they're intentionally separate, not a duplicate.
    """
    if quantity is None:
        return None
    cutoff = utcnow() - timedelta(minutes=STOCK_DUPLICATE_WINDOW_MINUTES)
    return session.execute(
        select(PantryItem)
        .where(
            PantryItem.canonical_item_id == canonical_item_id,
            PantryItem.location == location,
            PantryItem.expiration_date.is_(expiration_date) if expiration_date is None else PantryItem.expiration_date == expiration_date,
            PantryItem.container_size.is_(container_size) if container_size is None else PantryItem.container_size == container_size,
            PantryItem.removed_at.is_(None),
            PantryItem.added_at >= cutoff,
        )
        .order_by(PantryItem.added_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _identity_key(item: ItemInput) -> tuple:
    return (
        matching.normalize_name(item.raw_name),
        item.quantity,
        item.state,
        item.unit,
        item.container_size,
        item.location,
        item.expiration_date,
    )


def _collapse_identical(items: list[ItemInput]) -> list[tuple[ItemInput, int]]:
    """Correction: within-batch duplicate lines that are byte-identical
    across every field that matters get silently summed by the server as a
    deterministic backstop. Judgment-based collapsing (same product,
    differently phrased) is the calling client's job, done before this is
    ever called."""
    groups: dict[tuple, list[ItemInput]] = {}
    order: list[tuple] = []
    for item in items:
        key = _identity_key(item)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)

    collapsed = []
    for key in order:
        group = groups[key]
        rep = group[0]
        count = len(group)
        if count > 1 and rep.quantity is not None:
            rep = rep.model_copy(update={"quantity": sum(i.quantity or 0 for i in group)})
        collapsed.append((rep, count))
    return collapsed


def _is_order_replay(session: Session, order_dedup_key: str) -> bool:
    cutoff = utcnow() - timedelta(days=REPLAY_WINDOW_DAYS)
    existing = session.execute(
        select(PantryItem.id)
        .where(PantryItem.order_dedup_key == order_dedup_key, PantryItem.added_at >= cutoff)
        .limit(1)
    ).scalar_one_or_none()
    return existing is not None


def add_inventory_items(session: Session, payload: AddInventoryItemsInput) -> dict:
    order_batch_id = new_batch_id() if payload.order_dedup_key else None
    is_replay = payload.order_dedup_key is not None and _is_order_replay(
        session, payload.order_dedup_key
    )

    results = []
    added_count = 0
    needs_review_count = 0
    possible_duplicate_count = 0

    for item, collapsed_count in _collapse_identical(payload.items):
        resolution = matching.resolve_and_maybe_create(
            session,
            item.raw_name,
            category=item.category,
            item_class=item.item_class,
            default_unit=item.unit,
            default_container_size=item.container_size,
            default_location=item.location,
            confidence=item.confidence,
        )

        review_status = resolution.review_status
        review_reason = resolution.review_reason
        possible_duplicate_of = None

        if review_status == ReviewStatus.ok.value and resolution.canonical_item_id is not None:
            dup = _possible_stock_duplicate(
                session,
                resolution.canonical_item_id,
                item.location,
                item.quantity,
                item.expiration_date,
                item.container_size,
            )
            if dup is not None:
                review_status = ReviewStatus.possible_duplicate.value
                review_reason = ReviewReason.possible_stock_duplicate.value
                possible_duplicate_of = dup.id

        if is_replay:
            review_status = ReviewStatus.possible_duplicate.value
            review_reason = ReviewReason.order_replay.value
            possible_duplicate_of = None  # batch-level flag; resolved via resolve_order_batch

        row = PantryItem(
            canonical_item_id=resolution.canonical_item_id,
            raw_name=item.raw_name,
            quantity=item.quantity,
            state=item.state,
            container_size=item.container_size,
            unit=item.unit,
            location=item.location,
            expiration_date=item.expiration_date,
            source=item.source,
            confidence=item.confidence,
            review_status=review_status,
            review_reason=review_reason,
            possible_duplicate_of=possible_duplicate_of,
            order_dedup_key=payload.order_dedup_key,
            order_batch_id=order_batch_id,
        )
        session.add(row)
        session.flush()

        session.add(
            ItemHistory(
                pantry_item_id=row.id,
                change_type=ChangeType.added.value,
                quantity_delta=item.quantity,
                state_from=None,
                state_to=item.state,
                actor=payload.actor,
            )
        )

        canonical = session.get(CanonicalItem, resolution.canonical_item_id)
        added_count += 1
        if review_status == ReviewStatus.needs_review.value:
            needs_review_count += 1
        elif review_status == ReviewStatus.possible_duplicate.value:
            possible_duplicate_count += 1

        results.append(
            {
                "raw_name": item.raw_name,
                "pantry_item_id": row.id,
                "matched_canonical_item": canonical.canonical_name if canonical else None,
                "match_type": resolution.match_type,
                "score": resolution.score,
                "review_status": review_status,
                "review_reason": review_reason,
                "order_batch_id": order_batch_id,
                "collapsed_duplicate_lines": collapsed_count - 1,
            }
        )

    session.commit()
    return {
        "results": results,
        "summary": {
            "added": added_count,
            "needs_review": needs_review_count,
            "possible_duplicates": possible_duplicate_count,
            "order_replay_detected": is_replay,
        },
    }


def _pantry_item_or_error(session: Session, pantry_item_id: int) -> PantryItem:
    row = session.get(PantryItem, pantry_item_id)
    if row is None:
        raise NotFoundError("pantry_item", pantry_item_id)
    return row


def _soft_delete(row: PantryItem, superseded_by: int | None) -> tuple[int | None, str | None, str | None]:
    """Returns (quantity_delta, state_from, state_to) for the history row."""
    if row.quantity is not None:
        delta = -row.quantity
        row.quantity = 0
        state_from = state_to = None
    else:
        state_from, delta = row.state, None
        state_to = "out"
        row.state = state_to
    row.removed_at = utcnow()
    row.superseded_by = superseded_by
    row.review_status = ReviewStatus.ok.value
    row.review_reason = None
    return delta, state_from, state_to


def resolve_duplicate(session: Session, payload: ResolveDuplicateInput) -> dict:
    row = _pantry_item_or_error(session, payload.pantry_item_id)

    if payload.action == "keep_separate":
        row.review_status = ReviewStatus.ok.value
        row.review_reason = None
        session.commit()
        return {"pantry_item_id": row.id, "action": payload.action, "alias_written": False}

    if payload.action == "merge":
        target_id = payload.target_item_id or row.possible_duplicate_of
        if target_id is None:
            raise KitchenError(
                "MERGE_TARGET_REQUIRED",
                "merge requires target_item_id (no possible_duplicate_of is set on this row)",
            )
        target = _pantry_item_or_error(session, target_id)

        original_qty = row.quantity
        delta, state_from, state_to = _soft_delete(row, superseded_by=target.id)
        session.add(
            ItemHistory(
                pantry_item_id=row.id,
                change_type=ChangeType.manual_adjust.value,
                quantity_delta=delta,
                state_from=state_from,
                state_to=state_to,
                actor=payload.actor,
            )
        )

        # merge is honored even across different canonical items if the caller explicitly
        # names a target_item_id -- the alias below is what pulls them into agreement.
        alias_written = False
        if original_qty is not None and target.quantity is not None:
            target.quantity += original_qty
            session.add(
                ItemHistory(
                    pantry_item_id=target.id,
                    change_type=ChangeType.manual_adjust.value,
                    quantity_delta=original_qty,
                    actor=payload.actor,
                )
            )

        normalized = matching.normalize_name(row.raw_name)
        if target.canonical_item_id is not None:
            existing_alias = session.execute(
                select(CanonicalItem.id).where(CanonicalItem.canonical_name == normalized)
            ).scalar_one_or_none()
            from ..models import ItemAlias

            alias_exists = session.execute(
                select(ItemAlias.id).where(ItemAlias.alias == normalized)
            ).scalar_one_or_none()
            if not existing_alias and not alias_exists:
                session.add(ItemAlias(canonical_item_id=target.canonical_item_id, alias=normalized))
                alias_written = True

        session.commit()
        return {
            "pantry_item_id": row.id,
            "action": payload.action,
            "merged_into": target.id,
            "alias_written": alias_written,
        }

    if payload.action == "discard":
        original_qty = row.quantity
        delta, state_from, state_to = _soft_delete(row, superseded_by=None)
        session.add(
            ItemHistory(
                pantry_item_id=row.id,
                change_type=ChangeType.removed.value,
                quantity_delta=delta,
                state_from=state_from,
                state_to=state_to,
                actor=payload.actor,
            )
        )
        session.commit()
        return {"pantry_item_id": row.id, "action": payload.action, "alias_written": False}

    raise KitchenError("INVALID_ACTION", f"unknown action {payload.action}")


def resolve_order_batch(session: Session, payload: ResolveOrderBatchInput) -> dict:
    rows = list(
        session.execute(
            select(PantryItem).where(PantryItem.order_batch_id == payload.order_batch_id)
        ).scalars()
    )
    if not rows:
        raise NotFoundError("order_batch", payload.order_batch_id)

    affected = []
    for row in rows:
        if row.removed_at is not None:
            continue
        if payload.action == "discard":
            delta, state_from, state_to = _soft_delete(row, superseded_by=None)
            session.add(
                ItemHistory(
                    pantry_item_id=row.id,
                    change_type=ChangeType.removed.value,
                    quantity_delta=delta,
                    state_from=state_from,
                    state_to=state_to,
                    actor=payload.actor,
                )
            )
        else:  # keep_separate
            row.review_status = ReviewStatus.ok.value
            row.review_reason = None
        affected.append(row.id)

    session.commit()
    return {"order_batch_id": payload.order_batch_id, "action": payload.action, "affected_rows": affected}


def get_inventory(session: Session, payload: GetInventoryInput) -> dict:
    query = select(PantryItem).where(PantryItem.removed_at.is_(None))
    if payload.location:
        query = query.where(PantryItem.location == payload.location)
    if payload.review_status:
        query = query.where(PantryItem.review_status == payload.review_status)
    if payload.category or payload.item_class:
        query = query.join(CanonicalItem, PantryItem.canonical_item_id == CanonicalItem.id)
        if payload.category:
            query = query.where(CanonicalItem.category == payload.category)
        if payload.item_class:
            query = query.where(CanonicalItem.item_class == payload.item_class)

    rows = list(session.execute(query).scalars())

    if not payload.include_out:
        rows = [r for r in rows if not (r.quantity == 0 or r.state == "out")]

    def serialize(row: PantryItem) -> dict:
        return {
            "id": row.id,
            "canonical_item_id": row.canonical_item_id,
            "raw_name": row.raw_name,
            "quantity": row.quantity,
            "state": row.state,
            "container_size": row.container_size,
            "unit": row.unit,
            "location": row.location,
            "expiration_date": row.expiration_date.isoformat() if row.expiration_date else None,
            "review_status": row.review_status,
            "review_reason": row.review_reason,
            "source": row.source,
        }

    by_location: dict[str, list[dict]] = {"pantry": [], "fridge": [], "freezer": []}
    for row in rows:
        by_location.setdefault(row.location, []).append(serialize(row))

    cutoff = date.today() + timedelta(days=payload.expiring_within_days)
    expiring_soon = [
        serialize(r)
        for r in rows
        if r.expiration_date is not None and date.today() <= r.expiration_date <= cutoff
    ]

    return {"by_location": by_location, "expiring_soon": expiring_soon, "total_items": len(rows)}


def update_inventory_item(session: Session, payload: UpdateInventoryItemInput) -> dict:
    row = _pantry_item_or_error(session, payload.id)
    fields = payload.fields.model_dump(exclude_unset=True)

    quantity_delta = None
    state_from = state_to = None

    if "quantity" in fields and fields["quantity"] != row.quantity:
        quantity_delta = fields["quantity"] - (row.quantity or 0)
        row.quantity = fields["quantity"]
    if "state" in fields and fields["state"] != row.state:
        state_from, state_to = row.state, fields["state"]
        row.state = fields["state"]
    for key in ("location", "expiration_date", "raw_name", "container_size", "unit"):
        if key in fields:
            setattr(row, key, fields[key])
    if "category" in fields and row.canonical_item_id:
        canonical = session.get(CanonicalItem, row.canonical_item_id)
        if canonical:
            canonical.category = fields["category"]

    if quantity_delta is not None or state_to is not None:
        session.add(
            ItemHistory(
                pantry_item_id=row.id,
                change_type=ChangeType.manual_adjust.value,
                quantity_delta=quantity_delta,
                state_from=state_from,
                state_to=state_to,
                actor=payload.actor,
            )
        )
    session.commit()
    return {"id": row.id, "quantity": row.quantity, "state": row.state, "location": row.location}


def remove_inventory_item(session: Session, payload: RemoveInventoryItemInput) -> dict:
    row = _pantry_item_or_error(session, payload.id)
    delta, state_from, state_to = _soft_delete(row, superseded_by=None)
    change_type = ChangeType.expired.value if payload.reason == "expired" else ChangeType.removed.value
    session.add(
        ItemHistory(
            pantry_item_id=row.id,
            change_type=change_type,
            quantity_delta=delta,
            state_from=state_from,
            state_to=state_to,
            actor=payload.actor,
        )
    )
    session.commit()
    return {"id": row.id, "reason": payload.reason, "quantity": row.quantity, "state": row.state}


def get_item_history(session: Session, payload: GetItemHistoryInput) -> dict:
    query = select(ItemHistory).order_by(ItemHistory.timestamp.desc())
    if payload.item_id is not None:
        query = query.where(ItemHistory.pantry_item_id == payload.item_id)
    if payload.since is not None:
        query = query.where(ItemHistory.timestamp >= payload.since)
    if payload.limit:
        query = query.limit(payload.limit)

    rows = session.execute(query).scalars().all()
    return {
        "history": [
            {
                "id": h.id,
                "pantry_item_id": h.pantry_item_id,
                "change_type": h.change_type,
                "quantity_delta": h.quantity_delta,
                "state_from": h.state_from,
                "state_to": h.state_to,
                "actor": h.actor,
                "related_meal_log_id": h.related_meal_log_id,
                "timestamp": h.timestamp.isoformat(),
            }
            for h in rows
        ]
    }


def sweep_expired(session: Session, payload: SweepExpiredInput) -> dict:
    today = date.today()
    rows = list(
        session.execute(
            select(PantryItem).where(
                PantryItem.removed_at.is_(None),
                PantryItem.expiration_date.is_not(None),
                PantryItem.expiration_date < today,
            )
        ).scalars()
    )
    rows = [r for r in rows if not (r.quantity == 0 or r.state == "out")]

    expired_items = [
        {
            "id": r.id,
            "raw_name": r.raw_name,
            "expiration_date": r.expiration_date.isoformat(),
            "quantity": r.quantity,
            "state": r.state,
        }
        for r in rows
    ]

    if not payload.dry_run:
        for row in rows:
            delta, state_from, state_to = _soft_delete(row, superseded_by=None)
            session.add(
                ItemHistory(
                    pantry_item_id=row.id,
                    change_type=ChangeType.expired.value,
                    quantity_delta=delta,
                    state_from=state_from,
                    state_to=state_to,
                    actor=payload.actor,
                )
            )
        session.commit()

    return {"expired_items": expired_items, "applied": not payload.dry_run}
