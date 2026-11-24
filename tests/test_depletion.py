from datetime import date

import pytest
from factories import make_canonical, make_pantry_item

from kitchen_inventory_mcp.errors import DepletionShortfallError
from kitchen_inventory_mcp.models import ItemHistory, MealLog, PantryItem
from kitchen_inventory_mcp.schemas import (
    IngredientInput,
    LogMealInput,
    RecipeInput,
    SaveRecipeInput,
    UndoMealLogInput,
)
from kitchen_inventory_mcp.tools.recipes import log_meal, save_recipe, undo_meal_log


def _save(session, name, base_servings, ingredients):
    payload = SaveRecipeInput(
        recipe=RecipeInput(name=name, base_servings=base_servings, source="user_added"),
        ingredients=ingredients,
        actor="alex",
    )
    result = save_recipe(session, payload)
    assert result["unmatched_ingredients"] == []
    return result["recipe_id"]


def test_worked_acceptance_example(session):
    tomato = make_canonical(session, "canned tomato", item_class="countable")
    onion = make_canonical(session, "onion", item_class="countable")
    rice = make_canonical(session, "rice", item_class="staple")
    oil = make_canonical(session, "olive oil", item_class="staple")

    make_pantry_item(session, tomato, quantity=3)
    make_pantry_item(session, onion, quantity=1)
    make_pantry_item(session, rice, state="full")
    make_pantry_item(session, oil, state="open")

    recipe_id = _save(
        session,
        "Tomato Rice",
        base_servings=4,
        ingredients=[
            IngredientInput(raw_name="canned tomato", quantity=2),
            IngredientInput(raw_name="onion", quantity=1),
            IngredientInput(raw_name="rice", quantity=1, unit="cup"),
            IngredientInput(raw_name="olive oil", quantity=2, unit="tbsp"),
        ],
    )

    with pytest.raises(DepletionShortfallError) as exc_info:
        log_meal(
            session,
            LogMealInput(recipe_id=recipe_id, servings_made=6, cooked_by="sam"),
        )
    assert exc_info.value.details["shortfalls"] == [{"item": "Onion", "needed": 2, "have": 1}]

    # Inventory completely unchanged.
    assert session.get(PantryItem, 1).quantity == 3
    assert session.get(PantryItem, 2).quantity == 1
    assert session.get(PantryItem, 3).state == "full"
    assert session.get(PantryItem, 4).state == "open"

    forced = log_meal(
        session,
        LogMealInput(recipe_id=recipe_id, servings_made=6, cooked_by="sam", force=True),
    )
    assert "meal_log_id" in forced
    assert session.get(PantryItem, 1).quantity == 0
    assert session.get(PantryItem, 2).quantity == 0
    assert session.get(PantryItem, 3).state == "open"
    assert session.get(PantryItem, 4).state == "open"
    assert forced["clamped"] == [{"item": "Onion", "needed": 2, "clamped_to": 1}]

    meal_log = session.get(MealLog, forced["meal_log_id"])
    assert meal_log is not None
    history = (
        session.query(ItemHistory).filter(ItemHistory.related_meal_log_id == meal_log.id).all()
    )
    assert len(history) == 3  # tomato, onion, rice transition; oil untouched (already open)


def test_fefo_depletes_earliest_expiration_first(session):
    tomato = make_canonical(session, "canned tomato", item_class="countable")
    row_sept = make_pantry_item(
        session, tomato, quantity=2, expiration_date=date(2026, 9, 1)
    )
    row_dec = make_pantry_item(session, tomato, quantity=1, expiration_date=date(2026, 12, 1))

    recipe_id = _save(
        session,
        "Tomato Soup",
        base_servings=1,
        ingredients=[IngredientInput(raw_name="canned tomato", quantity=3)],
    )

    result = log_meal(session, LogMealInput(recipe_id=recipe_id, servings_made=1, cooked_by="alex"))

    assert result["clamped"] == []
    session.refresh(row_sept)
    session.refresh(row_dec)
    assert row_sept.quantity == 0
    assert row_dec.quantity == 0

    depleted = result["depleted"]
    assert len(depleted) == 2
    assert depleted[0]["pantry_item_id"] == row_sept.id and depleted[0]["taken"] == 2
    assert depleted[1]["pantry_item_id"] == row_dec.id and depleted[1]["taken"] == 1

    # Two separate item_history rows, one per affected pantry row.
    hist_sept = session.query(ItemHistory).filter(ItemHistory.pantry_item_id == row_sept.id).all()
    hist_dec = session.query(ItemHistory).filter(ItemHistory.pantry_item_id == row_dec.id).all()
    assert len(hist_sept) == 1 and hist_sept[0].quantity_delta == -2
    assert len(hist_dec) == 1 and hist_dec[0].quantity_delta == -1


def test_shortfall_sums_across_all_rows_for_canonical_item(session):
    tomato = make_canonical(session, "canned tomato", item_class="countable")
    make_pantry_item(session, tomato, quantity=1, expiration_date=date(2026, 9, 1))
    make_pantry_item(session, tomato, quantity=1, expiration_date=date(2026, 12, 1))

    recipe_id = _save(
        session,
        "Tomato Soup",
        base_servings=1,
        ingredients=[IngredientInput(raw_name="canned tomato", quantity=3)],
    )

    with pytest.raises(DepletionShortfallError) as exc_info:
        log_meal(session, LogMealInput(recipe_id=recipe_id, servings_made=1, cooked_by="alex"))
    # have=2 (summed across both rows), not 1 (a single row's quantity).
    assert exc_info.value.details["shortfalls"] == [{"item": "Canned Tomato", "needed": 3, "have": 2}]


def test_fractional_scale_rounds_up(session):
    flour_can = make_canonical(session, "canned tomato", item_class="countable")
    make_pantry_item(session, flour_can, quantity=1)

    recipe_id = _save(
        session,
        "Something",
        base_servings=4,
        ingredients=[IngredientInput(raw_name="canned tomato", quantity=1)],
    )

    result = log_meal(session, LogMealInput(recipe_id=recipe_id, servings_made=2, cooked_by="alex"))
    assert "meal_log_id" in result
    assert result["depleted"][0]["taken"] == 1  # ceil(1 * 0.5) == 1, not 0.5


def test_staple_transitions(session):
    rice = make_canonical(session, "rice", item_class="staple")
    make_pantry_item(session, rice, state="open")

    recipe_id = _save(
        session,
        "Rice Bowl",
        base_servings=1,
        ingredients=[IngredientInput(raw_name="rice", quantity=1)],
    )
    result = log_meal(session, LogMealInput(recipe_id=recipe_id, servings_made=1, cooked_by="alex"))
    assert "meal_log_id" in result
    assert result["staple_transitions"] == []  # already open, stays open, no-op

    row = session.query(PantryItem).filter(PantryItem.canonical_item_id == rice.id).one()
    assert row.state == "open"


def test_undo_restores_exact_pre_log_state(session):
    tomato = make_canonical(session, "canned tomato", item_class="countable")
    row = make_pantry_item(session, tomato, quantity=3)
    rice = make_canonical(session, "rice", item_class="staple")
    rice_row = make_pantry_item(session, rice, state="full")

    recipe_id = _save(
        session,
        "Tomato Rice",
        base_servings=1,
        ingredients=[
            IngredientInput(raw_name="canned tomato", quantity=2),
            IngredientInput(raw_name="rice", quantity=1),
        ],
    )
    result = log_meal(session, LogMealInput(recipe_id=recipe_id, servings_made=1, cooked_by="alex"))
    session.refresh(row)
    session.refresh(rice_row)
    assert row.quantity == 1
    assert rice_row.state == "open"

    undo_result = undo_meal_log(session, UndoMealLogInput(meal_log_id=result["meal_log_id"], actor="alex"))
    session.refresh(row)
    session.refresh(rice_row)
    assert row.quantity == 3
    assert rice_row.state == "full"
    assert len(undo_result["restored"]) == 2

    meal_log = session.get(MealLog, result["meal_log_id"])
    assert meal_log.undone_at is not None
