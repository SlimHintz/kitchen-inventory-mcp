"""Shared string-backed enums.

Plain Python str-enums stored as VARCHAR (native_enum=False on the SQLAlchemy
side) rather than native DB enums, so a future Postgres migration doesn't
need an ALTER TYPE dance.
"""

from enum import Enum


class Actor(str, Enum):
    alex = "alex"
    sam = "sam"
    system = "system"


class Location(str, Enum):
    pantry = "pantry"
    fridge = "fridge"
    freezer = "freezer"


class ItemClass(str, Enum):
    countable = "countable"
    staple = "staple"


class StapleState(str, Enum):
    full = "full"
    open = "open"
    low = "low"
    out = "out"


class Category(str, Enum):
    produce = "produce"
    dairy = "dairy"
    pantry = "pantry"
    frozen = "frozen"
    spice = "spice"
    protein = "protein"
    condiment = "condiment"
    baking = "baking"
    other = "other"


class Source(str, Enum):
    photo = "photo"
    order_import = "order_import"
    manual = "manual"
    depletion = "depletion"


class ReviewStatus(str, Enum):
    ok = "ok"
    needs_review = "needs_review"
    possible_duplicate = "possible_duplicate"


class ReviewReason(str, Enum):
    low_confidence = "low_confidence"
    new_canonical_item = "new_canonical_item"
    alias_confidence = "alias_confidence"
    possible_stock_duplicate = "possible_stock_duplicate"
    perfect_fuzzy_score = "perfect_fuzzy_score"
    order_replay = "order_replay"


class ChangeType(str, Enum):
    added = "added"
    depleted = "depleted"
    expired = "expired"
    manual_adjust = "manual_adjust"
    removed = "removed"
    undo = "undo"


class RemovalReason(str, Enum):
    used_up = "used_up"
    expired = "expired"
    thrown_out = "thrown_out"


class DuplicateAction(str, Enum):
    merge = "merge"
    keep_separate = "keep_separate"
    discard = "discard"


class OrderBatchAction(str, Enum):
    discard = "discard"
    keep_separate = "keep_separate"


class RecipeSource(str, Enum):
    ai_suggested = "ai_suggested"
    user_added = "user_added"


class PreferenceKind(str, Enum):
    allergy = "allergy"
    dislike = "dislike"
    avoid = "avoid"
    favorite = "favorite"


class PreferenceSubject(str, Enum):
    alex = "alex"
    sam = "sam"
    kid = "kid"
    household = "household"
