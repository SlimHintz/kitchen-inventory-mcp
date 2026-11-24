from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import matching
from ..enums import ItemClass, PreferenceKind, StapleState
from ..errors import NotFoundError
from ..models import CanonicalItem, GrocerySuggestion, HouseholdPreference, MealLog, Recipe, utcnow
from ..schemas import (
    DismissGrocerySuggestionInput,
    GetInventoryInput,
    GetPlanningContextInput,
    ScoreRecipeCoverageInput,
    SuggestGroceriesInput,
)
from . import inventory as inventory_tools

RECENTLY_COOKED_WINDOW_DAYS = 14
HIGH_RATING_THRESHOLD = 4.0


def get_planning_context(session: Session, payload: GetPlanningContextInput) -> dict:
    inv = inventory_tools.get_inventory(session, GetInventoryInput())
    preferences = list(session.execute(select(HouseholdPreference)).scalars())

    cutoff = utcnow() - timedelta(days=RECENTLY_COOKED_WINDOW_DAYS)
    recent_logs = list(
        session.execute(
            select(MealLog).where(MealLog.cooked_at >= cutoff, MealLog.undone_at.is_(None))
        ).scalars()
    )
    recently_cooked_ids = {log.recipe_id for log in recent_logs}

    all_recipes = list(session.execute(select(Recipe).where(Recipe.archived_at.is_(None))).scalars())

    def avg_rating(recipe: Recipe) -> float:
        ratings = [r.rating for r in recipe.ratings]
        return sum(ratings) / len(ratings) if ratings else -1.0

    ranked = sorted(all_recipes, key=avg_rating, reverse=True)
    top_n = ranked[: payload.recipe_limit]
    top_ids = {r.id for r in top_n}
    extra = [r for r in all_recipes if r.id in recently_cooked_ids and r.id not in top_ids]
    selected = top_n + extra

    recipe_payload = []
    for recipe in selected:
        ratings_by_rater = {r.rater: r.rating for r in recipe.ratings}
        logs = list(
            session.execute(
                select(MealLog).where(MealLog.recipe_id == recipe.id, MealLog.undone_at.is_(None))
            ).scalars()
        )
        last_cooked_at = max((log.cooked_at for log in logs), default=None)
        recipe_payload.append(
            {
                "recipe_id": recipe.id,
                "name": recipe.name,
                "tags": recipe.tags,
                "base_servings": recipe.base_servings,
                "ratings_by_rater": ratings_by_rater,
                "cook_count": len(logs),
                "last_cooked_at": last_cooked_at.isoformat() if last_cooked_at else None,
            }
        )

    return {
        "inventory": inv,
        "expiring_soon": inv["expiring_soon"],
        "preferences": [
            {
                "id": p.id,
                "kind": p.kind,
                "subject": p.subject,
                "applies_to": p.applies_to,
                "notes": p.notes,
            }
            for p in preferences
        ],
        "recipes": recipe_payload,
        "recipe_count_total_saved": len(all_recipes),
        "recently_cooked_14d": [
            {"recipe_id": log.recipe_id, "cooked_at": log.cooked_at.isoformat()} for log in recent_logs
        ],
    }


def score_recipe_coverage(session: Session, payload: ScoreRecipeCoverageInput) -> dict:
    results = []
    for ing in payload.ingredients:
        canonical_id, _score = matching.lookup_canonical_item(session, ing.raw_name)
        if canonical_id is None:
            results.append({"raw_name": ing.raw_name, "canonical_item_id": None, "status": "unknown"})
            continue

        canonical = session.get(CanonicalItem, canonical_id)
        if canonical.item_class == ItemClass.countable.value:
            total = matching.total_countable_quantity(session, canonical_id)
            if total <= 0:
                status = "missing"
            elif total >= ing.quantity:
                status = "have"
            else:
                status = "partial"
        else:
            best_state = matching.best_staple_state(session, canonical_id)
            if best_state is None or best_state == StapleState.out.value:
                status = "missing"
            elif best_state == StapleState.low.value:
                status = "partial"
            else:
                status = "have"

        results.append({"raw_name": ing.raw_name, "canonical_item_id": canonical_id, "status": status})

    weight = {"have": 1.0, "partial": 0.5, "missing": 0.0, "unknown": 0.0}
    coverage_score = sum(weight[r["status"]] for r in results) / len(results) if results else 0.0
    missing_items = [r["raw_name"] for r in results if r["status"] in ("missing", "unknown")]
    return {"results": results, "coverage_score": coverage_score, "missing_items": missing_items}


def _is_missing(session: Session, canonical: CanonicalItem, needed_quantity: float | None) -> bool:
    if canonical.item_class == ItemClass.countable.value:
        have = matching.total_countable_quantity(session, canonical.id)
        return have < (needed_quantity or 1)
    state = matching.best_staple_state(session, canonical.id)
    return state in (None, StapleState.out.value)


def suggest_groceries(session: Session, payload: SuggestGroceriesInput) -> dict:
    candidates: dict[int, dict] = {}

    staple_items = session.execute(
        select(CanonicalItem).where(CanonicalItem.item_class == ItemClass.staple.value)
    ).scalars()
    for ci in staple_items:
        state = matching.best_staple_state(session, ci.id)
        if state == StapleState.out.value:
            candidates[ci.id] = {
                "item_name": ci.display_name,
                "reason": "staple is out",
                "priority": 90,
                "unlocks_recipes": [],
            }
        elif state == StapleState.low.value:
            candidates.setdefault(
                ci.id,
                {
                    "item_name": ci.display_name,
                    "reason": "staple is running low",
                    "priority": 60,
                    "unlocks_recipes": [],
                },
            )

    all_recipes = list(session.execute(select(Recipe).where(Recipe.archived_at.is_(None))).scalars())
    for recipe in all_recipes:
        ratings = [r.rating for r in recipe.ratings]
        if not ratings or (sum(ratings) / len(ratings)) < HIGH_RATING_THRESHOLD:
            continue
        for ing in recipe.ingredients:
            if ing.canonical_item_id is None:
                continue
            canonical = session.get(CanonicalItem, ing.canonical_item_id)
            if not _is_missing(session, canonical, ing.quantity):
                continue
            entry = candidates.setdefault(
                canonical.id,
                {
                    "item_name": canonical.display_name,
                    "reason": "needed for a highly-rated saved recipe",
                    "priority": 50,
                    "unlocks_recipes": [],
                },
            )
            if recipe.name not in entry["unlocks_recipes"]:
                entry["unlocks_recipes"].append(recipe.name)
            entry["priority"] = max(entry["priority"], 50 + 5 * len(entry["unlocks_recipes"]))

    favorites = session.execute(
        select(HouseholdPreference).where(HouseholdPreference.kind == PreferenceKind.favorite.value)
    ).scalars()
    for pref in favorites:
        canonical_id, _score = matching.lookup_canonical_item(session, pref.subject)
        if canonical_id is None:
            continue
        canonical = session.get(CanonicalItem, canonical_id)
        if not _is_missing(session, canonical, None):
            continue
        candidates.setdefault(
            canonical.id,
            {
                "item_name": canonical.display_name,
                "reason": f"household favorite ({pref.applies_to})",
                "priority": 55,
                "unlocks_recipes": [],
            },
        )

    ranked = sorted(candidates.items(), key=lambda kv: -kv[1]["priority"])[: payload.limit]

    suggestions = []
    for canonical_id, data in ranked:
        gs = GrocerySuggestion(
            canonical_item_id=canonical_id,
            item_name=data["item_name"],
            reason=data["reason"],
            priority=data["priority"],
        )
        session.add(gs)
        session.flush()
        suggestions.append(
            {
                "id": gs.id,
                "item_name": data["item_name"],
                "canonical_item_id": canonical_id,
                "reason": data["reason"],
                "priority": data["priority"],
                "unlocks_recipes": data["unlocks_recipes"],
            }
        )
    session.commit()

    return {"suggestions": suggestions, "based_on_n_saved_recipes": len(all_recipes)}


def dismiss_grocery_suggestion(session: Session, payload: DismissGrocerySuggestionInput) -> dict:
    gs = session.get(GrocerySuggestion, payload.id)
    if gs is None:
        raise NotFoundError("grocery_suggestion", payload.id)
    gs.dismissed = True
    gs.dismissed_by = payload.actor
    gs.dismissed_at = utcnow()
    session.commit()
    return {"id": gs.id, "dismissed": True}
