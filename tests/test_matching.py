from factories import make_canonical

from kitchen_inventory_mcp import matching
from kitchen_inventory_mcp.models import ItemAlias
from kitchen_inventory_mcp.schemas import AddInventoryItemsInput, ItemInput
from kitchen_inventory_mcp.tools.inventory import add_inventory_items


def _add_one(session, raw_name, **kw):
    payload = AddInventoryItemsInput(
        items=[
            ItemInput(
                raw_name=raw_name,
                quantity=kw.pop("quantity", 1),
                category=kw.pop("category", "pantry"),
                item_class=kw.pop("item_class", "countable"),
                location=kw.pop("location", "pantry"),
                source=kw.pop("source", "manual"),
                **kw,
            )
        ],
        actor="alex",
    )
    return add_inventory_items(session, payload)


def test_exact_alias_hit_resolves(session):
    canned = make_canonical(session, "canned_tomatoes", item_class="countable")
    # Aliases are always stored normalized -- mirror that here.
    session.add(
        ItemAlias(
            canonical_item_id=canned.id, alias=matching.normalize_name("diced tomatoes canned")
        )
    )
    session.commit()

    result = _add_one(session, "Diced Tomatoes Canned")
    item = result["results"][0]
    assert item["match_type"] == "exact"
    assert item["review_status"] == "ok"
    assert item["matched_canonical_item"] == "canned_tomatoes"


def test_fuzzy_high_score_auto_resolves_and_writes_alias(session):
    make_canonical(session, "canned tomato", item_class="countable")

    # token_sort_ratio("canned tomato", "canned tomatoe") ~= 96 -- a genuine
    # typo, not containment (a bare extra letter, not an unrelated modifier
    # word tacked on), and not a perfect 100 either.
    result = _add_one(session, "canned tomatoe")
    item = result["results"][0]
    assert item["match_type"] == "fuzzy"
    assert 90 <= item["score"] < 100
    assert item["review_status"] == "ok"
    assert item["matched_canonical_item"] == "canned tomato"

    alias = session.query(ItemAlias).filter(ItemAlias.alias == "canned tomatoe").first()
    assert alias is not None


def test_perfect_fuzzy_score_still_flagged_needs_review(session):
    make_canonical(session, "canned tomato", item_class="countable")

    # token_sort_ratio normalizes word order, so a phrasing that's an exact
    # match once reordered still comes back as a "fuzzy" match_type (it
    # never matched the raw pre-sort string), scoring a perfect 100. A
    # perfect score via the fuzzy path is a signal, not a guarantee -- it
    # must still be flagged rather than silently trusted like a true exact
    # match would be.
    result = _add_one(session, "tomato canned")
    item = result["results"][0]
    assert item["match_type"] == "fuzzy"
    assert item["score"] == 100
    assert item["review_status"] == "needs_review"
    assert item["review_reason"] == "perfect_fuzzy_score"


def test_different_items_do_not_auto_merge(session):
    make_canonical(session, "chicken broth", item_class="countable", category="pantry")

    # token_sort_ratio("chicken broth", "chicken breast") ~= 81 -- ambiguous
    # band. It must NOT be silently written as an alias of chicken broth.
    result = _add_one(session, "chicken breast", category="protein")
    item = result["results"][0]
    assert item["review_status"] == "possible_duplicate"
    assert item["review_reason"] == "alias_confidence"
    assert item["matched_canonical_item"] == "chicken broth"  # tentative match only

    alias = session.query(ItemAlias).filter(ItemAlias.alias == "chicken breast").first()
    assert alias is None  # never silently merged/aliased


def test_possible_duplicate_band_75_to_89(session):
    make_canonical(session, "chicken bouillon", item_class="staple", category="other")

    # token_sort_ratio("chicken bouillon", "bouillon cube") ~= 76
    result = _add_one(
        session, "bouillon cube", item_class="staple", category="other", quantity=None, state="full"
    )
    item = result["results"][0]
    assert 75 <= item["score"] < 90
    assert item["review_status"] == "possible_duplicate"
    assert item["review_reason"] == "alias_confidence"


def test_score_below_75_creates_new_canonical_item(session):
    make_canonical(session, "canned tomato", item_class="countable")

    # token_sort_ratio("canned tomato", "crushed tomato") ~= 74
    result = _add_one(session, "crushed tomato")
    item = result["results"][0]
    assert item["score"] < 75
    assert item["match_type"] == "new"
    assert item["review_status"] == "needs_review"
    assert item["review_reason"] == "new_canonical_item"
    assert item["matched_canonical_item"] == "crushed tomato"


def test_low_confidence_forces_needs_review_regardless_of_match_quality(session):
    make_canonical(session, "canned tomato", item_class="countable")
    result = _add_one(session, "canned tomato", confidence=0.4)
    item = result["results"][0]
    assert item["match_type"] == "exact"
    assert item["review_status"] == "needs_review"
    assert item["review_reason"] == "low_confidence"
