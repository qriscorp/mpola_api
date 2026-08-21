"""
Public, unauthenticated endpoints — safe to call from marketing pages
(website footer, About sections) without a logged-in session.
"""

import json
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database.tables import PlatformSetting, LenderOfferTemplate, LoanApplication, LoanOffer, User
from repository.dependencies import get_db

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


# ── Marketplace preview: real search/filter over the FULL live dataset ──

RATE_BANDS = {
    "under5": lambda r: r < 5,
    "5to7": lambda r: 5 <= r <= 7,
    "7to10": lambda r: 7 < r <= 10,
    "above10": lambda r: r > 10,
}
DURATION_BANDS = {
    "1-3": (1, 3),
    "4-6": (4, 6),
    "7-12": (7, 12),
    "12+": (13, None),
}


def _offer_loan_types(template: LenderOfferTemplate) -> list[str]:
    try:
        types = json.loads(template.accepted_loan_types or "[]")
    except (TypeError, ValueError):
        types = []
    return [t.lower() for t in types if isinstance(t, str)]


def _offer_duration_bands(template: LenderOfferTemplate) -> set[str]:
    if template.max_duration_days is not None:
        return {"1-3"}
    if template.max_duration is None:
        return set()
    bands = set()
    for key, (lo, _hi) in DURATION_BANDS.items():
        if template.max_duration >= lo:
            bands.add(key)
    return bands


def _request_duration_band(application: LoanApplication) -> str | None:
    if application.duration_days is not None:
        return None
    if application.duration is None:
        return None
    for key, (lo, hi) in DURATION_BANDS.items():
        if application.duration >= lo and (hi is None or application.duration <= hi):
            return key
    return None


@router.get("/marketplace-preview")
def get_marketplace_preview(
    search: str = Query(None),
    listing_type: str = Query(None, description="all | offers | requests"),
    rate: str = Query(None, description="comma-separated: under5,5to7,7to10,above10"),
    loan_type: str = Query(None, description="comma-separated loan type keys"),
    duration: str = Query(None, description="comma-separated: 1-3,4-6,7-12,12+"),
    city: str = Query(None, description="comma-separated city names"),
    offset: int = Query(0, ge=0),
    limit: int = Query(6, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Powers the public landing page's browsable feed — real, live lender
    offers and borrower loan requests as a trust signal ("we're not
    thieves, real people are actually using this"), NOT the real
    marketplace itself (that stays behind login). Search/filters are
    applied against the FULL qualifying dataset, not just a recent window —
    a listing doesn't become unsearchable just because 30 newer ones exist.
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
        .all()
    )
    all_requests_q = (
        db.query(LoanApplication, User)
        .join(User, LoanApplication.borrower_id == User.id)
        .filter(
            LoanApplication.status.in_(["pending", "awaiting_guarantors"]),
            LoanApplication.is_frozen == False,  # noqa: E712
        )
        .all()
    )

    # Category counts and totals are always computed over the FULL
    # unfiltered dataset — these back the tab labels and filter checkbox
    # counts, which must reflect true totals regardless of what's currently
    # being searched/filtered for.
    category_counts: Counter = Counter()
    for template, _lender in all_offers_q:
        category_counts.update(_offer_loan_types(template))
    for application, _borrower in all_requests_q:
        if application.loan_type:
            category_counts[application.loan_type.lower()] += 1

    offer_counts = dict(
        db.query(LoanOffer.template_id, func.count(LoanOffer.id))
        .filter(LoanOffer.template_id.isnot(None))
        .group_by(LoanOffer.template_id)
        .all()
    )
    application_counts = dict(
        db.query(LoanOffer.application_id, func.count(LoanOffer.id))
        .group_by(LoanOffer.application_id)
        .all()
    )

    # ── Parse filter params ──
    search_lower = (search or "").strip().lower()
    listing_type = (listing_type or "all").strip().lower()
    rate_bands = {b for b in (rate or "").split(",") if b in RATE_BANDS}
    loan_types_wanted = {t.strip().lower() for t in (loan_type or "").split(",") if t.strip()}
    duration_bands = {b for b in (duration or "").split(",") if b in DURATION_BANDS}
    cities_wanted = {c.strip().lower() for c in (city or "").split(",") if c.strip()}

    def offer_matches(template: LenderOfferTemplate, lender: User) -> bool:
        if listing_type == "requests":
            return False
        if search_lower:
            haystack = " ".join([
                template.description or "",
                lender.full_name or lender.username or "",
                " ".join(_offer_loan_types(template)),
            ]).lower()
            if search_lower not in haystack:
                return False
        if rate_bands and not any(RATE_BANDS[b](template.interest_rate) for b in rate_bands):
            return False
        if loan_types_wanted and not (loan_types_wanted & set(_offer_loan_types(template))):
            return False
        if duration_bands and not (duration_bands & _offer_duration_bands(template)):
            return False
        if cities_wanted and (not lender.city or lender.city.lower() not in cities_wanted):
            return False
        return True

    def request_matches(application: LoanApplication, borrower: User) -> bool:
        if listing_type == "offers":
            return False
        if search_lower:
            haystack = " ".join([
                application.purpose or "",
                borrower.full_name or borrower.username or "",
                application.loan_type or "",
            ]).lower()
            if search_lower not in haystack:
                return False
        if rate_bands:
            return False  # rate is meaningless for a request — an active rate filter excludes all of them
        if loan_types_wanted and application.loan_type and application.loan_type.lower() not in loan_types_wanted:
            return False
        if duration_bands:
            band = _request_duration_band(application)
            if band is None or band not in duration_bands:
                return False
        if cities_wanted and (not borrower.city or borrower.city.lower() not in cities_wanted):
            return False
        return True

    matched_offers = [(t, l) for t, l in all_offers_q if offer_matches(t, l)]
    matched_requests = [(a, b) for a, b in all_requests_q if request_matches(a, b)]

    # Merge into one feed, newest first, then paginate the combined list —
    # matches how the UI actually presents them (interleaved, not two
    # separate lists), so "offset/limit" means what the frontend expects.
    combined = (
        [("offer", t, l, t.created_at) for t, l in matched_offers]
        + [("request", a, b, a.created_at) for a, b in matched_requests]
    )
    combined.sort(key=lambda row: row[3] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    total_matching = len(combined)
    page = combined[offset:offset + limit]

    listings = []
    for kind, obj, person, created_at in page:
        if kind == "offer":
            listings.append({
                "kind": "offer",
                "id": obj.id,
                "lender_name": person.full_name or person.username,
                "city": person.city,
                "description": obj.description,
                "min_amount": obj.min_amount,
                "max_amount": obj.max_amount,
                "interest_rate": obj.interest_rate,
                "loan_types": obj.accepted_loan_types,
                "max_duration": obj.max_duration,
                "max_duration_days": obj.max_duration_days,
                "offer_count": offer_counts.get(obj.id, 0),
                "created_at": created_at.isoformat() if created_at else None,
            })
        else:
            listings.append({
                "kind": "request",
                "id": obj.id,
                "borrower_name": _first_name_last_initial(person.full_name, person.username),
                "city": person.city,
                "purpose": obj.purpose,
                "credit_score": person.credit_score,
                "amount": obj.amount,
                "loan_type": obj.loan_type,
                "duration": obj.duration,
                "duration_days": obj.duration_days,
                "offer_count": application_counts.get(obj.id, 0),
                "created_at": created_at.isoformat() if created_at else None,
            })

    return {
        "listings": listings,
        "total_matching": total_matching,
        "has_more": offset + limit < total_matching,
        "total_offers": len(all_offers_q),
        "total_requests": len(all_requests_q),
        "category_counts": dict(category_counts),
    }
