"""
Public, unauthenticated endpoints — safe to call from marketing pages
(website footer, About sections) without a logged-in session.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database.tables import PlatformSetting
from repository.dependencies import get_db

router = APIRouter(prefix="/public", tags=["Public"])

_DEFAULTS = {
    "platform_name": "Mpola Uganda",
    "support_email": "support@mpola.ug",
}


@router.get("/platform-info")
def get_platform_info(db: Session = Depends(get_db)):
    """Just the handful of platform-wide settings that are meant to be
    shown to visitors — not the full admin settings payload (loan bounds,
    fee rates, etc. stay admin-only). `licence_number` is admin-entered
    free text (see Admin Settings > General) — omitted entirely (not a
    placeholder) until an admin actually sets it, so the public site never
    displays an unverified regulatory claim."""
    rows = db.query(PlatformSetting).filter(
        PlatformSetting.key.in_(["platform_name", "support_email", "licence_number"])
    ).all()
    values = {r.key: r.value for r in rows}

    return {
        "platform_name": values.get("platform_name") or _DEFAULTS["platform_name"],
        "support_email": values.get("support_email") or _DEFAULTS["support_email"],
        "licence_number": values.get("licence_number") or None,
    }
