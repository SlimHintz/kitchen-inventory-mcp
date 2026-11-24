"""Correction 6: get_planning_context must cap the saved-recipe payload
instead of returning every saved recipe."""

from datetime import timedelta

from kitchen_inventory_mcp.models import MealLog, Recipe, RecipeRating, utcnow
from kitchen_inventory_mcp.schemas import GetPlanningContextInput
from kitchen_inventory_mcp.tools.planning import get_planning_context


def _add_recipe(session, name, rating=None):
    recipe = Recipe(name=name, base_servings=4, tags=[], source="user_added")
    session.add(recipe)
    session.flush()
    if rating is not None:
        session.add(RecipeRating(recipe_id=recipe.id, rater="alex", rating=rating))
    return recipe


def test_recipe_payload_is_capped_at_configured_limit(session):
    for i in range(20):
        _add_recipe(session, f"Recipe {i}", rating=(i % 5) + 1)
    session.commit()

    result = get_planning_context(session, GetPlanningContextInput(recipe_limit=5))
    assert result["recipe_count_total_saved"] == 20
    assert len(result["recipes"]) == 5


def test_recipe_payload_returns_everything_when_under_the_cap(session):
    for i in range(10):
        _add_recipe(session, f"Recipe {i}", rating=(i % 5) + 1)
    session.commit()

    result = get_planning_context(session, GetPlanningContextInput(recipe_limit=40))
    assert result["recipe_count_total_saved"] == 10
    assert len(result["recipes"]) == 10


def test_recently_cooked_recipe_included_even_if_outside_top_n(session):
    for i in range(5):
        _add_recipe(session, f"Top {i}", rating=5)
    low_rated_but_recent = _add_recipe(session, "Recently Cooked Dud", rating=1)
    session.commit()

    session.add(
        MealLog(
            recipe_id=low_rated_but_recent.id,
            cooked_at=utcnow() - timedelta(days=2),
            servings_made=4,
            cooked_by="alex",
            depletion_applied=False,
        )
    )
    session.commit()

    result = get_planning_context(session, GetPlanningContextInput(recipe_limit=5))
    names = {r["name"] for r in result["recipes"]}
    assert "Recently Cooked Dud" in names
    assert len(result["recipes"]) == 6  # top 5 + the one recent addition
