"""
Loans router — applications, offers, active loans, repayments.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import BASE_URL
from database.tables import User, LoanApplication, LoanOffer, LenderOfferTemplate, Loan, Repayment, Guarantor, KYCDocument, BorrowerDocument, CustomDocumentResponse, Wallet, WalletTransaction, PlatformFeeTransaction, LenderApplicationSkip
from helpers import generateReferenceNumber, generateUniqueId, DOCUMENT_LABEL_MAP, safe_isoformat
from repository.auth_repo import _audit, _notify, _notify_admins
from repository.dependencies import get_db, current_active_user
from repository.models import LoanApplicationCreate, LoanApplicationUpdate, LoanOfferCreate, LoanOfferUpdate, LenderOfferTemplateCreate, LenderOfferTemplateUpdate, LenderOfferTemplateExpiryUpdate, RepaymentCreate, GuarantorAttach
from repository.security import require_roles
from routers.users import ALLOWED_BORROWER_DOCUMENT_EXTENSIONS, MAX_BORROWER_DOCUMENT_SIZE_BYTES
from utils.upg_client import UPGClient, _detect_carrier
from utils.fee import calc_platform_fee, calc_late_fee_platform_cut

# Also the exact count GuarantorAttach requires — auto-matching against lender
# standing offers only starts once every attached guarantor has accepted
# (see the respond endpoint in routers/guarantors.py); this check at
# offer-accept time is now a defensive second gate, not the primary one.
REQUIRED_ACCEPTED_GUARANTORS = 2

router = APIRouter(prefix="/loans", tags=["Loans"])


def _platform_setting(db: Session, key: str, default: float) -> float:
    """Admin-configurable platform setting, falling back to a default when unset."""
    from database.tables import PlatformSetting
    setting = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
    if not setting:
        return default
    try:
        return float(setting.value)
    except (TypeError, ValueError):
        return default


def _loan_amount_bounds(db: Session) -> tuple[float, float]:
    return (
        _platform_setting(db, "min_loan_amount", 1000),
        _platform_setting(db, "max_loan_amount", 50000000),
    )


def _max_interest_rate(db: Session) -> float:
    """Admin-configurable ceiling on a lender's interest_rate, expressed as
    %/month (see Admin Settings > Max Interest Rate)."""
    return _platform_setting(db, "max_interest_rate", 10)


def _calc_interest(amount: float, rate: float, duration: int | None, duration_days: int | None) -> float:
    """Simple interest, rate is %/month. A standard loan multiplies by the
    month count directly; a short-term "emergency" loan (duration_days set)
    prorates the same monthly rate down to the actual number of days —
    e.g. a 10%/month rate charges ~2.3% on a 7-day loan, not a full 10%."""
    if duration_days is not None:
        return amount * (rate / 100) * (duration_days / 30)
    return amount * (rate / 100) * duration


def _duration_label(duration: int | None, duration_days: int | None) -> str:
    if duration_days is not None:
        return f"{duration_days} day{'s' if duration_days != 1 else ''}"
    return f"{duration} month{'s' if duration != 1 else ''}"


# ═══════════════════════════════════════════════
#  LOAN APPLICATIONS (Borrower)
# ═══════════════════════════════════════════════

@router.post("/applications")
async def create_application(
    data: LoanApplicationCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Create a new loan application. Borrowers only."""
    min_amount, max_amount = _loan_amount_bounds(db)
    if data.amount < min_amount or data.amount > max_amount:
        raise HTTPException(
            status_code=400,
            detail=f"Amount must be between {min_amount:,.0f} and {max_amount:,.0f}",
        )
    # Normalize to naive UTC regardless of whether the client sent an
    # offset/'Z'-suffixed (aware) or plain (naive) value — MySQL DATETIME
    # has no tz concept, and every other valid_until-style field in this
    # codebase (LenderOfferTemplate.valid_until) is stored/compared the
    # same naive-UTC way, so this keeps the convention consistent and
    # avoids a naive/aware TypeError on the comparison below.
    valid_until = data.valid_until.replace(tzinfo=None) if data.valid_until else None
    if valid_until and valid_until <= datetime.now(timezone.utc).replace(tzinfo=None):
        raise HTTPException(status_code=400, detail="Valid-until date must be in the future")

    # Calculate estimated payment (simple interest, rate is % per month)
    rate = 3.0  # default platform rate — a display estimate only; real lender offers set their own
    total_interest = _calc_interest(data.amount, rate, data.duration, data.duration_days)
    total_repayable = data.amount + total_interest
    # For an emergency (duration_days) loan this is the single lump-sum
    # repayment, not a monthly instalment — same field, different meaning,
    # see Loan.monthly_payment.
    monthly_payment = total_repayable if data.duration_days is not None else total_repayable / data.duration

    app = LoanApplication(
        borrower_id=user.id,
        reference_number=generateReferenceNumber(),
        amount=data.amount,
        duration=data.duration,
        duration_days=data.duration_days,
        loan_type=data.loan_type,
        purpose=data.purpose,
        interest_rate=rate,
        total_repayable=round(total_repayable, 2),
        monthly_payment=round(monthly_payment, 2),
        max_interest_rate=data.max_interest_rate,
        valid_until=valid_until,
        # Not matching-eligible yet — the apply wizard saves progress from
        # this point on (see GET /applications/draft), attaching 2
        # guarantors only once the borrower reaches the end of the wizard
        # (POST .../guarantors below) — that's the real "submission" moment,
        # not this one, so admins are notified there instead. Notifying here
        # would fire for every wizard session that merely reaches step 1,
        # including ones the borrower never finishes.
        status="awaiting_guarantors",
    )
    db.add(app)
    db.commit()
    db.refresh(app)

    return {
        "status": 200,
        "message": "Loan application submitted",
        "application": _app_response(app),
    }


@router.get("/applications")
async def list_my_applications(
    status: str = Query(None),
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """List current user's loan applications."""
    query = db.query(LoanApplication).filter(LoanApplication.borrower_id == user.id)
    if status:
        query = query.filter(LoanApplication.status == status)
    total = query.count()
    apps = query.order_by(LoanApplication.created_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "applications": [_app_response(a) for a in apps]}


@router.get("/applications/draft")
async def get_draft_application(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """The apply wizard saves progress as real data starting from step 1 —
    an application that's been created but hasn't had its guarantors
    attached yet is, by definition, an unfinished draft (once guarantors
    are attached, it's a real submitted request waiting on them, not a
    draft — see attach_guarantors). Lets the wizard resume exactly where a
    borrower left off after navigating away or closing the app. Registered
    before /applications/{app_id} so "draft" isn't swallowed as a path
    param (same reasoning as /users/search-guarantor-candidate)."""
    candidates = (
        db.query(LoanApplication)
        .filter(
            LoanApplication.borrower_id == user.id,
            LoanApplication.status == "awaiting_guarantors",
        )
        .order_by(LoanApplication.created_at.desc())
        .all()
    )
    draft = next((a for a in candidates if not a.guarantors), None)
    if not draft:
        return {"draft": None}

    return {"draft": _app_response(draft)}


@router.get("/applications/{app_id}")
async def get_application(
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    app = db.query(LoanApplication).filter(LoanApplication.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    # Borrower can see own, lenders can see any, admin can see all
    if user.has_admin_access or user.role == "lender" or app.borrower_id == user.id:
        return _app_response(app, db, include_offers=True)
    raise HTTPException(status_code=403, detail="Not authorized")


@router.post("/applications/{app_id}/guarantors")
async def attach_guarantors(
    app_id: str,
    data: GuarantorAttach,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Attach the 2 required guarantors to a freshly-created application —
    called immediately after POST /loans/applications by the same submit
    flow. Each must be a real Mpola account (confirmed client-side via
    GET /users/search-guarantor-candidate before this call). Fires a
    real-time notification to each; the application stays
    'awaiting_guarantors' — and is NOT matched to any lender — until both
    accept (see PUT /guarantors/{id}/respond in routers/guarantors.py)."""
    app = db.query(LoanApplication).filter(
        LoanApplication.id == app_id, LoanApplication.borrower_id == user.id
    ).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    if app.status != "awaiting_guarantors":
        raise HTTPException(status_code=400, detail="This application already has its guarantors attached")

    guarantor_ids = data.guarantor_user_ids
    if len(set(guarantor_ids)) != len(guarantor_ids):
        raise HTTPException(status_code=400, detail="Guarantors must be two different people")
    if user.id in guarantor_ids:
        raise HTTPException(status_code=400, detail="You can't be your own guarantor")

    candidates = db.query(User).filter(User.id.in_(guarantor_ids)).all()
    if len(candidates) != len(guarantor_ids):
        raise HTTPException(status_code=404, detail="One of those accounts no longer exists")

    borrower_name = user.full_name or user.username
    for guarantor_user_id in guarantor_ids:
        g = Guarantor(application_id=app_id, guarantor_user_id=guarantor_user_id)
        db.add(g)
        _notify(
            db, guarantor_user_id,
            title="Guarantor request",
            message=(
                f"{borrower_name} added you as a guarantor on their UGX {app.amount:,.0f} "
                f"loan request. Approve to help them get matched, or decline if you're not interested."
            ),
            type="guarantor_invite_received",
            data={"application_id": app.id},
        )

    _audit(db, "guarantors_attached", username=user.username, user_id=user.id,
           resource_type="loan_application", resource_id=app_id,
           details={"guarantor_user_ids": guarantor_ids})

    # This is the real "submission" moment under the save-as-you-go wizard —
    # the borrower has finished all 4 steps and their guarantors are now
    # being asked to approve, not just idly created a draft in step 1.
    _notify_admins(
        db,
        title="New loan application",
        message=f"{borrower_name} applied for a {app.loan_type} loan of UGX {app.amount:,.0f}.",
        type="new_application",
        data={"application_id": app.id},
        setting_key="notif_new_applications",
    )

    db.commit()

    return {"status": 200, "message": "Guarantors added — waiting for them to accept"}


# ═══════════════════════════════════════════════
#  APPLICATION LIFECYCLE (Borrower) — edit, withdraw, pause
# ═══════════════════════════════════════════════
#  Mirrors the LenderOfferTemplate lifecycle below (edit/delete/freeze/
#  unfreeze), scoped to "not yet matched into a funded loan" instead of
#  "pending admin review" — applications don't need admin approval before
#  going live, so the whole awaiting_guarantors/pending window is editable.

def _get_own_application(db: Session, app_id: str, user: User) -> LoanApplication:
    app = db.query(LoanApplication).filter(LoanApplication.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    if app.borrower_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    return app


def _cancel_pending_offers(db: Session, app: LoanApplication, reason: str) -> None:
    """Declines every still-pending LoanOffer on this application and tells
    the lender why — used whenever the application changes in a way that
    invalidates offers already extended against its old terms (an edit that
    changes amount/duration/loan_type) or the application goes away entirely
    (withdrawal)."""
    pending_offers = [o for o in app.offers if o.status == "pending"]
    for offer in pending_offers:
        offer.status = "declined"
        _notify(
            db, offer.lender_id,
            title="Offer no longer available",
            message=f"{reason} Your offer on this request has been withdrawn.",
            type="offer_declined",
            data={"application_id": app.id},
        )


@router.put("/applications/{app_id}")
async def update_application(
    app_id: str,
    data: LoanApplicationUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Borrower edits their own request — only while it hasn't been matched
    into a funded loan yet (awaiting_guarantors or pending), AND only while
    no guarantor has accepted yet. A guarantor's acceptance is given for a
    specific amount/duration/loan_type — once someone has actually committed
    to guarantee this loan, its terms are locked; the borrower can still
    freeze or withdraw it, but not quietly change what was agreed to. (Note:
    status=="pending" always implies every guarantor already accepted, since
    that's what flips it — the two checks below together cover both "some
    have accepted while others are still deciding" and "all have accepted".)
    """
    app = _get_own_application(db, app_id, user)
    if app.status not in ("awaiting_guarantors", "pending"):
        raise HTTPException(status_code=400, detail="Only requests that haven't been matched yet can be edited")
    if any(g.status == "accepted" for g in app.guarantors):
        raise HTTPException(
            status_code=400,
            detail="This request can no longer be edited — a guarantor has already approved it. You can still freeze or withdraw it.",
        )

    update_dict = data.model_dump(exclude_unset=True)
    if not update_dict:
        raise HTTPException(status_code=400, detail="Nothing to update")

    if "amount" in update_dict:
        min_amount, max_amount = _loan_amount_bounds(db)
        if update_dict["amount"] < min_amount or update_dict["amount"] > max_amount:
            raise HTTPException(status_code=400, detail=f"Amount must be between {min_amount:,.0f} and {max_amount:,.0f}")

    if "valid_until" in update_dict and update_dict["valid_until"] is not None:
        valid_until = update_dict["valid_until"].replace(tzinfo=None)
        if valid_until <= datetime.now(timezone.utc).replace(tzinfo=None):
            raise HTTPException(status_code=400, detail="Valid-until date must be in the future")
        update_dict["valid_until"] = valid_until

    terms_changed = any(
        k in update_dict and update_dict[k] != getattr(app, k)
        for k in ("amount", "duration", "duration_days", "loan_type")
    )

    for key, val in update_dict.items():
        setattr(app, key, val)

    if terms_changed:
        rate = app.interest_rate or 3.0
        total_interest = _calc_interest(app.amount, rate, app.duration, app.duration_days)
        total_repayable = app.amount + total_interest
        app.total_repayable = round(total_repayable, 2)
        app.monthly_payment = round(
            total_repayable if app.duration_days is not None else total_repayable / app.duration, 2
        )

        # The guard above already ruled out any "accepted" guarantor, so
        # everyone left is "pending" (or "declined", who's moot either way)
        # — nothing to reset, just keep their still-open invite honest about
        # what they'd actually be guaranteeing if they accept now.
        for g in app.guarantors:
            if g.status == "pending":
                _notify(
                    db, g.guarantor_user_id,
                    title="Loan request updated",
                    message=(
                        f"{user.full_name or user.username} updated the loan request you're being asked "
                        f"to guarantee — it's now UGX {app.amount:,.0f} for {_duration_label(app.duration, app.duration_days)}."
                    ),
                    type="guarantor_invite_received",
                    data={"application_id": app.id},
                )

    _audit(db, "application_updated", username=user.username, user_id=user.id,
           resource_type="loan_application", resource_id=app.id,
           details={"fields": list(update_dict.keys()), "terms_changed": terms_changed})
    db.commit()
    db.refresh(app)
    return {"status": 200, "message": "Updated", "application": _app_response(app)}


@router.delete("/applications/{app_id}")
async def delete_application(
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Borrower withdraws their own request — only while it hasn't been
    matched into a funded loan yet. Notifies any guarantor who was pending
    or had already accepted, and any lender with a still-pending offer, that
    it's gone."""
    app = _get_own_application(db, app_id, user)
    if app.status not in ("awaiting_guarantors", "pending"):
        raise HTTPException(status_code=400, detail="Only requests that haven't been matched yet can be withdrawn")

    for g in app.guarantors:
        if g.status in ("pending", "accepted"):
            _notify(
                db, g.guarantor_user_id,
                title="Loan request withdrawn",
                message="The loan request you were guaranteeing was withdrawn by the borrower — no action needed.",
                type="guarantor_request_expired",
                data={"application_id": app.id},
            )
    _cancel_pending_offers(db, app, "The borrower withdrew this loan request.")

    _audit(db, "application_withdrawn", username=user.username, user_id=user.id,
           resource_type="loan_application", resource_id=app.id)
    db.delete(app)
    db.commit()
    return {"status": 200, "message": "Withdrawn"}


@router.post("/applications/{app_id}/freeze")
async def freeze_own_application(
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Borrower pauses their own request — it stops being matched to new
    lender offers, but stays open (not withdrawn) so it can be unfrozen
    later. Existing guarantor invites and any pending offers are untouched."""
    app = _get_own_application(db, app_id, user)
    if app.status not in ("awaiting_guarantors", "pending"):
        raise HTTPException(status_code=400, detail="Only requests that haven't been matched yet can be frozen")
    if app.is_frozen:
        raise HTTPException(status_code=400, detail="Already frozen")

    app.is_frozen = True
    app.frozen_by = "borrower"
    _audit(db, "application_frozen_by_borrower", username=user.username, user_id=user.id,
           resource_type="loan_application", resource_id=app.id)
    db.commit()
    db.refresh(app)
    return {"status": 200, "message": "Frozen", "application": _app_response(app)}


@router.post("/applications/{app_id}/unfreeze")
async def unfreeze_own_application(
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Borrower un-pauses their own request — blocked if an admin was the
    one who froze it (only admin can undo that)."""
    app = _get_own_application(db, app_id, user)
    if not app.is_frozen:
        raise HTTPException(status_code=400, detail="Not frozen")
    if app.frozen_by == "admin":
        raise HTTPException(status_code=403, detail="This request was frozen by an admin and can only be unfrozen by them")

    app.is_frozen = False
    app.frozen_by = None
    _audit(db, "application_unfrozen_by_borrower", username=user.username, user_id=user.id,
           resource_type="loan_application", resource_id=app.id)
    db.commit()
    db.refresh(app)
    return {"status": 200, "message": "Unfrozen", "application": _app_response(app)}


# ═══════════════════════════════════════════════
#  LOAN MARKETPLACE (Lender)
# ═══════════════════════════════════════════════

@router.get("/marketplace")
async def browse_marketplace(
    loan_type: str = Query(None),
    min_amount: float = Query(None),
    max_amount: float = Query(None),
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Browse available loan applications (for lenders)."""
    skipped_ids = db.query(LenderApplicationSkip.application_id).filter(
        LenderApplicationSkip.lender_id == user.id
    )
    query = db.query(LoanApplication).filter(
        LoanApplication.status == "pending",
        LoanApplication.is_frozen == False,  # noqa: E712 — SQLAlchemy needs `== False`, not `is False`
        ~LoanApplication.id.in_(skipped_ids),
    )
    if loan_type:
        query = query.filter(LoanApplication.loan_type == loan_type)
    if min_amount:
        query = query.filter(LoanApplication.amount >= min_amount)
    if max_amount:
        query = query.filter(LoanApplication.amount <= max_amount)

    total = query.count()
    apps = query.order_by(LoanApplication.created_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "applications": [_app_response(a) for a in apps]}


@router.post("/marketplace/{application_id}/skip")
async def skip_marketplace_application(
    application_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Lender declines to offer on this application — hides it from their
    own marketplace/applications view only. Every other lender still sees
    it, and the application's status is untouched."""
    app = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    existing = db.query(LenderApplicationSkip).filter(
        LenderApplicationSkip.lender_id == user.id,
        LenderApplicationSkip.application_id == application_id,
    ).first()
    if not existing:
        db.add(LenderApplicationSkip(lender_id=user.id, application_id=application_id))
        db.commit()

    return {"status": 200, "message": "Application hidden from your marketplace view"}


# ═══════════════════════════════════════════════
#  LOAN OFFERS (Lender -> Borrower)
# ═══════════════════════════════════════════════

@router.post("/offers")
async def make_offer(
    data: LoanOfferCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Lender makes an offer on a loan application."""
    app = db.query(LoanApplication).filter(LoanApplication.id == data.application_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    if app.status != "pending":
        raise HTTPException(status_code=400, detail="Application no longer accepting offers")
    if app.borrower_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot make an offer on your own application")

    max_rate = _max_interest_rate(db)
    if data.interest_rate > max_rate:
        raise HTTPException(status_code=400, detail=f"Interest rate cannot exceed {max_rate}%/month")
    if app.max_interest_rate is not None and data.interest_rate > app.max_interest_rate:
        raise HTTPException(status_code=400, detail=f"Borrower capped this request at {app.max_interest_rate}%/month")

    total_interest = _calc_interest(data.amount, data.interest_rate, data.duration, data.duration_days)
    total_repayable = data.amount + total_interest
    monthly_payment = total_repayable if data.duration_days is not None else total_repayable / data.duration

    offer = LoanOffer(
        application_id=data.application_id,
        lender_id=user.id,
        amount=data.amount,
        interest_rate=data.interest_rate,
        duration=data.duration,
        duration_days=data.duration_days,
        total_repayable=round(total_repayable, 2),
        monthly_payment=round(monthly_payment, 2),
        required_documents=json.dumps(data.required_documents) if data.required_documents else None,
    )
    db.add(offer)
    _notify(
        db, app.borrower_id,
        title="New offer received",
        message=(
            f"{user.full_name or user.username} offered UGX {data.amount:,.0f} "
            f"at {data.interest_rate}%/month for {_duration_label(data.duration, data.duration_days)} on your loan request."
        ),
        type="loan_offer",
        data={"application_id": app.id},
    )
    db.commit()
    db.refresh(offer)

    return {"status": 200, "message": "Offer submitted", "offer": _offer_response(offer, db)}


@router.get("/offers/mine")
async def my_offers(
    status: str = Query(None),
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """List offers made by the current lender."""
    query = db.query(LoanOffer).filter(LoanOffer.lender_id == user.id)
    if status:
        query = query.filter(LoanOffer.status == status)
    total = query.count()
    offers = query.order_by(LoanOffer.created_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "offers": [_offer_response(o, db) for o in offers]}


@router.get("/offers/received")
async def offers_received(
    status: str = Query(None),
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """List offers received across all of the current borrower's applications."""
    query = (
        db.query(LoanOffer)
        .join(LoanApplication, LoanOffer.application_id == LoanApplication.id)
        .filter(LoanApplication.borrower_id == user.id)
    )
    if status:
        query = query.filter(LoanOffer.status == status)
    total = query.count()
    offers = query.order_by(LoanOffer.created_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "offers": [_offer_response(o, db) for o in offers]}


def _required_documents_status(
    db: Session, borrower_id: str, required_documents, application_id: str = None,
) -> list[dict]:
    """Resolves an offer's required_documents (JSON list of labels, e.g.
    "National ID", "Bank Statement (3mo)") against what the borrower
    actually has on file — KYCDocument for identity labels, BorrowerDocument
    (account-wide, reusable — see database/tables.py) for everything else,
    CustomDocumentResponse (per-application) for a lender's free-typed
    "Other: ..." requirement. See DOCUMENT_LABEL_MAP (helpers.py). Includes
    the file itself (not just a satisfied flag) so a lender reviewing before
    disbursement can actually open what was provided, not just see a
    checkmark."""
    labels = json.loads(required_documents) if required_documents else []
    if not labels:
        return []

    kyc_docs = {
        d.document_type: d for d in db.query(KYCDocument).filter(KYCDocument.user_id == borrower_id).all()
    }
    borrower_docs = {
        d.document_type: d for d in db.query(BorrowerDocument).filter(BorrowerDocument.user_id == borrower_id).all()
    }
    custom_responses = {}
    if application_id:
        custom_responses = {
            r.label: r
            for r in db.query(CustomDocumentResponse)
            .filter(CustomDocumentResponse.application_id == application_id)
            .all()
        }

    result = []
    for label in labels:
        source, type_key = DOCUMENT_LABEL_MAP.get(label, (None, None))
        if source not in ("kyc", "borrower_doc"):
            # A lender-specified custom requirement — not a fixed document
            # type, so it's fulfilled by either an uploaded file or a
            # free-text explanation (at least one), tracked per-application.
            custom = custom_responses.get(label)
            result.append({
                "label": label,
                "type": None,
                "source": "custom",
                "satisfied": bool(custom and (custom.text_response or custom.file_url)),
                "file_url": custom.file_url if custom else None,
                "file_name": custom.file_name if custom else None,
                "verified": False,
                "text_response": custom.text_response if custom else None,
            })
            continue

        doc = kyc_docs.get(type_key) if source == "kyc" else borrower_docs.get(type_key)
        satisfied = doc is not None
        result.append({
            "label": label,
            "type": type_key,
            "source": source,
            "satisfied": satisfied,
            "file_url": doc.file_url if doc else None,
            "file_name": doc.file_name if doc else None,
            "verified": doc.verified if doc else False,
            "text_response": None,
        })
    return result


@router.post("/applications/{app_id}/custom-document-response")
async def submit_custom_document_response(
    app_id: str,
    label: str = Form(...),
    text_response: str = Form(None),
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Borrower fulfils a lender's custom ("Other: ...") document requirement
    — either a file, a text explanation, or both. Upserts on (application,
    label) so re-submitting (e.g. replacing a wrong file) overwrites rather
    than piling up duplicates, same convention as KYC/BorrowerDocument."""
    app = db.query(LoanApplication).filter(
        LoanApplication.id == app_id, LoanApplication.borrower_id == user.id,
    ).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    if not text_response and not file:
        raise HTTPException(status_code=400, detail="Provide a file, a text response, or both")

    existing = db.query(CustomDocumentResponse).filter(
        CustomDocumentResponse.application_id == app_id,
        CustomDocumentResponse.label == label,
    ).first()

    file_url = existing.file_url if existing else None
    file_name = existing.file_name if existing else None
    if file:
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in ALLOWED_BORROWER_DOCUMENT_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext or 'unknown'}")
        contents = await file.read()
        if len(contents) > MAX_BORROWER_DOCUMENT_SIZE_BYTES:
            raise HTTPException(status_code=400, detail="File exceeds 10MB limit")
        os.makedirs("uploads", exist_ok=True)
        stored_name = f"{generateUniqueId(20)}{ext}"
        with open(os.path.join("uploads", stored_name), "wb") as f:
            f.write(contents)
        file_url = f"{BASE_URL}/uploads/{stored_name}"
        file_name = file.filename

    if existing:
        if text_response is not None:
            existing.text_response = text_response
        existing.file_url = file_url
        existing.file_name = file_name
        resp = existing
    else:
        resp = CustomDocumentResponse(
            application_id=app_id, label=label,
            text_response=text_response, file_url=file_url, file_name=file_name,
        )
        db.add(resp)

    _audit(db, "custom_document_response_submitted", username=user.username, user_id=user.id,
           resource_type="loan_application", resource_id=app_id, details={"label": label})
    db.commit()
    db.refresh(resp)

    return {
        "status": 200,
        "message": "Saved",
        "response": {
            "label": resp.label,
            "text_response": resp.text_response,
            "file_url": resp.file_url,
            "file_name": resp.file_name,
        },
    }


@router.put("/offers/{offer_id}")
async def respond_to_offer(
    offer_id: str,
    data: LoanOfferUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Borrower accepts or declines an offer."""
    if data.status not in ("accepted", "declined"):
        raise HTTPException(status_code=400, detail="Status must be 'accepted' or 'declined'")

    # Locked for the rest of this request — without this, two concurrent
    # accept requests for the same offer (double-click, two tabs) could both
    # read status=="pending" before either commits, both pass every check
    # below, and both create a Loan. The second request now simply blocks
    # here until the first commits, then sees status=="accepted" and 400s.
    offer = db.query(LoanOffer).filter(LoanOffer.id == offer_id).with_for_update().first()
    if not offer:
        raise HTTPException(status_code=404, detail="Offer not found")

    app = db.query(LoanApplication).filter(LoanApplication.id == offer.application_id).with_for_update().first()
    if not app or app.borrower_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Guard against double-submit/replay — without this, accepting twice would
    # disburse via UPG twice and create two Loan records for one offer.
    if offer.status != "pending":
        raise HTTPException(status_code=400, detail=f"Offer already {offer.status}")
    if app.status != "pending":
        raise HTTPException(status_code=400, detail="This application has already been funded or is no longer open")

    if data.status == "accepted":
        accepted_guarantors = db.query(Guarantor).filter(
            Guarantor.application_id == app.id, Guarantor.status == "accepted",
        ).count()
        if accepted_guarantors < REQUIRED_ACCEPTED_GUARANTORS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"This loan needs {REQUIRED_ACCEPTED_GUARANTORS} guarantors to accept "
                    f"before it can be disbursed ({accepted_guarantors} so far)."
                ),
            )

        missing_docs = [
            d["label"] for d in _required_documents_status(db, app.borrower_id, offer.required_documents, app.id)
            if not d["satisfied"]
        ]
        if missing_docs:
            raise HTTPException(
                status_code=400,
                detail=f"Upload the documents this offer requires before accepting: {', '.join(missing_docs)}.",
            )

        # Disbursement is a separate, lender-approved step (see
        # approve_disbursement below), which re-checks the lender's own
        # wallet setup and balance there — not here. Whether the lender has
        # a wallet set up is entirely outside the borrower's control, so
        # accepting must never block on it; the loan simply waits in
        # pending_disbursement until the lender sets up their wallet and
        # approves. Only the borrower's own wallet — something they can
        # actually act on — is checked at accept time. (lender_wallet is
        # still looked up below, read-only, to warn the lender in their
        # notification if their balance looks short.)
        lender_wallet = db.query(Wallet).filter(Wallet.user_id == offer.lender_id).first()

        borrower_wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
        if not borrower_wallet or not borrower_wallet.is_wallet_setup:
            raise HTTPException(status_code=400, detail="Please set up your wallet before accepting a loan offer")

        offer.status = "accepted"
        app.status = "funded"
        app.interest_rate = offer.interest_rate
        app.total_repayable = offer.total_repayable
        app.monthly_payment = offer.monthly_payment

        # Loan starts pending_disbursement — no money moves and no
        # next_payment_date/disbursed_at until the lender approves via
        # POST /loans/active/{loan_id}/approve-disbursement.
        loan = Loan(
            application_id=app.id,
            borrower_id=app.borrower_id,
            lender_id=offer.lender_id,
            amount=offer.amount,
            interest_rate=offer.interest_rate,
            duration=offer.duration,
            duration_days=offer.duration_days,
            monthly_payment=offer.monthly_payment,
            total_repayable=offer.total_repayable,
            # An emergency (duration_days) loan is a single bullet repayment
            # — one instalment, due duration_days after disbursement — not a
            # monthly instalment count.
            total_instalments=1 if offer.duration_days is not None else offer.duration,
            status="pending_disbursement",
            required_documents=offer.required_documents,
            borrower_note=data.note,
        )
        db.add(loan)
        db.flush()

        platform_fee = calc_platform_fee(offer.amount)
        total_needed = offer.amount + platform_fee
        if not lender_wallet or not lender_wallet.is_wallet_setup:
            shortfall_note = " Set up your Mpola wallet before you can approve it."
        elif lender_wallet.balance < total_needed:
            shortfall_note = (
                f" Your wallet balance looks insufficient (need UGX {total_needed:,.0f}, "
                f"including the platform fee) — deposit before approving."
            )
        else:
            shortfall_note = ""
        _notify(
            db, offer.lender_id,
            title="Loan needs your approval to disburse",
            message=(
                f"{user.full_name or user.username} accepted your offer of UGX {offer.amount:,.0f}. "
                f"Approve disbursement to release the funds.{shortfall_note}"
            ),
            type="loan_pending_disbursement",
            data={"application_id": app.id, "loan_id": loan.id},
        )

        # Decline other pending offers, and let those lenders know
        other_offers = db.query(LoanOffer).filter(
            LoanOffer.application_id == app.id,
            LoanOffer.id != offer_id,
            LoanOffer.status == "pending",
        ).all()
        for other in other_offers:
            other.status = "declined"
            _notify(
                db, other.lender_id,
                title="Offer declined",
                message="The borrower accepted a different offer on this loan request.",
                type="offer_declined",
                data={"application_id": app.id},
            )
    else:
        offer.status = "declined"
        _notify(
            db, offer.lender_id,
            title="Offer declined",
            message=f"{user.full_name or user.username} declined your offer of UGX {offer.amount:,.0f}.",
            type="offer_declined",
            data={"application_id": app.id},
        )

    db.commit()
    return {"status": 200, "message": f"Offer {data.status}"}


def _offer_template_response(t: LenderOfferTemplate) -> dict:
    return {
        "id": t.id,
        "lender_id": t.lender_id,
        "max_amount": t.max_amount,
        "min_amount": t.min_amount,
        "interest_rate": t.interest_rate,
        "max_duration": t.max_duration,
        "max_duration_days": t.max_duration_days,
        "accepted_loan_types": json.loads(t.accepted_loan_types) if t.accepted_loan_types else [],
        "required_documents": json.loads(t.required_documents) if t.required_documents else [],
        "description": t.description,
        "valid_until": safe_isoformat(t.valid_until),
        "max_concurrent_loans": t.max_concurrent_loans,
        "status": t.status,
        "is_frozen": t.is_frozen,
        "frozen_by": t.frozen_by,
        "created_at": safe_isoformat(t.created_at),
    }


@router.post("/offer-templates")
async def create_offer_template(
    data: LenderOfferTemplateCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """A lender submits their standing lending criteria. Submissions sit as
    'pending_review' until an admin approves them — once approved, they're
    matched against every pending application (and every new one going
    forward) and auto-generate real offers. See auto_match_offers_for_*.
    """
    template = LenderOfferTemplate(
        lender_id=user.id,
        max_amount=data.max_amount,
        min_amount=data.min_amount,
        interest_rate=data.interest_rate,
        max_duration=data.max_duration,
        max_duration_days=data.max_duration_days,
        accepted_loan_types=json.dumps(data.accepted_loan_types),
        required_documents=json.dumps(data.required_documents),
        description=data.description,
        valid_until=data.valid_until,
        max_concurrent_loans=data.max_concurrent_loans,
        status="draft" if data.is_draft else "pending_review",
    )
    db.add(template)
    _audit(db, "lender_offer_template_created", username=user.username, user_id=user.id,
           resource_type="lender_offer_template", details={"status": template.status})
    db.commit()
    db.refresh(template)

    return {
        "status": 200,
        "message": "Saved as draft" if data.is_draft else "Submitted for review",
        "template": _offer_template_response(template),
    }


@router.get("/offer-templates/mine")
async def my_offer_templates(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """List the current lender's submitted offer templates."""
    templates = (
        db.query(LenderOfferTemplate)
        .filter(LenderOfferTemplate.lender_id == user.id)
        .order_by(LenderOfferTemplate.created_at.desc())
        .all()
    )
    return {"templates": [_offer_template_response(t) for t in templates]}


def _get_own_template(db: Session, template_id: str, user: User) -> LenderOfferTemplate:
    template = db.query(LenderOfferTemplate).filter(LenderOfferTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Offer template not found")
    if template.lender_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    return template


@router.put("/offer-templates/{template_id}")
async def update_offer_template(
    template_id: str,
    data: LenderOfferTemplateUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Lender edits their own standing offer — only while it's still pending
    admin review. Once approved/rejected, use freeze/unfreeze instead."""
    template = _get_own_template(db, template_id, user)
    if template.status != "pending_review":
        raise HTTPException(status_code=400, detail="Only templates pending review can be edited")

    update_dict = data.model_dump(exclude_unset=True)

    # LenderOfferTemplateUpdate's own validator only catches min/max being
    # invalid relative to EACH OTHER when both are sent together in this
    # request — it can't see the template's existing persisted value when
    # only one of the two is being changed. Merge against the current row
    # here so a partial edit can never leave a stored min >= max.
    effective_max = update_dict.get("max_amount", template.max_amount)
    effective_min = update_dict.get("min_amount", template.min_amount)
    if effective_min >= effective_max:
        raise HTTPException(
            status_code=400,
            detail=f"Min loan amount ({effective_min:,.0f}) must be less than max loan amount ({effective_max:,.0f})",
        )

    for key, val in update_dict.items():
        if key in ("accepted_loan_types", "required_documents"):
            setattr(template, key, json.dumps(val))
        else:
            setattr(template, key, val)

    _audit(db, "offer_template_updated", username=user.username, user_id=user.id,
           resource_type="lender_offer_template", resource_id=template.id)
    db.commit()
    db.refresh(template)
    return {"status": 200, "message": "Updated", "template": _offer_template_response(template)}


@router.delete("/offer-templates/{template_id}")
async def delete_offer_template(
    template_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Lender deletes their own standing offer — only while it's still
    pending admin review."""
    template = _get_own_template(db, template_id, user)
    if template.status != "pending_review":
        raise HTTPException(status_code=400, detail="Only templates pending review can be deleted")

    _audit(db, "offer_template_deleted", username=user.username, user_id=user.id,
           resource_type="lender_offer_template", resource_id=template.id)
    db.delete(template)
    db.commit()
    return {"status": 200, "message": "Deleted"}


@router.post("/offer-templates/{template_id}/freeze")
async def freeze_own_offer_template(
    template_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Lender pauses their own approved standing offer — it stops matching
    new applications, but stays approved (not deleted/rejected) so it can
    be unfrozen later."""
    template = _get_own_template(db, template_id, user)
    if template.status != "approved":
        raise HTTPException(status_code=400, detail="Only approved offers can be frozen")
    if template.is_frozen:
        raise HTTPException(status_code=400, detail="Already frozen")

    template.is_frozen = True
    template.frozen_by = "lender"
    _audit(db, "offer_template_frozen_by_lender", username=user.username, user_id=user.id,
           resource_type="lender_offer_template", resource_id=template.id)
    db.commit()
    db.refresh(template)
    return {"status": 200, "message": "Frozen", "template": _offer_template_response(template)}


@router.post("/offer-templates/{template_id}/unfreeze")
async def unfreeze_own_offer_template(
    template_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Lender un-pauses their own standing offer — blocked if an admin was
    the one who froze it (only admin can undo that)."""
    template = _get_own_template(db, template_id, user)
    if not template.is_frozen:
        raise HTTPException(status_code=400, detail="Not frozen")
    if template.frozen_by == "admin":
        raise HTTPException(status_code=403, detail="This offer was frozen by an admin and can only be unfrozen by them")

    template.is_frozen = False
    template.frozen_by = None
    _audit(db, "offer_template_unfrozen_by_lender", username=user.username, user_id=user.id,
           resource_type="lender_offer_template", resource_id=template.id)
    db.commit()
    db.refresh(template)
    return {"status": 200, "message": "Unfrozen", "template": _offer_template_response(template)}


@router.put("/offer-templates/{template_id}/expiry")
async def extend_offer_template_expiry(
    template_id: str,
    data: LenderOfferTemplateExpiryUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Lender extends (or clears) the expiry on their own APPROVED standing
    offer — the one field editable post-approval without a new admin review,
    since it can't change the loan terms themselves. Once valid_until passes,
    _template_matches permanently excludes the offer; this is the only way
    to revive it short of submitting a whole new template."""
    template = _get_own_template(db, template_id, user)
    if template.status != "approved":
        raise HTTPException(status_code=400, detail="Only approved offers can have their expiry updated here — edit the offer directly while it's pending review")

    template.valid_until = data.valid_until
    template.expiry_notified = False
    _audit(db, "offer_template_expiry_updated", username=user.username, user_id=user.id,
           resource_type="lender_offer_template", resource_id=template.id,
           details={"valid_until": safe_isoformat(data.valid_until)})
    db.commit()
    db.refresh(template)
    return {"status": 200, "message": "Expiry updated", "template": _offer_template_response(template)}


# ═══════════════════════════════════════════════
#  STANDING OFFER AUTO-MATCHING
# ═══════════════════════════════════════════════
#  An approved LenderOfferTemplate is a lender's standing lending criteria.
#  These two entry points are how it actually turns into real offers:
#    - a new application checks every approved template (see create_application)
#    - a newly-approved template checks every pending application (see
#      routers/admin.py's review_offer_template)
#  Either way, matching creates a real LoanOffer exactly as if the lender had
#  made it by hand — the borrower still has to accept it themselves.

def _template_matches(db: Session, template: LenderOfferTemplate, app: LoanApplication) -> bool:
    if template.status != "approved":
        return False
    if template.is_frozen:
        return False
    if template.lender_id == app.borrower_id:
        return False
    if app.is_frozen:
        return False
    # A standing offer is either month-based (max_duration) or a day-based
    # "emergency" offer (max_duration_days) — exactly one is ever set, same
    # split as LoanApplication/LoanOffer. Only match within the same term
    # shape; a month-based template never matches an emergency application
    # and vice versa.
    if app.duration_days is not None:
        if template.max_duration_days is None or app.duration_days > template.max_duration_days:
            return False
    else:
        if template.max_duration is None or app.duration > template.max_duration:
            return False
    # template.valid_until round-trips through MySQL as a naive datetime even
    # though it's always written as UTC — compare naive-to-naive rather than
    # against an aware `now` (mixing the two raises TypeError).
    if template.valid_until and template.valid_until < datetime.now(timezone.utc).replace(tzinfo=None):
        return False
    # Same naive-UTC comparison, same reasoning, for the borrower's own
    # optional expiry — belt-and-suspenders alongside scheduler._expire_stale_applications,
    # which flips app.status to "expired" once a day; this catches the gap
    # in between (an application can expire mid-day, well before the next run).
    if app.valid_until and app.valid_until < datetime.now(timezone.utc).replace(tzinfo=None):
        return False
    if not (template.min_amount <= app.amount <= template.max_amount):
        return False
    if app.max_interest_rate is not None and template.interest_rate > app.max_interest_rate:
        return False

    accepted_types = json.loads(template.accepted_loan_types) if template.accepted_loan_types else []
    if accepted_types and app.loan_type not in accepted_types:
        return False

    if template.max_concurrent_loans is not None:
        active_count = db.query(func.count(Loan.id)).filter(
            Loan.lender_id == template.lender_id,
            Loan.status.in_(["pending_disbursement", "active", "overdue"]),
        ).scalar()
        if active_count >= template.max_concurrent_loans:
            return False

    already_offered = db.query(LoanOffer).filter(
        LoanOffer.application_id == app.id,
        LoanOffer.lender_id == template.lender_id,
    ).first()
    if already_offered:
        return False

    return True


def _create_offer_from_template(db: Session, app: LoanApplication, template: LenderOfferTemplate) -> LoanOffer:
    total_interest = _calc_interest(app.amount, template.interest_rate, app.duration, app.duration_days)
    total_repayable = app.amount + total_interest
    monthly_payment = total_repayable if app.duration_days is not None else total_repayable / app.duration

    offer = LoanOffer(
        application_id=app.id,
        lender_id=template.lender_id,
        amount=app.amount,
        interest_rate=template.interest_rate,
        duration=app.duration,
        duration_days=app.duration_days,
        total_repayable=round(total_repayable, 2),
        monthly_payment=round(monthly_payment, 2),
        required_documents=template.required_documents,
    )
    db.add(offer)
    db.flush()

    lender = db.query(User).filter(User.id == template.lender_id).first()
    _notify(
        db, app.borrower_id,
        title="New offer received",
        message=(
            f"{lender.full_name if lender else 'A lender'} auto-offered UGX {app.amount:,.0f} "
            f"at {template.interest_rate}%/month for {_duration_label(app.duration, app.duration_days)}, "
            "matching your loan request."
        ),
        type="loan_offer",
        data={"application_id": app.id},
    )
    _notify(
        db, template.lender_id,
        title="Standing offer matched",
        message=(
            f"Your standing offer criteria matched a new UGX {app.amount:,.0f} "
            f"{app.loan_type} request — an offer was sent automatically."
        ),
        type="lender_offer_template",
        data={"application_id": app.id},
        pref_key="notif_new_application",
    )
    _audit(db, "offer_auto_matched", username=lender.username if lender else None,
           resource_type="loan_offer", resource_id=offer.id,
           details={"application_id": app.id, "template_id": template.id})
    return offer


def auto_match_offers_for_application(db: Session, app: LoanApplication) -> int:
    """New application → check it against every approved standing offer."""
    templates = db.query(LenderOfferTemplate).filter(LenderOfferTemplate.status == "approved").all()
    created = 0
    for template in templates:
        if _template_matches(db, template, app):
            _create_offer_from_template(db, app, template)
            created += 1
    return created


def _try_activate_matching(db: Session, app: LoanApplication) -> bool:
    """An application becomes matching-eligible the moment every one of its
    guarantors has accepted — called from the guarantor-respond endpoint in
    routers/guarantors.py. Documents are no longer part of this gate: a
    lender's required_documents aren't even known until a specific offer
    exists, so they're resolved and enforced at offer-accept time instead
    (see respond_to_offer's document check below), not here. Returns
    whether it just activated."""
    if app.status != "awaiting_guarantors":
        return False
    if not app.guarantors:
        return False
    if any(g.status != "accepted" for g in app.guarantors):
        return False
    app.status = "pending"
    auto_match_offers_for_application(db, app)
    return True


def auto_match_offers_for_template(db: Session, template: LenderOfferTemplate) -> int:
    """Newly-approved standing offer → check it against every pending application."""
    apps = db.query(LoanApplication).filter(LoanApplication.status == "pending").all()
    created = 0
    for app in apps:
        if _template_matches(db, template, app):
            _create_offer_from_template(db, app, template)
            created += 1
    return created


# ═══════════════════════════════════════════════
#  ACTIVE LOANS
# ═══════════════════════════════════════════════

@router.get("/active")
async def my_active_loans(
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """List active loans for current user (as borrower or lender)."""
    query = db.query(Loan).filter(
        (Loan.borrower_id == user.id) | (Loan.lender_id == user.id)
    )
    total = query.count()
    loans = query.order_by(Loan.created_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "loans": [_loan_response(l, db) for l in loans]}


@router.get("/earnings")
async def my_earnings(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Aggregate lender earnings, computed from real loan/repayment data.

    Interest earned is approximated per-repayment as
    repayment_amount * (total_repayable - amount) / total_repayable —
    i.e. every repayment carries the same interest/principal split as the
    loan overall. This matches the flat/add-on interest model used when
    offers are priced (see make_offer/create_application), since no
    amortization schedule is tracked to split payments more precisely.
    """
    # Excludes pending_disbursement — that money hasn't actually left the
    # lender's wallet yet, so it isn't "deployed"/earning anything.
    loans = db.query(Loan).filter(
        Loan.lender_id == user.id,
        Loan.status != "pending_disbursement",
    ).all()

    def interest_ratio(loan: Loan) -> float:
        return (loan.total_repayable - loan.amount) / loan.total_repayable if loan.total_repayable else 0.0

    active_loan_list = [l for l in loans if l.status in ("active", "overdue")]

    total_deployed = sum(l.amount for l in loans)
    active_loans = len(active_loan_list)
    total_repaid = sum(l.total_paid for l in loans)
    total_earned = sum(l.total_paid * interest_ratio(l) for l in loans)
    avg_yield = (
        sum(l.interest_rate for l in active_loan_list) / len(active_loan_list)
        if active_loan_list else 0.0
    )

    # Concentration warning — flags when too much of a lender's currently
    # outstanding capital sits with one borrower or one loan type, a standard
    # "don't put all your eggs in one basket" nudge on lending platforms.
    # Only the single worst offender is reported to keep the UI simple.
    concentration_warning = None
    active_deployed = sum(l.amount for l in active_loan_list)
    if active_deployed > 0:
        by_borrower: dict = {}
        by_type: dict = {}
        for l in active_loan_list:
            by_borrower[l.borrower_id] = by_borrower.get(l.borrower_id, 0.0) + l.amount
            by_type[l.loan_type] = by_type.get(l.loan_type, 0.0) + l.amount

        worst_borrower_id = max(by_borrower, key=by_borrower.get)
        worst_borrower_pct = by_borrower[worst_borrower_id] / active_deployed * 100
        worst_type = max(by_type, key=by_type.get)
        worst_type_pct = by_type[worst_type] / active_deployed * 100

        if worst_borrower_pct >= worst_type_pct and worst_borrower_pct > 40:
            borrower_loan = next(l for l in active_loan_list if l.borrower_id == worst_borrower_id)
            concentration_warning = {
                "type": "borrower",
                "label": borrower_loan.borrower.full_name if borrower_loan.borrower else "One borrower",
                "pct": round(worst_borrower_pct, 1),
            }
        elif worst_type_pct > 40:
            concentration_warning = {
                "type": "loan_type",
                "label": worst_type,
                "pct": round(worst_type_pct, 1),
            }

    loan_by_id = {l.id: l for l in loans}
    monthly_totals = {}
    this_month_earned = 0.0
    now = datetime.now(timezone.utc)

    if loans:
        repayments = db.query(Repayment).filter(
            Repayment.loan_id.in_(list(loan_by_id.keys())),
        ).all()
        for r in repayments:
            loan = loan_by_id.get(r.loan_id)
            if not loan:
                continue
            earned_portion = r.amount * interest_ratio(loan)
            month_key = r.created_at.strftime("%Y-%m")
            monthly_totals[month_key] = monthly_totals.get(month_key, 0.0) + earned_portion
            if r.created_at.year == now.year and r.created_at.month == now.month:
                this_month_earned += earned_portion

    months = []
    y, m = now.year, now.month
    for _ in range(6):
        months.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    months.reverse()

    monthly_earnings = [
        {
            "month": f"{y:04d}-{m:02d}",
            "amount": round(monthly_totals.get(f"{y:04d}-{m:02d}", 0.0), 2),
        }
        for (y, m) in months
    ]

    return {
        "total_deployed": round(total_deployed, 2),
        "active_loans": active_loans,
        "total_repaid": round(total_repaid, 2),
        "total_earned": round(total_earned, 2),
        "this_month_earned": round(this_month_earned, 2),
        "avg_yield": round(avg_yield, 2),
        "monthly_earnings": monthly_earnings,
        "concentration_warning": concentration_warning,
    }


@router.get("/active/{loan_id}")
async def get_loan(
    loan_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    loan = db.query(Loan).filter(Loan.id == loan_id).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    if loan.borrower_id != user.id and loan.lender_id != user.id and not user.has_admin_access:
        raise HTTPException(status_code=403, detail="Not authorized")
    return _loan_response(loan, db, include_repayments=True)


@router.post("/active/{loan_id}/approve-disbursement")
async def approve_disbursement(
    loan_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Lender approves and triggers the actual wallet-to-wallet transfer for
    a loan the borrower already accepted (see respond_to_offer, which
    creates it as 'pending_disbursement' without moving any money). The
    balance check happens here, not at accept time, since the lender's
    balance can change between the borrower's acceptance and this approval."""
    # Locked for the rest of this request — without this, two concurrent
    # approve calls (double-click, two devices) could both pass the
    # pending_disbursement check before either commits and both disburse,
    # debiting the lender and crediting the borrower twice for one loan.
    loan = db.query(Loan).filter(Loan.id == loan_id).with_for_update().first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    if loan.lender_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    if loan.status != "pending_disbursement":
        raise HTTPException(status_code=400, detail=f"Loan is not awaiting disbursement (status: {loan.status})")

    # Lock both wallets in a fixed order (ascending user_id) — not just
    # lender-then-borrower — so this never deadlocks against a concurrent
    # make_repayment on a *different* loan between the same two users (which
    # locks borrower-then-lender for that loan; if the user_ids happen to be
    # reversed between the two loans, opposite lock order would deadlock).
    first_uid, second_uid = sorted([loan.lender_id, loan.borrower_id])
    wallets_by_uid = {
        w.user_id: w
        for w in db.query(Wallet).filter(Wallet.user_id.in_([first_uid, second_uid])).with_for_update().all()
    }
    lender_wallet = wallets_by_uid.get(loan.lender_id)
    borrower_wallet = wallets_by_uid.get(loan.borrower_id)
    if not lender_wallet or not lender_wallet.is_wallet_setup:
        raise HTTPException(status_code=400, detail="Set up your wallet before approving disbursement")
    if not borrower_wallet or not borrower_wallet.is_wallet_setup:
        raise HTTPException(status_code=400, detail="Borrower's wallet is no longer set up — cannot disburse")

    platform_fee = calc_platform_fee(loan.amount)
    total_debit = loan.amount + platform_fee
    if lender_wallet.balance < total_debit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Insufficient wallet balance to fund this loan — needs UGX {total_debit:,.0f} "
                f"(UGX {platform_fee:,.0f} platform fee included). "
                f"You need UGX {total_debit - lender_wallet.balance:,.0f} more."
            ),
        )

    borrower = db.query(User).filter(User.id == loan.borrower_id).first()
    lender_wallet.balance -= total_debit
    borrower_wallet.balance += loan.amount

    lender_tx = WalletTransaction(
        wallet_id=lender_wallet.id,
        amount=loan.amount,
        type="disbursement",
        direction="debit",
        status="completed",
        description=f"Loan disbursed to {borrower.full_name or borrower.username}",
        counterparty=borrower.username,
        loan_id=loan.id,
    )
    db.add(lender_tx)
    db.flush()
    db.add(PlatformFeeTransaction(
        user_id=loan.lender_id,
        wallet_transaction_id=lender_tx.id,
        category="loan_disbursement",
        platform_fee=platform_fee,
        provider_fee=0,
        total_fee=platform_fee,
    ))

    db.add(WalletTransaction(
        wallet_id=borrower_wallet.id,
        amount=loan.amount,
        type="disbursement",
        direction="credit",
        status="completed",
        description=f"Loan received from {user.full_name or user.username}",
        counterparty=user.username,
        loan_id=loan.id,
    ))

    loan.status = "active"
    loan.disbursed_at = datetime.now(timezone.utc)
    loan.next_payment_date = datetime.now(timezone.utc) + timedelta(
        days=loan.duration_days if loan.duration_days is not None else 30
    )
    loan.next_payment_amount = loan.monthly_payment

    _notify(
        db, loan.borrower_id,
        title="Funds disbursed",
        message=f"{user.full_name or user.username} approved your loan — UGX {loan.amount:,.0f} has been disbursed to your Mpola wallet.",
        type="loan_disbursed",
        data={"loan_id": loan.id},
    )

    _audit(db, "loan_disbursed", username=user.username, user_id=user.id,
           resource_type="loan", resource_id=loan.id, details={"amount": loan.amount})

    db.commit()
    return {"status": 200, "message": "Loan disbursed", "loan": _loan_response(loan, db)}


# ═══════════════════════════════════════════════
#  REPAYMENTS
# ═══════════════════════════════════════════════

@router.post("/repayments")
async def make_repayment(
    data: RepaymentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Borrower makes a repayment on an active loan."""
    # Locked for the rest of this request — without this, two concurrent
    # repayment submissions on the same loan (double-click) could both read
    # the same paid_instalments/total_paid baseline and both apply on top
    # of it, silently losing one payment's worth of progress, or both debit
    # the borrower's wallet for what the UI showed as a single payment.
    loan = db.query(Loan).filter(Loan.id == data.loan_id, Loan.borrower_id == user.id).with_for_update().first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    if loan.status not in ("active", "overdue"):
        raise HTTPException(status_code=400, detail="Loan is not active")

    repayment = Repayment(
        loan_id=loan.id,
        amount=data.amount,
        instalment_number=loan.paid_instalments + 1,
        payment_method=data.payment_method,
    )
    db.add(repayment)

    if data.payment_method == "mobile_money":
        # Collect straight from the borrower's phone via UPG — doesn't touch the wallet.
        phone = data.phone_number or user.phone_number
        if not phone:
            raise HTTPException(status_code=400, detail="Phone number required for mobile money repayment")
        carrier = (data.carrier or _detect_carrier(phone)).upper()
        try:
            resp = UPGClient().collect(amount=data.amount, phone=phone, carrier=carrier)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Payment gateway error: {e}")
        if not UPGClient.is_success(resp):
            raise HTTPException(status_code=400, detail=resp.get("message", "Mobile money collection failed"))
        repayment.transaction_id = UPGClient.transaction_id(resp)
    else:
        # Wallet repayment — wallet-to-wallet: the borrower pays the amount plus
        # a 0.5% platform fee (TX_FEE_RATE, charged on every wallet transaction).
        # Separately, if part of this payment covers an outstanding late fee,
        # the platform also takes LATE_FEE_PLATFORM_CUT_RATE (5%) of just that
        # portion — carved out of what would otherwise go to the lender, not
        # an extra charge to the borrower. See utils/fee.py for both rates.
        # Lock both wallets in a fixed order (ascending user_id), matching
        # approve_disbursement's convention — prevents a deadlock if a
        # repayment and a disbursement between the same two users (on two
        # different loans, with lender/borrower roles reversed) land at the
        # same moment.
        first_uid, second_uid = sorted([user.id, loan.lender_id])
        wallets_by_uid = {
            w.user_id: w
            for w in db.query(Wallet).filter(Wallet.user_id.in_([first_uid, second_uid])).with_for_update().all()
        }
        wallet = wallets_by_uid.get(user.id)
        lender_wallet = wallets_by_uid.get(loan.lender_id)
        if not wallet or not wallet.is_wallet_setup:
            raise HTTPException(status_code=400, detail="Please set up your wallet first")
        if not lender_wallet or not lender_wallet.is_wallet_setup:
            raise HTTPException(status_code=400, detail="Lender's wallet is not set up — repayment cannot be completed")

        platform_fee = calc_platform_fee(data.amount)
        total_debit = data.amount + platform_fee
        if wallet.balance < total_debit:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient wallet balance — you need UGX {total_debit:,.0f} (UGX {platform_fee:,.0f} platform fee included)",
            )

        # Outstanding late fee is collected first out of whatever the borrower
        # pays this instalment; only that slice is subject to the platform's cut.
        outstanding_late_fee = max(0.0, (loan.late_fee_amount or 0.0) - (loan.late_fee_paid or 0.0))
        late_fee_portion = min(data.amount, outstanding_late_fee)
        late_fee_platform_cut = calc_late_fee_platform_cut(late_fee_portion)
        lender_credit = data.amount - late_fee_platform_cut

        wallet.balance -= total_debit
        lender_wallet.balance += lender_credit
        if late_fee_portion > 0:
            loan.late_fee_paid = (loan.late_fee_paid or 0.0) + late_fee_portion

        wallet_tx = WalletTransaction(
            wallet_id=wallet.id,
            amount=data.amount,
            type="repayment",
            direction="debit",
            status="completed",
            description=f"Loan repayment — instalment #{loan.paid_instalments + 1}",
            counterparty=loan.id,
            loan_id=loan.id,
        )
        db.add(wallet_tx)

        lender_tx_description = f"Repayment received from {user.full_name or user.username} — instalment #{loan.paid_instalments + 1}"
        if late_fee_platform_cut > 0:
            lender_tx_description += f" (includes UGX {late_fee_portion:,.0f} late fee, UGX {late_fee_platform_cut:,.0f} platform cut)"
        lender_tx = WalletTransaction(
            wallet_id=lender_wallet.id,
            amount=lender_credit,
            type="repayment",
            direction="credit",
            status="completed",
            description=lender_tx_description,
            counterparty=loan.id,
            loan_id=loan.id,
        )
        db.add(lender_tx)
        db.flush()  # populate tx ids before using them as references below
        repayment.transaction_id = wallet_tx.id
        repayment.lender_transaction_id = lender_tx.id

        db.add(PlatformFeeTransaction(
            user_id=user.id,
            wallet_transaction_id=wallet_tx.id,
            category="loan_repayment",
            platform_fee=platform_fee,
            provider_fee=0,
            total_fee=platform_fee,
        ))
        if late_fee_platform_cut > 0:
            db.add(PlatformFeeTransaction(
                user_id=loan.lender_id,
                wallet_transaction_id=lender_tx.id,
                category="late_fee_platform_cut",
                platform_fee=late_fee_platform_cut,
                provider_fee=0,
                total_fee=late_fee_platform_cut,
            ))

    loan.total_paid += data.amount
    loan.paid_instalments += 1
    if loan.total_paid >= loan.total_repayable:
        loan.status = "completed"
        loan.next_payment_date = None
        loan.next_payment_amount = None
        _notify(
            db, loan.lender_id,
            title="Loan fully repaid",
            message=f"{user.full_name or user.username} has fully repaid their UGX {loan.amount:,.0f} loan.",
            type="repayment",
            data={"loan_id": loan.id},
            pref_key="notif_repayment_received",
        )
    else:
        loan.next_payment_date = datetime.now(timezone.utc) + timedelta(days=30)
        loan.next_payment_amount = loan.monthly_payment
        _notify(
            db, loan.lender_id,
            title="Payment received",
            message=f"{user.full_name or user.username} paid UGX {data.amount:,.0f} (instalment #{repayment.instalment_number}).",
            type="repayment",
            pref_key="notif_repayment_received",
            data={"loan_id": loan.id},
        )

    _audit(db, "loan_repayment", username=user.username, user_id=user.id,
           resource_type="loan", resource_id=loan.id,
           details={"amount": data.amount, "payment_method": data.payment_method,
                     "instalment_number": repayment.instalment_number})
    db.commit()

    return {
        "status": 200,
        "message": "Repayment recorded",
        "repayment": {
            "id": repayment.id,
            "amount": repayment.amount,
            "instalment_number": repayment.instalment_number,
            "payment_method": repayment.payment_method,
            "transaction_id": repayment.transaction_id,
            "created_at": safe_isoformat(repayment.created_at),
        },
        "loan": _loan_response(loan, db),
    }


@router.get("/repayments/mine")
async def my_repayments(
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Full repayment history for the current borrower, across every loan
    they've ever taken — powers a receipts list, not just 'the latest one'."""
    query = (
        db.query(Repayment)
        .join(Loan, Loan.id == Repayment.loan_id)
        .filter(Loan.borrower_id == user.id)
    )
    total = query.count()
    repayments = query.order_by(Repayment.created_at.desc()).offset(skip).limit(limit).all()
    return {
        "total": total,
        "repayments": [
            {
                "id": r.id,
                "loan_id": r.loan_id,
                "amount": r.amount,
                "instalment_number": r.instalment_number,
                "status": r.status,
                "payment_method": r.payment_method,
                "transaction_id": r.transaction_id,
                "created_at": safe_isoformat(r.created_at),
                "lender_name": r.loan.lender_user.full_name if r.loan and r.loan.lender_user else None,
            }
            for r in repayments
        ],
    }


@router.get("/repayments/{repayment_id}/receipt")
async def get_repayment_receipt(
    repayment_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Real PDF receipt for a single repayment — borrower or lender on the loan only."""
    repayment = db.query(Repayment).filter(Repayment.id == repayment_id).first()
    if not repayment:
        raise HTTPException(status_code=404, detail="Repayment not found")

    loan = db.query(Loan).filter(Loan.id == repayment.loan_id).first()
    if not loan or user.id not in (loan.borrower_id, loan.lender_id):
        raise HTTPException(status_code=404, detail="Repayment not found")

    from utils.receipts import build_repayment_receipt_pdf

    pdf_bytes = build_repayment_receipt_pdf(
        receipt_id=repayment.id,
        borrower_name=loan.borrower.full_name or loan.borrower.username,
        lender_name=loan.lender_user.full_name or loan.lender_user.username,
        loan_reference=loan.application.reference_number if loan.application else loan.id[:10],
        amount=repayment.amount,
        instalment_number=repayment.instalment_number,
        payment_method=repayment.payment_method or "unknown",
        status=repayment.status,
        paid_at=str(repayment.created_at),
    )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="mpola-receipt-{repayment.id}.pdf"'},
    )


@router.get("/{loan_id}/disbursement-receipt")
async def get_disbursement_receipt(
    loan_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Real PDF receipt for a loan's disbursement — borrower or lender on the loan only."""
    loan = db.query(Loan).filter(Loan.id == loan_id).first()
    if not loan or user.id not in (loan.borrower_id, loan.lender_id):
        raise HTTPException(status_code=404, detail="Loan not found")
    if not loan.disbursed_at:
        raise HTTPException(status_code=400, detail="This loan hasn't been disbursed yet")

    from utils.receipts import build_disbursement_receipt_pdf

    pdf_bytes = build_disbursement_receipt_pdf(
        receipt_id=loan.id,
        borrower_name=loan.borrower.full_name or loan.borrower.username,
        lender_name=loan.lender_user.full_name or loan.lender_user.username,
        loan_reference=loan.application.reference_number if loan.application else loan.id[:10],
        amount=loan.amount,
        platform_fee=calc_platform_fee(loan.amount),
        disbursed_at=str(loan.disbursed_at),
    )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="mpola-disbursement-{loan.id}.pdf"'},
    )


# ═══════════════════════════════════════════════
#  RESPONSE HELPERS
# ═══════════════════════════════════════════════

def _app_response(app: LoanApplication, db: Session = None, include_offers: bool = False) -> dict:
    result = {
        "id": app.id,
        "reference_number": app.reference_number,
        "amount": app.amount,
        "duration": app.duration,
        "duration_days": app.duration_days,
        "loan_type": app.loan_type,
        "purpose": app.purpose,
        "status": app.status,
        "interest_rate": app.interest_rate,
        "monthly_payment": app.monthly_payment,
        "total_repayable": app.total_repayable,
        "max_interest_rate": app.max_interest_rate,
        "valid_until": safe_isoformat(app.valid_until),
        "is_frozen": app.is_frozen,
        "frozen_by": app.frozen_by,
        "created_at": safe_isoformat(app.created_at),
        "borrower": {
            "id": app.borrower.id,
            "full_name": app.borrower.full_name,
            "kyc_status": app.borrower.kyc_status,
            "credit_score": app.borrower.credit_score,
        } if app.borrower else None,
        "offers_count": len(app.offers),
        "pending_offers_count": sum(1 for o in app.offers if o.status == "pending"),
        # Once status is "funded", that alone doesn't say whether the lender
        # has actually released the money yet — check the real Loan row:
        # "pending_disbursement" means accepted but not yet disbursed,
        # anything past that (active, completed, ...) means it really did
        # get funded. Lets the frontend show "Awaiting Disbursement" vs the
        # real "Funded" instead of treating acceptance itself as funding.
        "loan_id": app.loan.id if app.loan else None,
        "loan_status": app.loan.status if app.loan else None,
        "loan_disbursed_at": safe_isoformat(app.loan.disbursed_at) if app.loan else None,
        "guarantors": [
            {
                "id": g.id,
                "guarantor_user_id": g.guarantor_user_id,
                "full_name": g.guarantor_user.full_name if g.guarantor_user else None,
                "username": g.guarantor_user.username if g.guarantor_user else None,
                "relationship_type": g.relationship_type,
                "status": g.status,
            }
            for g in app.guarantors
        ],
    }
    if include_offers:
        result["offers"] = [_offer_response(o, db) for o in app.offers]
    return result


def _offer_response(offer: LoanOffer, db: Session) -> dict:
    app = offer.application
    return {
        "id": offer.id,
        "application_id": offer.application_id,
        "application_reference": app.reference_number if app else None,
        "borrower_name": app.borrower.full_name if app and app.borrower else None,
        "loan_type": app.loan_type if app else None,
        "application_status": app.status if app else None,
        "lender_id": offer.lender_id,
        "lender_name": offer.lender.full_name if offer.lender else None,
        "amount": offer.amount,
        "interest_rate": offer.interest_rate,
        "duration": offer.duration,
        "duration_days": offer.duration_days,
        "monthly_payment": offer.monthly_payment,
        "total_repayable": offer.total_repayable,
        "status": offer.status,
        "required_documents": json.loads(offer.required_documents) if offer.required_documents else [],
        "required_documents_status": (
            _required_documents_status(db, app.borrower_id, offer.required_documents, app.id) if app else []
        ),
        "created_at": safe_isoformat(offer.created_at),
    }


def _loan_response(loan: Loan, db: Session = None, include_repayments: bool = False) -> dict:
    result = {
        "id": loan.id,
        "application_id": loan.application_id,
        "borrower_id": loan.borrower_id,
        "lender_id": loan.lender_id,
        "borrower_name": loan.borrower.full_name if loan.borrower else None,
        # Only the borrower/lender/admin on this loan can reach this
        # response at all (see GET /active/{loan_id}'s auth check), so it's
        # safe to include contact details here — the lender needs a real
        # way to reach the borrower before releasing funds.
        "borrower_phone": loan.borrower.phone_number if loan.borrower else None,
        "borrower_email": loan.borrower.email if loan.borrower else None,
        "lender_name": loan.lender_user.full_name if loan.lender_user else None,
        "amount": loan.amount,
        "interest_rate": loan.interest_rate,
        "duration": loan.duration,
        "duration_days": loan.duration_days,
        "monthly_payment": loan.monthly_payment,
        "total_repayable": loan.total_repayable,
        "total_paid": loan.total_paid,
        "paid_instalments": loan.paid_instalments,
        "total_instalments": loan.total_instalments,
        "next_payment_date": safe_isoformat(loan.next_payment_date),
        "next_payment_amount": loan.next_payment_amount,
        "late_fee_amount": loan.late_fee_amount or 0.0,
        "late_fee_paid": loan.late_fee_paid or 0.0,
        "status": loan.status,
        "disbursed_at": safe_isoformat(loan.disbursed_at),
        "created_at": safe_isoformat(loan.created_at),
        "borrower_note": loan.borrower_note,
        "required_documents": json.loads(loan.required_documents) if loan.required_documents else [],
        # Lets the lender's disbursement-approval screen show exactly what
        # was required next to what the borrower actually has on file,
        # right before releasing funds — only resolved when a db session is
        # passed (every route handler has one; kept optional so this
        # function still works from any db-less context).
        "required_documents_status": (
            _required_documents_status(db, loan.borrower_id, loan.required_documents, loan.application_id)
            if db is not None else []
        ),
        # Same guarantors who backed the original application — lets the
        # lender see who's vouching for this loan before approving
        # disbursement, not just the borrower's own say-so.
        "guarantors": [
            {
                "id": g.id,
                "full_name": g.guarantor_user.full_name if g.guarantor_user else None,
                "username": g.guarantor_user.username if g.guarantor_user else None,
                "relationship_type": g.relationship_type,
                "status": g.status,
            }
            for g in loan.application.guarantors
        ] if loan.application else [],
    }
    if include_repayments:
        result["repayments"] = [
            {
                "id": r.id,
                "amount": r.amount,
                "instalment_number": r.instalment_number,
                "status": r.status,
                "payment_method": r.payment_method,
                "transaction_id": r.transaction_id,
                "created_at": safe_isoformat(r.created_at),
            }
            for r in loan.repayments
        ]
    return result
