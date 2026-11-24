from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import NotFoundError
from ..models import HouseholdPreference
from ..schemas import RemovePreferenceInput, SetPreferenceInput


def get_preferences(session: Session) -> dict:
    rows = list(session.execute(select(HouseholdPreference)).scalars())
    return {
        "preferences": [
            {
                "id": p.id,
                "kind": p.kind,
                "subject": p.subject,
                "applies_to": p.applies_to,
                "notes": p.notes,
            }
            for p in rows
        ]
    }


def set_preference(session: Session, payload: SetPreferenceInput) -> dict:
    existing = session.execute(
        select(HouseholdPreference).where(
            HouseholdPreference.kind == payload.kind,
            HouseholdPreference.subject == payload.subject,
            HouseholdPreference.applies_to == payload.applies_to,
        )
    ).scalar_one_or_none()
    if existing:
        existing.notes = payload.notes
        row = existing
    else:
        row = HouseholdPreference(
            kind=payload.kind, subject=payload.subject, applies_to=payload.applies_to, notes=payload.notes
        )
        session.add(row)
    session.commit()
    return {"id": row.id, "kind": row.kind, "subject": row.subject, "applies_to": row.applies_to}


def remove_preference(session: Session, payload: RemovePreferenceInput) -> dict:
    row = session.get(HouseholdPreference, payload.id)
    if row is None:
        raise NotFoundError("preference", payload.id)
    session.delete(row)
    session.commit()
    return {"id": payload.id, "removed": True}
