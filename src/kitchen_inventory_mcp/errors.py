"""Every error return is a structured object with a stable error_code, never
a raw exception string."""

from __future__ import annotations

from typing import Any


class KitchenError(Exception):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None):
        self.error_code = error_code
        self.message = message
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"error_code": self.error_code, "message": self.message, "details": self.details}


class NotFoundError(KitchenError):
    def __init__(self, entity: str, entity_id: int | str):
        super().__init__(
            error_code=f"{entity.upper()}_NOT_FOUND",
            message=f"{entity} {entity_id} not found",
            details={"entity": entity, "id": entity_id},
        )


class DepletionShortfallError(KitchenError):
    def __init__(self, shortfalls: list[dict[str, Any]]):
        super().__init__(
            error_code="DEPLETION_SHORTFALL",
            message="Depletion would take one or more items below zero",
            details={"shortfalls": shortfalls},
        )
