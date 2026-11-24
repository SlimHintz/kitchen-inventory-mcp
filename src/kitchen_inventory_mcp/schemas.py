"""Pydantic input models for every tool signature, so malformed input fails
validation naming the offending field rather than raising deep inside the
business logic."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

ActorLiteral = Literal["alex", "sam"]
RaterLiteral = Literal["alex", "sam"]
LocationLiteral = Literal["pantry", "fridge", "freezer"]
CategoryLiteral = Literal[
    "produce", "dairy", "pantry", "frozen", "spice", "protein", "condiment", "baking", "other"
]
ItemClassLiteral = Literal["countable", "staple"]
StapleStateLiteral = Literal["full", "open", "low", "out"]
SourceLiteral = Literal["photo", "order_import", "manual"]
ReviewStatusLiteral = Literal["ok", "needs_review", "possible_duplicate"]
RemovalReasonLiteral = Literal["used_up", "expired", "thrown_out"]
DuplicateActionLiteral = Literal["merge", "keep_separate", "discard"]
OrderBatchActionLiteral = Literal["discard", "keep_separate"]
RecipeSourceLiteral = Literal["ai_suggested", "user_added"]
PreferenceKindLiteral = Literal["allergy", "dislike", "avoid", "favorite"]
PreferenceSubjectLiteral = Literal["alex", "sam", "kid", "household"]


class ItemInput(BaseModel):
    raw_name: str = Field(min_length=1)
    quantity: int | None = None
    state: StapleStateLiteral | None = None
    unit: str | None = None
    container_size: str | None = None
    category: CategoryLiteral
    item_class: ItemClassLiteral
    location: LocationLiteral
    expiration_date: date | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: SourceLiteral


class AddInventoryItemsInput(BaseModel):
    items: list[ItemInput] = Field(min_length=1)
    actor: ActorLiteral
    order_dedup_key: str | None = None


class ResolveDuplicateInput(BaseModel):
    pantry_item_id: int
    action: DuplicateActionLiteral
    target_item_id: int | None = None
    actor: ActorLiteral


class ResolveOrderBatchInput(BaseModel):
    order_batch_id: str
    action: OrderBatchActionLiteral
    actor: ActorLiteral


class GetInventoryInput(BaseModel):
    category: CategoryLiteral | None = None
    location: LocationLiteral | None = None
    item_class: ItemClassLiteral | None = None
    review_status: ReviewStatusLiteral | None = None
    expiring_within_days: int = 7
    include_out: bool = False


class UpdateInventoryItemFields(BaseModel):
    quantity: int | None = None
    state: StapleStateLiteral | None = None
    location: LocationLiteral | None = None
    expiration_date: date | None = None
    category: CategoryLiteral | None = None
    raw_name: str | None = None
    container_size: str | None = None
    unit: str | None = None


class UpdateInventoryItemInput(BaseModel):
    id: int
    fields: UpdateInventoryItemFields
    actor: ActorLiteral


class RemoveInventoryItemInput(BaseModel):
    id: int
    reason: RemovalReasonLiteral
    actor: ActorLiteral


class GetItemHistoryInput(BaseModel):
    item_id: int | None = None
    since: datetime | None = None
    limit: int | None = 100


class SweepExpiredInput(BaseModel):
    dry_run: bool = True
    actor: ActorLiteral


class IngredientInput(BaseModel):
    raw_name: str = Field(min_length=1)
    quantity: float
    unit: str | None = None
    optional: bool = False
    substitution_notes: str | None = None


class ScoreRecipeCoverageInput(BaseModel):
    ingredients: list[IngredientInput] = Field(min_length=1)


class SuggestGroceriesInput(BaseModel):
    limit: int = 10


class DismissGrocerySuggestionInput(BaseModel):
    id: int
    actor: ActorLiteral


class RecipeInput(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    instructions: str | None = None
    prep_time_min: int | None = None
    cook_time_min: int | None = None
    base_servings: int = Field(gt=0)
    tags: list[str] = Field(default_factory=list)
    source: RecipeSourceLiteral = "user_added"


class SaveRecipeInput(BaseModel):
    recipe: RecipeInput
    ingredients: list[IngredientInput] = Field(min_length=1)
    actor: ActorLiteral


class UpdateRecipeFields(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    prep_time_min: int | None = None
    cook_time_min: int | None = None
    base_servings: int | None = Field(default=None, gt=0)
    tags: list[str] | None = None
    ingredients: list[IngredientInput] | None = None


class UpdateRecipeInput(BaseModel):
    recipe_id: int
    fields: UpdateRecipeFields
    actor: ActorLiteral


class DeleteRecipeInput(BaseModel):
    recipe_id: int
    actor: ActorLiteral


class RateRecipeInput(BaseModel):
    recipe_id: int
    rater: RaterLiteral
    rating: int = Field(ge=1, le=5)
    notes: str | None = None


class LogMealInput(BaseModel):
    recipe_id: int
    servings_made: int = Field(gt=0)
    cooked_by: ActorLiteral
    cooked_at: datetime | None = None
    notes: str | None = None
    force: bool = False


class UndoMealLogInput(BaseModel):
    meal_log_id: int
    actor: ActorLiteral


class GetRecipeHistoryInput(BaseModel):
    tag: str | None = None
    min_rating: float | None = None
    cooked_since: datetime | None = None
    include_archived: bool = False


class GetPlanningContextInput(BaseModel):
    recipe_limit: int = 40


class SetPreferenceInput(BaseModel):
    kind: PreferenceKindLiteral
    subject: str = Field(min_length=1)
    applies_to: PreferenceSubjectLiteral
    notes: str | None = None
    actor: ActorLiteral


class RemovePreferenceInput(BaseModel):
    id: int
    actor: ActorLiteral
