"""Deterministic item resolution: normalization, fuzzy matching against
canonical items/aliases, FEFO row ordering for depletion, and cross-row
aggregation for coverage scoring. No LLM calls -- this is the "server does
deterministic matching and math" half of the architecture.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from .enums import ReviewReason, ReviewStatus, StapleState
from .models import CanonicalItem, ItemAlias, PantryItem

AUTO_ALIAS_THRESHOLD = 90
POSSIBLE_DUPLICATE_THRESHOLD = 75
LOW_CONFIDENCE_THRESHOLD = 0.7

# A canonical name shorter than this is disproportionately easy to hit by
# accident on character overlap alone ("nutmeg"/"mustard") -- it needs a
# noticeably higher score to auto-accept. This only raises the ceiling for
# auto-acceptance; the possible_duplicate floor (POSSIBLE_DUPLICATE_THRESHOLD)
# is unchanged, so short-canonical near-misses still get flagged rather than
# silently becoming a new item.
SHORT_CANONICAL_LENGTH = 7
SHORT_CANONICAL_AUTO_THRESHOLD = 97

# Tokens that change what the ingredient *is*, not just how it's described.
# "celery salt" is not "celery": a modifier present on the raw item but
# absent from the candidate disqualifies that candidate outright, regardless
# of how similar the strings otherwise look. This exists because containment
# based scorers (and even full-string ones, for short base words) can still
# rate "X salt"/"X powder" as a near-perfect match for bare "X".
DISQUALIFYING_MODIFIERS = {
    "salt",
    "powder",
    "seed",
    "seasoning",
    "flake",
    "ground",
    "dried",
    "extract",
}

_STAPLE_PRIORITY = {
    StapleState.full.value: 3,
    StapleState.open.value: 2,
    StapleState.low.value: 1,
    StapleState.out.value: 0,
}


def normalize_name(raw: str) -> str:
    s = raw.strip().lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(_singularize(w) for w in s.split(" "))


def _singularize(word: str) -> str:
    if len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("oes"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


@dataclass
class CandidateMatch:
    canonical_item_id: int
    score: float
    canonical_name: str


def _disqualified(raw_tokens: set[str], candidate_tokens: set[str]) -> bool:
    raw_modifiers = raw_tokens & DISQUALIFYING_MODIFIERS
    candidate_modifiers = candidate_tokens & DISQUALIFYING_MODIFIERS
    return bool(raw_modifiers - candidate_modifiers)


def auto_threshold_for(canonical_name: str) -> float:
    if len(canonical_name) < SHORT_CANONICAL_LENGTH:
        return SHORT_CANONICAL_AUTO_THRESHOLD
    return AUTO_ALIAS_THRESHOLD


def _best_fuzzy_candidate(session: Session, normalized: str) -> CandidateMatch | None:
    """Full-string token similarity (token_sort_ratio), not containment
    (token_set_ratio would score "garlic salt" against "garlic" as a
    perfect match, since "garlic" is a subset of "garlic salt"'s tokens --
    unmatched modifier tokens must penalize the score). Candidates whose
    modifiers change the ingredient's culinary role are excluded outright,
    not merely scored lower."""
    id_to_name = dict(session.execute(select(CanonicalItem.id, CanonicalItem.canonical_name)).all())
    raw_tokens = set(normalized.split(" "))

    candidates: list[tuple[str, int]] = [(name, cid) for cid, name in id_to_name.items()]
    for canonical_id, alias in session.execute(select(ItemAlias.canonical_item_id, ItemAlias.alias)).all():
        candidates.append((alias, canonical_id))

    best: CandidateMatch | None = None
    for text, canonical_id in candidates:
        if _disqualified(raw_tokens, set(text.split(" "))):
            continue
        score = fuzz.token_sort_ratio(normalized, text)
        if best is None or score > best.score:
            best = CandidateMatch(
                canonical_item_id=canonical_id, score=score, canonical_name=id_to_name[canonical_id]
            )
    return best


def lookup_canonical_item(session: Session, raw_name: str) -> tuple[int | None, float]:
    """Read-only resolution used by score_recipe_coverage. Never writes an
    alias or creates a canonical item -- a scoring pass on a candidate
    recipe shouldn't pollute the canonical item table."""
    normalized = normalize_name(raw_name)

    exact = session.execute(
        select(CanonicalItem.id).where(CanonicalItem.canonical_name == normalized)
    ).scalar_one_or_none()
    if exact is not None:
        return exact, 100.0

    alias_hit = session.execute(
        select(ItemAlias.canonical_item_id).where(ItemAlias.alias == normalized)
    ).scalar_one_or_none()
    if alias_hit is not None:
        return alias_hit, 100.0

    best = _best_fuzzy_candidate(session, normalized)
    if best is not None and best.score >= auto_threshold_for(best.canonical_name):
        return best.canonical_item_id, best.score
    return None, (best.score if best else 0.0)


@dataclass
class ResolutionResult:
    canonical_item_id: int | None
    match_type: str  # "exact" | "fuzzy" | "new"
    score: float | None
    alias_written: bool
    review_status: str
    review_reason: str | None


def resolve_and_maybe_create(
    session: Session,
    raw_name: str,
    *,
    category: str,
    item_class: str,
    default_unit: str | None,
    default_container_size: str | None,
    default_location: str | None,
    confidence: float | None,
) -> ResolutionResult:
    """Full resolution pipeline used by add_inventory_items. Writes new
    aliases/canonical items per the score bands; never silently merges."""
    normalized = normalize_name(raw_name)

    exact_canonical = session.execute(
        select(CanonicalItem).where(CanonicalItem.canonical_name == normalized)
    ).scalar_one_or_none()
    exact_alias_id = None
    if exact_canonical is None:
        exact_alias_id = session.execute(
            select(ItemAlias.canonical_item_id).where(ItemAlias.alias == normalized)
        ).scalar_one_or_none()

    if exact_canonical is not None or exact_alias_id is not None:
        canonical_item_id = exact_canonical.id if exact_canonical else exact_alias_id
        result = ResolutionResult(
            canonical_item_id=canonical_item_id,
            match_type="exact",
            score=100.0,
            alias_written=False,
            review_status=ReviewStatus.ok.value,
            review_reason=None,
        )
        return _apply_confidence_override(result, confidence)

    best = _best_fuzzy_candidate(session, normalized)

    if best is not None and best.score >= auto_threshold_for(best.canonical_name):
        session.add(ItemAlias(canonical_item_id=best.canonical_item_id, alias=normalized))
        session.flush()
        # A perfect score by the fuzzy path (as opposed to the exact-match
        # branch above) is still a signal, not a guarantee -- word-order-only
        # differences and other coincidences can produce 100 here even when
        # the fuzzy path is the one that actually ran. Flag it rather than
        # silently trusting it the way a true exact match is trusted.
        if best.score >= 100.0:
            review_status = ReviewStatus.needs_review.value
            review_reason = ReviewReason.perfect_fuzzy_score.value
        else:
            review_status = ReviewStatus.ok.value
            review_reason = None
        result = ResolutionResult(
            canonical_item_id=best.canonical_item_id,
            match_type="fuzzy",
            score=best.score,
            alias_written=True,
            review_status=review_status,
            review_reason=review_reason,
        )
        return _apply_confidence_override(result, confidence)

    if best is not None and best.score >= POSSIBLE_DUPLICATE_THRESHOLD:
        result = ResolutionResult(
            canonical_item_id=best.canonical_item_id,
            match_type="fuzzy",
            score=best.score,
            alias_written=False,
            review_status=ReviewStatus.possible_duplicate.value,
            review_reason=ReviewReason.alias_confidence.value,
        )
        return _apply_confidence_override(result, confidence)

    new_item = CanonicalItem(
        canonical_name=normalized,
        display_name=raw_name.strip(),
        category=category,
        item_class=item_class,
        default_unit=default_unit,
        default_container_size=default_container_size,
        default_location=default_location,
    )
    session.add(new_item)
    session.flush()
    result = ResolutionResult(
        canonical_item_id=new_item.id,
        match_type="new",
        score=best.score if best else None,
        alias_written=False,
        review_status=ReviewStatus.needs_review.value,
        review_reason=ReviewReason.new_canonical_item.value,
    )
    return _apply_confidence_override(result, confidence)


def _apply_confidence_override(result: ResolutionResult, confidence: float | None) -> ResolutionResult:
    if confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD:
        result.review_status = ReviewStatus.needs_review.value
        result.review_reason = ReviewReason.low_confidence.value
    return result


def live_rows_for_canonical_item(session: Session, canonical_item_id: int) -> list[PantryItem]:
    """Non-superseded rows for a canonical item, across however many
    pantry_items rows it's split into (different expiration dates, etc.)."""
    return list(
        session.execute(
            select(PantryItem)
            .where(
                PantryItem.canonical_item_id == canonical_item_id,
                PantryItem.removed_at.is_(None),
            )
        ).scalars()
    )


def total_countable_quantity(session: Session, canonical_item_id: int) -> int:
    return sum((row.quantity or 0) for row in live_rows_for_canonical_item(session, canonical_item_id))


def best_staple_state(session: Session, canonical_item_id: int) -> str | None:
    rows = live_rows_for_canonical_item(session, canonical_item_id)
    states = [row.state for row in rows if row.state]
    if not states:
        return None
    return max(states, key=lambda s: _STAPLE_PRIORITY.get(s, -1))


def fefo_depletion_candidates(session: Session, canonical_item_id: int) -> list[PantryItem]:
    """Rows ordered earliest-expiration-first, NULL expiration last, tie
    broken by added_at ascending (oldest stock first) -- FEFO."""
    return list(
        session.execute(
            select(PantryItem)
            .where(
                PantryItem.canonical_item_id == canonical_item_id,
                PantryItem.removed_at.is_(None),
                PantryItem.quantity.is_not(None),
                PantryItem.quantity > 0,
            )
            .order_by(
                PantryItem.expiration_date.is_(None),
                PantryItem.expiration_date.asc(),
                PantryItem.added_at.asc(),
            )
        ).scalars()
    )


def oldest_full_staple_row(session: Session, canonical_item_id: int) -> PantryItem | None:
    rows = [
        r
        for r in live_rows_for_canonical_item(session, canonical_item_id)
        if r.state == StapleState.full.value
    ]
    if not rows:
        return None
    return min(rows, key=lambda r: r.added_at)
