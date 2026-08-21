"""
Public, unauthenticated endpoints — safe to call from marketing pages
(website footer, About sections) without a logged-in session.
"""

import json
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database.tables import PlatformSetting, LenderOfferTemplate, LoanApplication, User
from repository.dependencies import get_db

# How many of each side the public landing page actually shows in its
# browsable feed — deliberately more than a tiny "preview" so the tab/filter
# UI has something real to filter over, but still far short of the full
# live marketplace (which stays behind login, per the actual business
# rule: real applications/offers only happen inside a dashboard).
PREVIEW_LIMIT = 12

router = APIRouter(prefix="/public", tags=["Public"])


def _first_name_last_initial(full_name: str | None, username: str) -> str:
    """Borrower requests show reduced identity on the public landing page —
    a borrower applying for a loan never consented to their full name being
    published to anonymous visitors, unlike a lender who is actively
    advertising their own offer. "Agnes Kyomuhendo" -> "Agnes K."; a single-
    word name or missing name just shows as-is / falls back to username."""
    name = (full_name or username or "").strip()
    parts = name.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[-1][0]}."
    return name or "A Mpola user"

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


@router.get("/marketplace-preview")
def get_marketplace_preview(db: Session = Depends(get_db)):
    """Powers the public landing page's browsable feed — real, live lender
    offers and borrower loan requests as a trust signal ("we're not
    thieves, real people are actually using this"), NOT the real
    marketplace itself. The real application/offer/accept flow only exists
    inside a logged-in dashboard; PREVIEW_LIMIT of each is returned, newest
    first, alongside real totals/category counts computed over every
    qualifying row (not just the ones actually returned) so headline
    numbers on the page are never inflated beyond what's shown.
    """
    now = datetime.now(timezone.utc)

    all_offers_q = (
        db.query(LenderOfferTemplate, User)
        .join(User, LenderOfferTemplate.lender_id == User.id)
        .filter(
            LenderOfferTemplate.status == "approved",
            LenderOfferTemplate.is_frozen == False,  # noqa: E712
            or_(LenderOfferTemplate.valid_until.is_(None), LenderOfferTemplate.valid_until > now),
        )
        .order_by(LenderOfferTemplate.created_at.desc())
        .all()
    )
    all_requests_q = (
        db.query(LoanApplication, User)
        .join(User, LoanApplication.borrower_id == User.id)
        .filter(
            LoanApplication.status.in_(["pending", "awaiting_guarantors"]),
            LoanApplication.is_frozen == False,  # noqa: E712
        )
        .order_by(LoanApplication.created_at.desc())
        .all()
    )

    # Category counts combine both sides — a lender's template can accept
    # several loan types at once (accepted_loan_types is a JSON list), a
    # borrower's request is always exactly one, so the offer side is
    # unnested before counting.
    category_counts: Counter = Counter()
    for template, _lender in all_offers_q:
        try:
            types = json.loads(template.accepted_loan_types or "[]")
        except (TypeError, ValueError):
            types = []
        category_counts.update(t.lower() for t in types if isinstance(t, str))
    for application, _borrower in all_requests_q:
        if application.loan_type:
            category_counts[application.loan_type.lower()] += 1

    offers = [
        {
            "id": template.id,
            "lender_name": lender.full_name or lender.username,
            "city": lender.city,
            "min_amount": template.min_amount,
            "max_amount": template.max_amount,
            "interest_rate": template.interest_rate,
            "loan_types": template.accepted_loan_types,  # JSON string, parsed client-side
            "created_at": template.created_at.isoformat() if template.created_at else None,
        }
        for template, lender in all_offers_q[:PREVIEW_LIMIT]
    ]
    requests_ = [
        {
            "id": application.id,
            "borrower_name": _first_name_last_initial(borrower.full_name, borrower.username),
            "city": borrower.city,
            "amount": application.amount,
            "loan_type": application.loan_type,
            "duration": application.duration,
            "duration_days": application.duration_days,
            "created_at": application.created_at.isoformat() if application.created_at else None,
        }
        for application, borrower in all_requests_q[:PREVIEW_LIMIT]
    ]

    return {
        "offers": offers,
        "requests": requests_,
        "total_offers": len(all_offers_q),
        "total_requests": len(all_requests_q),
        "category_counts": dict(category_counts),
    }
