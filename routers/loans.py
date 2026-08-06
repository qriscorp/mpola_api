"""
Loans router — applications, offers, active loans, repayments.
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, Response
from sqlalchemy.orm import Session

from config import BASE_URL, FRONTEND_URL
from database.tables import User, LoanApplication, LoanOffer, LenderOfferTemplate, Loan, Repayment, Guarantor, LoanDocument, Wallet, WalletTransaction, PlatformFeeTransaction
from helpers import generateReferenceNumber, generateUniqueId, normalizePhoneNumber
from repository.auth_repo import _audit, _notify, send_sms
from repository.dependencies import get_db, current_active_user
from repository.models import LoanApplicationCreate, LoanOfferCreate, LoanOfferUpdate, LenderOfferTemplateCreate, RepaymentCreate, GuarantorCreate, GuarantorRespond
from repository.security import require_roles
from utils.upg_client import UPGClient, _detect_carrier
from utils.fee import calc_platform_fee

# Guarantors must reach this many "accepted" responses before a loan can be disbursed.
REQUIRED_ACCEPTED_GUARANTORS = 2

MAX_DOCUMENT_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}

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
        _platform_setting(db, "min_loan_amount", 100000),
        _platform_setting(db, "max_loan_amount", 50000000),
    )


def _max_interest_rate(db: Session) -> float:
    return _platform_setting(db, "max_interest_rate", 25)


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

    # Calculate estimated monthly payment (simple interest)
    rate = 15.0  # default platform rate
    total_interest = data.amount * (rate / 100) * (data.duration / 12)
    total_repayable = data.amount + total_interest
    monthly_payment = total_repayable / data.duration

    app = LoanApplication(
        borrower_id=user.id,
        reference_number=generateReferenceNumber(),
        amount=data.amount,
        duration=data.duration,
        loan_type=data.loan_type,
        purpose=data.purpose,
        interest_rate=rate,
        total_repayable=round(total_repayable, 2),
        monthly_payment=round(monthly_payment, 2),
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
        return _app_response(app, include_offers=True)
    raise HTTPException(status_code=403, detail="Not authorized")


@router.post("/applications/{app_id}/guarantors")
async def add_guarantor(
    app_id: str,
    data: GuarantorCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Add a guarantor and text them a link to accept or decline.
    A loan can't be disbursed until REQUIRED_ACCEPTED_GUARANTORS have accepted.
    """
    app = db.query(LoanApplication).filter(
        LoanApplication.id == app_id, LoanApplication.borrower_id == user.id
    ).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    token = generateUniqueId(22)
    g = Guarantor(
        application_id=app_id,
        name=data.name,
        phone=data.phone,
        relationship_type=data.relationship_type,
        confirmation_token=token,
    )
    db.add(g)
    _audit(db, "guarantor_added", username=user.username, user_id=user.id,
           resource_type="loan_application", resource_id=app_id,
           details={"guarantor_name": data.name, "guarantor_phone": data.phone})
    db.commit()

    normalized = normalizePhoneNumber(data.phone)
    if normalized:
        link = f"{FRONTEND_URL}/guarantor/{token}"
        borrower_name = user.full_name or user.username
        message = (
            f"{borrower_name} asked you to be a guarantor on Mpola for a loan of "
            f"UGX {app.amount:,.0f}. Confirm or decline: {link}"
        )
        threading.Thread(target=send_sms, args=(normalized, message), daemon=True).start()

    return {"status": 200, "message": "Guarantor added"}


# ═══════════════════════════════════════════════
#  GUARANTOR CONFIRMATION (public — guarantors have no account)
# ═══════════════════════════════════════════════

@router.get("/guarantors/{token}")
async def get_guarantor_invite(token: str, db: Session = Depends(get_db)):
    """Public lookup so a guarantor can see what they're being asked to confirm."""
    g = db.query(Guarantor).filter(Guarantor.confirmation_token == token).first()
    if not g:
        raise HTTPException(status_code=404, detail="Invite not found")

    app = db.query(LoanApplication).filter(LoanApplication.id == g.application_id).first()
    borrower = app.borrower if app else None

    return {
        "guarantor": {
            "id": g.id,
            "name": g.name,
            "status": g.status,
        },
        "application": {
            "id": app.id if app else None,
            "amount": app.amount if app else None,
            "duration": app.duration if app else None,
            "loan_type": app.loan_type if app else None,
            "borrower_name": borrower.full_name if borrower else None,
        },
    }


@router.post("/guarantors/{token}/respond")
async def respond_to_guarantor_invite(
    token: str,
    data: GuarantorRespond,
    db: Session = Depends(get_db),
):
    """Public — the guarantor accepts or declines via the SMS link. Single-use."""
    if data.status not in ("accepted", "declined"):
        raise HTTPException(status_code=400, detail="Status must be 'accepted' or 'declined'")

    g = db.query(Guarantor).filter(Guarantor.confirmation_token == token).first()
    if not g:
        raise HTTPException(status_code=404, detail="Invite not found")
    if g.status != "pending":
        raise HTTPException(status_code=400, detail=f"Already {g.status}")

    g.status = data.status
    g.responded_at = datetime.now(timezone.utc)

    app = db.query(LoanApplication).filter(LoanApplication.id == g.application_id).first()
    if app:
        _notify(
            db, app.borrower_id,
            title="Guarantor responded",
            message=f"{g.name} {data.status} your request to be a guarantor.",
            type="guarantor_response",
        )
    db.commit()

    return {"status": 200, "message": f"Guarantor {data.status}"}


# ═══════════════════════════════════════════════
#  APPLICATION DOCUMENTS
# ═══════════════════════════════════════════════

@router.post("/applications/{app_id}/documents")
async def upload_document(
    app_id: str,
    document_type: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    app = db.query(LoanApplication).filter(
        LoanApplication.id == app_id, LoanApplication.borrower_id == user.id
    ).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext or 'unknown'}")

    contents = await file.read()
    if len(contents) > MAX_DOCUMENT_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File exceeds 10MB limit")

    os.makedirs("uploads", exist_ok=True)
    stored_name = f"{generateUniqueId(20)}{ext}"
    with open(os.path.join("uploads", stored_name), "wb") as f:
        f.write(contents)

    doc = LoanDocument(
        application_id=app_id,
        document_type=document_type,
        file_url=f"{BASE_URL}/uploads/{stored_name}",
        file_name=file.filename,
    )
    db.add(doc)
    _audit(db, "document_uploaded", username=user.username, user_id=user.id,
           resource_type="loan_application", resource_id=app_id,
           details={"document_type": document_type})
    db.commit()

    return {
        "status": 200,
        "message": "Document uploaded",
        "document": {
            "id": doc.id,
            "document_type": doc.document_type,
            "file_url": doc.file_url,
            "file_name": doc.file_name,
            "verified": doc.verified,
        },
    }


@router.get("/applications/{app_id}/documents")
async def list_documents(
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    app = db.query(LoanApplication).filter(LoanApplication.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    if not (user.has_admin_access or user.role == "lender") and app.borrower_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    docs = db.query(LoanDocument).filter(LoanDocument.application_id == app_id).all()
    return {
        "documents": [
            {
                "id": d.id,
                "document_type": d.document_type,
                "file_url": d.file_url,
                "file_name": d.file_name,
                "verified": d.verified,
            }
            for d in docs
        ]
    }


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
    query = db.query(LoanApplication).filter(LoanApplication.status == "pending")
    if loan_type:
        query = query.filter(LoanApplication.loan_type == loan_type)
    if min_amount:
        query = query.filter(LoanApplication.amount >= min_amount)
    if max_amount:
        query = query.filter(LoanApplication.amount <= max_amount)

    total = query.count()
    apps = query.order_by(LoanApplication.created_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "applications": [_app_response(a) for a in apps]}


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
        raise HTTPException(status_code=400, detail=f"Interest rate cannot exceed {max_rate}% p.a.")

    total_interest = data.amount * (data.interest_rate / 100) * (data.duration / 12)
    total_repayable = data.amount + total_interest
    monthly_payment = total_repayable / data.duration

    offer = LoanOffer(
        application_id=data.application_id,
        lender_id=user.id,
        amount=data.amount,
        interest_rate=data.interest_rate,
        duration=data.duration,
        total_repayable=round(total_repayable, 2),
        monthly_payment=round(monthly_payment, 2),
    )
    db.add(offer)
    _notify(
        db, app.borrower_id,
        title="New offer received",
        message=(
            f"{user.full_name or user.username} offered UGX {data.amount:,.0f} "
            f"at {data.interest_rate}% p.a. for {data.duration} months on your loan request."
        ),
        type="loan_offer",
        data={"application_id": app.id},
    )
    db.commit()
    db.refresh(offer)

    return {"status": 200, "message": "Offer submitted", "offer": _offer_response(offer)}


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
    return {"total": total, "offers": [_offer_response(o) for o in offers]}


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
    return {"total": total, "offers": [_offer_response(o) for o in offers]}


@router.patch("/offers/{offer_id}")
async def respond_to_offer(
    offer_id: str,
    data: LoanOfferUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Borrower accepts or declines an offer."""
    if data.status not in ("accepted", "declined"):
        raise HTTPException(status_code=400, detail="Status must be 'accepted' or 'declined'")

    offer = db.query(LoanOffer).filter(LoanOffer.id == offer_id).first()
    if not offer:
        raise HTTPException(status_code=404, detail="Offer not found")

    app = db.query(LoanApplication).filter(LoanApplication.id == offer.application_id).first()
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

        # Disbursement is wallet-to-wallet: the lender must have the funds sitting
        # in their own Mpola wallet already, plus the 0.5% platform fee — the
        # borrower's wallet is credited the exact amount requested.
        lender_wallet = db.query(Wallet).filter(Wallet.user_id == offer.lender_id).first()
        if not lender_wallet or not lender_wallet.is_wallet_setup:
            raise HTTPException(status_code=400, detail="Lender has not set up their wallet yet — cannot disburse this loan")

        borrower_wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
        if not borrower_wallet or not borrower_wallet.is_wallet_setup:
            raise HTTPException(status_code=400, detail="Please set up your wallet before accepting a loan offer")

        platform_fee = calc_platform_fee(offer.amount)
        total_debit = offer.amount + platform_fee
        if lender_wallet.balance < total_debit:
            raise HTTPException(
                status_code=400,
                detail=f"Lender has insufficient wallet balance to fund this loan — needs UGX {total_debit:,.0f} (UGX {platform_fee:,.0f} platform fee included)",
            )

        offer.status = "accepted"
        app.status = "funded"

        lender_wallet.balance -= total_debit
        borrower_wallet.balance += offer.amount

        lender_tx = WalletTransaction(
            wallet_id=lender_wallet.id,
            amount=offer.amount,
            type="disbursement",
            status="completed",
            description=f"Loan disbursed to {user.full_name or user.username}",
            counterparty=user.username,
        )
        db.add(lender_tx)
        db.flush()
        db.add(PlatformFeeTransaction(
            user_id=offer.lender_id,
            wallet_transaction_id=lender_tx.id,
            category="loan_disbursement",
            platform_fee=platform_fee,
            provider_fee=0,
            total_fee=platform_fee,
        ))

        borrower_tx = WalletTransaction(
            wallet_id=borrower_wallet.id,
            amount=offer.amount,
            type="disbursement",
            status="completed",
            description=f"Loan received from {offer.lender.full_name or offer.lender.username}",
            counterparty=offer.lender.username,
        )
        db.add(borrower_tx)

        # Create active loan
        loan = Loan(
            application_id=app.id,
            borrower_id=app.borrower_id,
            lender_id=offer.lender_id,
            amount=offer.amount,
            interest_rate=offer.interest_rate,
            duration=offer.duration,
            monthly_payment=offer.monthly_payment,
            total_repayable=offer.total_repayable,
            total_instalments=offer.duration,
            next_payment_date=datetime.now(timezone.utc) + timedelta(days=30),
            next_payment_amount=offer.monthly_payment,
            disbursed_at=datetime.now(timezone.utc),
        )
        db.add(loan)

        _notify(
            db, offer.lender_id,
            title="Offer accepted",
            message=f"{user.full_name or user.username} accepted your offer of UGX {offer.amount:,.0f}. Funds have been disbursed.",
            type="offer_accepted",
            data={"application_id": app.id},
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
        "accepted_loan_types": json.loads(t.accepted_loan_types) if t.accepted_loan_types else [],
        "required_documents": json.loads(t.required_documents) if t.required_documents else [],
        "description": t.description,
        "valid_until": str(t.valid_until) if t.valid_until else None,
        "max_concurrent_loans": t.max_concurrent_loans,
        "status": t.status,
        "created_at": str(t.created_at),
    }


@router.post("/offer-templates")
async def create_offer_template(
    data: LenderOfferTemplateCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """A lender submits their standing lending criteria. There's no automated
    matching engine yet — submissions sit as 'pending_review' until an admin
    dashboard exists to review and publish them.
    """
    template = LenderOfferTemplate(
        lender_id=user.id,
        max_amount=data.max_amount,
        min_amount=data.min_amount,
        interest_rate=data.interest_rate,
        max_duration=data.max_duration,
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
    return {"total": total, "loans": [_loan_response(l) for l in loans]}


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
    loans = db.query(Loan).filter(Loan.lender_id == user.id).all()

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
    return _loan_response(loan, include_repayments=True)


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
    loan = db.query(Loan).filter(Loan.id == data.loan_id, Loan.borrower_id == user.id).first()
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
        # a 0.5% platform fee; the lender's wallet is credited the exact amount.
        wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
        if not wallet or not wallet.is_wallet_setup:
            raise HTTPException(status_code=400, detail="Please set up your wallet first")

        lender_wallet = db.query(Wallet).filter(Wallet.user_id == loan.lender_id).first()
        if not lender_wallet or not lender_wallet.is_wallet_setup:
            raise HTTPException(status_code=400, detail="Lender's wallet is not set up — repayment cannot be completed")

        platform_fee = calc_platform_fee(data.amount)
        total_debit = data.amount + platform_fee
        if wallet.balance < total_debit:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient wallet balance — you need UGX {total_debit:,.0f} (UGX {platform_fee:,.0f} platform fee included)",
            )

        wallet.balance -= total_debit
        lender_wallet.balance += data.amount

        wallet_tx = WalletTransaction(
            wallet_id=wallet.id,
            amount=data.amount,
            type="repayment",
            status="completed",
            description=f"Loan repayment — instalment #{loan.paid_instalments + 1}",
            counterparty=loan.id,
        )
        db.add(wallet_tx)
        db.flush()  # populate wallet_tx.id before using it as the repayment's reference
        repayment.transaction_id = wallet_tx.id

        db.add(PlatformFeeTransaction(
            user_id=user.id,
            wallet_transaction_id=wallet_tx.id,
            category="loan_repayment",
            platform_fee=platform_fee,
            provider_fee=0,
            total_fee=platform_fee,
        ))

        lender_tx = WalletTransaction(
            wallet_id=lender_wallet.id,
            amount=data.amount,
            type="repayment",
            status="completed",
            description=f"Repayment received from {user.full_name or user.username} — instalment #{loan.paid_instalments + 1}",
            counterparty=loan.id,
        )
        db.add(lender_tx)

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
        )
    else:
        loan.next_payment_date = datetime.now(timezone.utc) + timedelta(days=30)
        loan.next_payment_amount = loan.monthly_payment
        _notify(
            db, loan.lender_id,
            title="Payment received",
            message=f"{user.full_name or user.username} paid UGX {data.amount:,.0f} (instalment #{repayment.instalment_number}).",
            type="repayment",
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
            "created_at": str(repayment.created_at),
        },
        "loan": _loan_response(loan),
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


# ═══════════════════════════════════════════════
#  RESPONSE HELPERS
# ═══════════════════════════════════════════════

def _app_response(app: LoanApplication, include_offers: bool = False) -> dict:
    result = {
        "id": app.id,
        "reference_number": app.reference_number,
        "amount": app.amount,
        "duration": app.duration,
        "loan_type": app.loan_type,
        "purpose": app.purpose,
        "status": app.status,
        "interest_rate": app.interest_rate,
        "monthly_payment": app.monthly_payment,
        "total_repayable": app.total_repayable,
        "created_at": str(app.created_at),
        "borrower": {
            "id": app.borrower.id,
            "full_name": app.borrower.full_name,
            "kyc_status": app.borrower.kyc_status,
            "credit_score": app.borrower.credit_score,
        } if app.borrower else None,
        "offers_count": len(app.offers),
        "pending_offers_count": sum(1 for o in app.offers if o.status == "pending"),
    }
    if include_offers:
        result["offers"] = [_offer_response(o) for o in app.offers]
        result["guarantors"] = [
            {"id": g.id, "name": g.name, "phone": g.phone,
             "relationship_type": g.relationship_type, "status": g.status}
            for g in app.guarantors
        ]
    return result


def _offer_response(offer: LoanOffer) -> dict:
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
        "monthly_payment": offer.monthly_payment,
        "total_repayable": offer.total_repayable,
        "status": offer.status,
        "created_at": str(offer.created_at),
    }


def _loan_response(loan: Loan, include_repayments: bool = False) -> dict:
    result = {
        "id": loan.id,
        "borrower_id": loan.borrower_id,
        "lender_id": loan.lender_id,
        "borrower_name": loan.borrower.full_name if loan.borrower else None,
        "lender_name": loan.lender_user.full_name if loan.lender_user else None,
        "amount": loan.amount,
        "interest_rate": loan.interest_rate,
        "duration": loan.duration,
        "monthly_payment": loan.monthly_payment,
        "total_repayable": loan.total_repayable,
        "total_paid": loan.total_paid,
        "paid_instalments": loan.paid_instalments,
        "total_instalments": loan.total_instalments,
        "next_payment_date": str(loan.next_payment_date) if loan.next_payment_date else None,
        "next_payment_amount": loan.next_payment_amount,
        "status": loan.status,
        "disbursed_at": str(loan.disbursed_at) if loan.disbursed_at else None,
        "created_at": str(loan.created_at),
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
                "created_at": str(r.created_at),
            }
            for r in loan.repayments
        ]
    return result
