"""
Loans router — applications, offers, active loans, repayments.
Returns dummy data for now (connected to real auth).
"""

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database.tables import User, LoanApplication, LoanOffer, Loan, Repayment, Guarantor, LoanDocument
from helpers import generateReferenceNumber
from repository.dependencies import get_db, current_active_user
from repository.models import LoanApplicationCreate, LoanOfferCreate, LoanOfferUpdate, RepaymentCreate, GuarantorCreate
from repository.security import require_roles
from utils.upg_client import UPGClient, _detect_carrier

router = APIRouter(prefix="/loans", tags=["Loans"])


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
    if user.role in ("admin", "super_admin", "lender") or app.borrower_id == user.id:
        return _app_response(app, include_offers=True)
    raise HTTPException(status_code=403, detail="Not authorized")


@router.post("/applications/{app_id}/guarantors")
async def add_guarantor(
    app_id: str,
    data: GuarantorCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    app = db.query(LoanApplication).filter(
        LoanApplication.id == app_id, LoanApplication.borrower_id == user.id
    ).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    g = Guarantor(
        application_id=app_id,
        name=data.name,
        phone=data.phone,
        relationship_type=data.relationship_type,
    )
    db.add(g)
    db.commit()
    return {"status": 200, "message": "Guarantor added"}


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


@router.patch("/offers/{offer_id}")
async def respond_to_offer(
    offer_id: str,
    data: LoanOfferUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Borrower accepts or declines an offer."""
    offer = db.query(LoanOffer).filter(LoanOffer.id == offer_id).first()
    if not offer:
        raise HTTPException(status_code=404, detail="Offer not found")

    app = db.query(LoanApplication).filter(LoanApplication.id == offer.application_id).first()
    if not app or app.borrower_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    if data.status == "accepted":
        offer.status = "accepted"
        app.status = "funded"

        # Disburse loan funds to borrower's mobile money via UPG
        borrower = db.query(User).filter(User.id == app.borrower_id).first()
        borrower_phone = borrower.phone_number if borrower else None
        if not borrower_phone:
            raise HTTPException(status_code=400, detail="Borrower has no phone number on record for disbursement")
        carrier = _detect_carrier(borrower_phone)
        try:
            disburse_resp = UPGClient().disburse(amount=offer.amount, phone=borrower_phone, carrier=carrier)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Disbursement failed: {e}")
        if not UPGClient.is_success(disburse_resp):
            raise HTTPException(status_code=400, detail=disburse_resp.get("message", "Mobile money disbursement failed"))

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
        # Decline other pending offers
        db.query(LoanOffer).filter(
            LoanOffer.application_id == app.id,
            LoanOffer.id != offer_id,
            LoanOffer.status == "pending",
        ).update({"status": "declined"})
    else:
        offer.status = "declined"

    db.commit()
    return {"status": 200, "message": f"Offer {data.status}"}


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


@router.get("/active/{loan_id}")
async def get_loan(
    loan_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    loan = db.query(Loan).filter(Loan.id == loan_id).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    if loan.borrower_id != user.id and loan.lender_id != user.id and user.role not in ("admin", "super_admin"):
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

    # If paying via mobile money, collect from borrower's phone via UPG
    if data.payment_method == "mobile_money":
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
        repayment.reference = UPGClient.transaction_id(resp)

    loan.total_paid += data.amount
    loan.paid_instalments += 1
    if loan.total_paid >= loan.total_repayable:
        loan.status = "completed"
    else:
        loan.next_payment_date = datetime.now(timezone.utc) + timedelta(days=30)

    db.commit()
    return {"status": 200, "message": "Repayment recorded"}


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
    return {
        "id": offer.id,
        "application_id": offer.application_id,
        "lender_id": offer.lender_id,
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
                "created_at": str(r.created_at),
            }
            for r in loan.repayments
        ]
    return result
