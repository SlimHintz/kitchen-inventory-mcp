"""Regression tests for the containment-scoring bug: token_set_ratio scored
any raw_name that CONTAINS a canonical token as a perfect match regardless of
surrounding modifier text ("garlic salt" -> "garlic" at 100), silently
corrupting resolution for anything with a compound/prepared form. Fixed by
switching to token_sort_ratio (penalizes unmatched tokens instead of
ignoring them), an explicit disqualifying-modifier list, and a stricter
auto-accept threshold for short canonical names.
"""

from factories import make_canonical
from test_matching import _add_one


def test_garlic_salt_does_not_resolve_to_garlic(session):
    make_canonical(session, "garlic", item_class="countable", category="produce")

    result = _add_one(
        session, "Spice Classics Garlic Salt", category="spice", item_class="staple",
        quantity=None, state="full",
    )
    item = result["results"][0]
    assert item["matched_canonical_item"] != "garlic"
    assert item["match_type"] == "new"
    assert item["review_status"] == "needs_review"
    assert item["review_reason"] == "new_canonical_item"


def test_celery_salt_does_not_resolve_to_celery(session):
    make_canonical(session, "celery", item_class="countable", category="produce")

    result = _add_one(
        session, "Celery Salt", category="spice", item_class="staple", quantity=None, state="full"
    )
    item = result["results"][0]
    assert item["matched_canonical_item"] != "celery"
    assert item["match_type"] == "new"


def test_celery_seed_does_not_resolve_to_celery(session):
    make_canonical(session, "celery", item_class="countable", category="produce")

    result = _add_one(
        session, "Spice Classics Celery Seed", category="spice", item_class="staple",
        quantity=None, state="full",
    )
    item = result["results"][0]
    assert item["matched_canonical_item"] != "celery"
    assert item["match_type"] == "new"


def test_onion_powder_resolves_to_its_own_canonical_not_onion(session):
    make_canonical(session, "onion", item_class="countable", category="produce")
    make_canonical(session, "onion powder", item_class="staple", category="spice")

    result = _add_one(
        session, "Onion Powder", category="spice", item_class="staple", quantity=None, state="full"
    )
    item = result["results"][0]
    assert item["matched_canonical_item"] == "onion powder"
    assert item["match_type"] == "exact"
    assert item["review_status"] == "ok"


def test_garlic_powder_resolves_to_its_own_canonical_not_garlic(session):
    make_canonical(session, "garlic", item_class="countable", category="produce")
    make_canonical(session, "garlic powder", item_class="staple", category="spice")

    result = _add_one(
        session, "Garlic Powder", category="spice", item_class="staple", quantity=None, state="full"
    )
    item = result["results"][0]
    assert item["matched_canonical_item"] == "garlic powder"
    assert item["match_type"] == "exact"
    assert item["review_status"] == "ok"


def test_nutmeg_never_matches_mustard(session):
    make_canonical(session, "mustard", item_class="staple", category="condiment")

    result = _add_one(
        session, "McCormick Ground Nutmeg", category="spice", item_class="staple",
        quantity=None, state="full",
    )
    item = result["results"][0]
    assert item["matched_canonical_item"] != "mustard"
    assert item["match_type"] == "new"


def test_bay_leaves_never_matches_oregano(session):
    make_canonical(session, "oregano", item_class="staple", category="spice")

    result = _add_one(
        session, "McCormick Bay Leaves", category="spice", item_class="staple",
        quantity=None, state="full",
    )
    item = result["results"][0]
    assert item["matched_canonical_item"] != "oregano"


def test_exact_name_ingestion_still_resolves_at_100_ok(session):
    make_canonical(session, "black pepper", item_class="staple", category="spice")

    result = _add_one(
        session, "Black Pepper", category="spice", item_class="staple", quantity=None, state="full"
    )
    item = result["results"][0]
    assert item["match_type"] == "exact"
    assert item["score"] == 100
    assert item["review_status"] == "ok"
    assert item["matched_canonical_item"] == "black pepper"
