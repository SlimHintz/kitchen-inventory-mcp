from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import matching
from ..enums import ChangeType, ItemClass, StapleState
from ..errors import DepletionShortfallError, KitchenError, NotFoundError
from ..models import (
    CanonicalItem,
    ItemHistory,
    MealLog,
    Recipe,
    RecipeIngredient,
    RecipeRating,
    utcnow,
)
from ..schemas import (
    DeleteRecipeInput,
    GetRecipeHistoryInput,
    IngredientInput,
    LogMealInput,
    RateRecipeInput,
    SaveRecipeInput,
    UndoMealLogInput,
    UpdateRecipeInput,
)


def _resolve_ingredients(session: Session, recipe: Recipe, ingredients: list[IngredientInput]) -> list[str]:
    unmatched = []
    for ing in ingredients:
        canonical_id, _score = matching.lookup_canonical_item(session, ing.raw_name)
        session.add(
            RecipeIngredient(
                recipe_id=recipe.id,
                canonical_item_id=canonical_id,
                raw_name=ing.raw_name,
                quantity=ing.quantity,
                unit=ing.unit,
                optional=ing.optional,
                substitution_notes=ing.substitution_notes,
            )
        )
        if canonical_id is None:
            unmatched.append(ing.raw_name)
    return unmatched


def save_recipe(session: Session, payload: SaveRecipeInput) -> dict:
    recipe = Recipe(
        name=payload.recipe.name,
        description=payload.recipe.description,
        instructions=payload.recipe.instructions,
        prep_time_min=payload.recipe.prep_time_min,
        cook_time_min=payload.recipe.cook_time_min,
        base_servings=payload.recipe.base_servings,
        tags=payload.recipe.tags,
        source=payload.recipe.source,
    )
    session.add(recipe)
    session.flush()
    unmatched = _resolve_ingredients(session, recipe, payload.ingredients)
    session.commit()
    return {"recipe_id": recipe.id, "unmatched_ingredients": unmatched}


def _recipe_or_error(session: Session, recipe_id: int) -> Recipe:
    recipe = session.get(Recipe, recipe_id)
    if recipe is None:
        raise NotFoundError("recipe", recipe_id)
    return recipe


def update_recipe(session: Session, payload: UpdateRecipeInput) -> dict:
    recipe = _recipe_or_error(session, payload.recipe_id)
    fields = payload.fields.model_dump(exclude_unset=True, exclude={"ingredients"})
    for key, value in fields.items():
        setattr(recipe, key, value)

    unmatched: list[str] = []
    if payload.fields.ingredients is not None:
        existing = session.execute(
            select(RecipeIngredient).where(RecipeIngredient.recipe_id == recipe.id)
        ).scalars()
        for row in existing:
            session.delete(row)
        session.flush()
        unmatched = _resolve_ingredients(session, recipe, payload.fields.ingredients)

    session.commit()
    return {"recipe_id": recipe.id, "unmatched_ingredients": unmatched}


def delete_recipe(session: Session, payload: DeleteRecipeInput) -> dict:
    recipe = _recipe_or_error(session, payload.recipe_id)
    recipe.archived_at = utcnow()
    session.commit()
    return {"recipe_id": recipe.id, "archived_at": recipe.archived_at.isoformat()}


def rate_recipe(session: Session, payload: RateRecipeInput) -> dict:
    _recipe_or_error(session, payload.recipe_id)
    existing = session.execute(
        select(RecipeRating).where(
            RecipeRating.recipe_id == payload.recipe_id, RecipeRating.rater == payload.rater
        )
    ).scalar_one_or_none()
    if existing:
        existing.rating = payload.rating
        existing.notes = payload.notes
        existing.rated_at = utcnow()
        row = existing
    else:
        row = RecipeRating(
            recipe_id=payload.recipe_id, rater=payload.rater, rating=payload.rating, notes=payload.notes
        )
        session.add(row)
    session.commit()
    return {"recipe_id": payload.recipe_id, "rater": payload.rater, "rating": payload.rating}


def log_meal(session: Session, payload: LogMealInput) -> dict:
    recipe = _recipe_or_error(session, payload.recipe_id)
    if recipe.archived_at is not None:
        raise KitchenError("RECIPE_ARCHIVED", "cannot log a meal against an archived recipe")

    ingredients = list(
        session.execute(
            select(RecipeIngredient).where(RecipeIngredient.recipe_id == recipe.id)
        ).scalars()
    )
    resolvable = [i for i in ingredients if i.canonical_item_id is not None]
    if not resolvable:
        raise KitchenError(
            "NO_RESOLVABLE_INGREDIENTS",
            "recipe has zero resolvable ingredients; nothing to deplete",
            {"recipe_id": recipe.id},
        )

    scale = payload.servings_made / recipe.base_servings

    plan = []
    shortfalls = []
    for ing in resolvable:
        canonical = session.get(CanonicalItem, ing.canonical_item_id)
        if canonical.item_class == ItemClass.countable.value:
            needed = math.ceil(ing.quantity * scale)
            have = matching.total_countable_quantity(session, canonical.id)
            plan.append(
                {"kind": "countable", "canonical": canonical, "needed": needed, "have": have}
            )
            if needed > have:
                shortfalls.append({"item": canonical.display_name, "needed": needed, "have": have})
        else:
            best_state = matching.best_staple_state(session, canonical.id)
            plan.append({"kind": "staple", "canonical": canonical, "have_state": best_state})
            if best_state is None or best_state == StapleState.out.value:
                shortfalls.append(
                    {"item": canonical.display_name, "needed": "some", "have": best_state or "not_in_inventory"}
                )

    if shortfalls and not payload.force:
        raise DepletionShortfallError(shortfalls)

    meal_log = MealLog(
        recipe_id=recipe.id,
        cooked_at=payload.cooked_at or utcnow(),
        servings_made=payload.servings_made,
        cooked_by=payload.cooked_by,
        notes=payload.notes,
        depletion_applied=True,
    )
    session.add(meal_log)
    session.flush()

    depleted = []
    staple_transitions = []
    clamped = []

    for p in plan:
        canonical = p["canonical"]
        if p["kind"] == "countable":
            needed = p["needed"]
            rows = matching.fefo_depletion_candidates(session, canonical.id)
            total_available = sum(r.quantity for r in rows)
            take_total = min(needed, total_available)
            remaining = take_total
            for row in rows:
                if remaining <= 0:
                    break
                take = min(row.quantity, remaining)
                row.quantity -= take
                remaining -= take
                session.add(
                    ItemHistory(
                        pantry_item_id=row.id,
                        change_type=ChangeType.depleted.value,
                        quantity_delta=-take,
                        actor=payload.cooked_by,
                        related_meal_log_id=meal_log.id,
                    )
                )
                depleted.append(
                    {
                        "pantry_item_id": row.id,
                        "canonical_item": canonical.canonical_name,
                        "taken": take,
                        "expiration_date": row.expiration_date.isoformat() if row.expiration_date else None,
                    }
                )
            if take_total < needed:
                clamped.append({"item": canonical.display_name, "needed": needed, "clamped_to": take_total})
        else:
            row = matching.oldest_full_staple_row(session, canonical.id)
            if row is not None:
                row.state = StapleState.open.value
                session.add(
                    ItemHistory(
                        pantry_item_id=row.id,
                        change_type=ChangeType.depleted.value,
                        state_from=StapleState.full.value,
                        state_to=StapleState.open.value,
                        actor=payload.cooked_by,
                        related_meal_log_id=meal_log.id,
                    )
                )
                staple_transitions.append(
                    {"pantry_item_id": row.id, "canonical_item": canonical.canonical_name, "state_from": "full", "state_to": "open"}
                )
            elif p["have_state"] in (None, StapleState.out.value):
                clamped.append({"item": canonical.display_name, "note": "not in inventory or already out; no transition applied"})

    session.commit()
    return {
        "meal_log_id": meal_log.id,
        "depleted": depleted,
        "staple_transitions": staple_transitions,
        "clamped": clamped,
    }


def undo_meal_log(session: Session, payload: UndoMealLogInput) -> dict:
    meal_log = session.get(MealLog, payload.meal_log_id)
    if meal_log is None:
        raise NotFoundError("meal_log", payload.meal_log_id)
    if meal_log.undone_at is not None:
        raise KitchenError("ALREADY_UNDONE", f"meal_log {meal_log.id} was already undone")

    history_rows = list(
        session.execute(
            select(ItemHistory).where(ItemHistory.related_meal_log_id == meal_log.id)
        ).scalars()
    )
    from ..models import PantryItem

    restored = []
    for h in history_rows:
        row = session.get(PantryItem, h.pantry_item_id)
        if h.quantity_delta is not None:
            row.quantity = (row.quantity or 0) - h.quantity_delta
            session.add(
                ItemHistory(
                    pantry_item_id=row.id,
                    change_type=ChangeType.undo.value,
                    quantity_delta=-h.quantity_delta,
                    actor=payload.actor,
                    related_meal_log_id=meal_log.id,
                )
            )
            restored.append({"pantry_item_id": row.id, "quantity_restored": -h.quantity_delta})
        elif h.state_to is not None:
            row.state = h.state_from
            session.add(
                ItemHistory(
                    pantry_item_id=row.id,
                    change_type=ChangeType.undo.value,
                    state_from=h.state_to,
                    state_to=h.state_from,
                    actor=payload.actor,
                    related_meal_log_id=meal_log.id,
                )
            )
            restored.append({"pantry_item_id": row.id, "state_restored_to": h.state_from})

    meal_log.undone_at = utcnow()
    session.commit()
    return {"meal_log_id": meal_log.id, "restored": restored}


def get_recipe_history(session: Session, payload: GetRecipeHistoryInput) -> dict:
    query = select(Recipe)
    if not payload.include_archived:
        query = query.where(Recipe.archived_at.is_(None))
    recipes = list(session.execute(query).scalars())

    results = []
    for recipe in recipes:
        if payload.tag and payload.tag not in (recipe.tags or []):
            continue
        ratings = list(
            session.execute(select(RecipeRating).where(RecipeRating.recipe_id == recipe.id)).scalars()
        )
        ratings_by_rater = {r.rater: r.rating for r in ratings}
        avg_rating = sum(ratings_by_rater.values()) / len(ratings_by_rater) if ratings_by_rater else None
        if payload.min_rating is not None and (avg_rating is None or avg_rating < payload.min_rating):
            continue

        logs = list(
            session.execute(
                select(MealLog).where(MealLog.recipe_id == recipe.id, MealLog.undone_at.is_(None))
            ).scalars()
        )
        if payload.cooked_since is not None:
            logs_since = [log for log in logs if log.cooked_at >= payload.cooked_since]
            if not logs_since:
                continue
        cook_count = len(logs)
        last_cooked_at = max((log.cooked_at for log in logs), default=None)

        results.append(
            {
                "recipe_id": recipe.id,
                "name": recipe.name,
                "tags": recipe.tags,
                "base_servings": recipe.base_servings,
                "ratings_by_rater": ratings_by_rater,
                "average_rating": avg_rating,
                "cook_count": cook_count,
                "last_cooked_at": last_cooked_at.isoformat() if last_cooked_at else None,
                "archived": recipe.archived_at is not None,
            }
        )

    return {"recipes": results}
