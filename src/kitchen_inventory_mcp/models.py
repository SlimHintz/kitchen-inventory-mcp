"""SQLAlchemy 2.0 models.

No SQLite-specific types, JSON via the portable SQLAlchemy JSON type,
timezone-aware UTC timestamps everywhere -- chosen so this can move to
Postgres later without a schema rewrite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_batch_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class CanonicalItem(Base):
    __tablename__ = "canonical_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(20))
    item_class: Mapped[str] = mapped_column(String(20))
    default_unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    default_container_size: Mapped[str | None] = mapped_column(String(50), nullable=True)
    default_location: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    aliases: Mapped[list[ItemAlias]] = relationship(back_populates="canonical_item")
    pantry_items: Mapped[list[PantryItem]] = relationship(back_populates="canonical_item")


class ItemAlias(Base):
    __tablename__ = "item_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_item_id: Mapped[int] = mapped_column(ForeignKey("canonical_items.id"))
    alias: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    canonical_item: Mapped[CanonicalItem] = relationship(back_populates="aliases")


class PantryItem(Base):
    __tablename__ = "pantry_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("canonical_items.id"), nullable=True, index=True
    )
    raw_name: Mapped[str] = mapped_column(String(300))
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    container_size: Mapped[str | None] = mapped_column(String(50), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    location: Mapped[str] = mapped_column(String(20))
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_status: Mapped[str] = mapped_column(String(20), default="ok")
    review_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    possible_duplicate_of: Mapped[int | None] = mapped_column(
        ForeignKey("pantry_items.id"), nullable=True
    )

    # Correction 3: resolve_duplicate must soft-delete, never hard-delete,
    # so item_history.pantry_item_id never orphans.
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey("pantry_items.id"), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Correction 2: order_dedup_key is the client-supplied fingerprint used to
    # detect a whole order pasted twice; order_batch_id is unique per
    # add_inventory_items call and is what batch resolution actually scopes
    # to (so discarding a replay never touches the original submission's
    # rows even though they share the same order_dedup_key).
    order_dedup_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    order_batch_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    canonical_item: Mapped[CanonicalItem | None] = relationship(back_populates="pantry_items")
    history: Mapped[list[ItemHistory]] = relationship(
        back_populates="pantry_item", foreign_keys="ItemHistory.pantry_item_id"
    )


class ItemHistory(Base):
    __tablename__ = "item_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    pantry_item_id: Mapped[int] = mapped_column(ForeignKey("pantry_items.id"), index=True)
    change_type: Mapped[str] = mapped_column(String(20))
    quantity_delta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state_from: Mapped[str | None] = mapped_column(String(20), nullable=True)
    state_to: Mapped[str | None] = mapped_column(String(20), nullable=True)
    actor: Mapped[str] = mapped_column(String(20))
    related_meal_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("meal_log.id"), nullable=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    pantry_item: Mapped[PantryItem] = relationship(
        back_populates="history", foreign_keys=[pantry_item_id]
    )


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    prep_time_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cook_time_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_servings: Mapped[int] = mapped_column(Integer)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    ingredients: Mapped[list[RecipeIngredient]] = relationship(back_populates="recipe")
    ratings: Mapped[list[RecipeRating]] = relationship(back_populates="recipe")


class RecipeIngredient(Base):
    __tablename__ = "recipe_ingredients"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"), index=True)
    canonical_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("canonical_items.id"), nullable=True
    )
    raw_name: Mapped[str] = mapped_column(String(300))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    optional: Mapped[bool] = mapped_column(Boolean, default=False)
    substitution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    recipe: Mapped[Recipe] = relationship(back_populates="ingredients")
    canonical_item: Mapped[CanonicalItem | None] = relationship()


class RecipeRating(Base):
    __tablename__ = "recipe_ratings"
    __table_args__ = (UniqueConstraint("recipe_id", "rater", name="uq_recipe_rater"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"), index=True)
    rater: Mapped[str] = mapped_column(String(20))
    rating: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    rated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    recipe: Mapped[Recipe] = relationship(back_populates="ratings")


class MealLog(Base):
    __tablename__ = "meal_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"), index=True)
    cooked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    servings_made: Mapped[int] = mapped_column(Integer)
    cooked_by: Mapped[str] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    depletion_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    recipe: Mapped[Recipe] = relationship()


class GrocerySuggestion(Base):
    __tablename__ = "grocery_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("canonical_items.id"), nullable=True
    )
    item_name: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer)
    suggested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissed_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HouseholdPreference(Base):
    __tablename__ = "household_preferences"
    __table_args__ = (
        UniqueConstraint("kind", "subject", "applies_to", name="uq_preference_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(String(200))
    applies_to: Mapped[str] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Minimal OAuth wrapper around the shared bearer token -------------------
#
# These exist purely so remote MCP clients' account-level "custom connector" flow
# (used by web, mobile, and Desktop's GUI connector dialog) has an OAuth
# handshake to perform. There is no separate user/password system underneath
# -- /oauth/authorize just checks the same KITCHEN_MCP_TOKEN and, on match,
# issues an access token scoped to whichever client registered.


class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # client_id
    redirect_uris: Mapped[list] = mapped_column(JSON)
    client_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OAuthAuthorizationCode(Base):
    __tablename__ = "oauth_authorization_codes"

    code: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64))
    redirect_uri: Mapped[str] = mapped_column(String(500))
    code_challenge: Mapped[str] = mapped_column(String(200))
    code_challenge_method: Mapped[str] = mapped_column(String(10))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OAuthAccessToken(Base):
    __tablename__ = "oauth_access_tokens"

    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
