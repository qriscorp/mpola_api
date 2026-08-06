"""
Dispute flow — borrowers/lenders can flag a problem with a loan or
transaction; admins triage and resolve (see routers/admin.py).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.tables import User, Dispute, Loan
from repository.auth_repo import _audit, _notify
from repository.dependencies import get_db, current_active_user
from repository.models import DisputeCreate

router = APIRouter(prefix="/disputes", tags=["Disputes"])


def _dispute_response(d: Dispute) -> dict:
    return {
        "id": d.id,
        "category": d.category,
        "description": d.description,
        "status": d.status,
        "loan_id": d.loan_id,
        "resolution_note": d.resolution_note,
        "resolved_by": d.resolved_by,
        "resolved_at": str(d.resolved_at) if d.resolved_at else None,
        "created_at": str(d.created_at),
    }


@router.post("")
def file_dispute(
    data: DisputeCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    if data.loan_id:
        loan = db.query(Loan).filter(
            Loan.id == data.loan_id,
            (Loan.borrower_id == user.id) | (Loan.lender_id == user.id),
        ).first()
        if not loan:
            raise HTTPException(status_code=404, detail="Loan not found")

    dispute = Dispute(
        user_id=user.id,
        loan_id=data.loan_id,
        category=data.category,
        description=data.description,
    )
    db.add(dispute)
    _audit(db, "dispute_filed", username=user.username, user_id=user.id,
           resource_type="dispute", details={"category": data.category, "loan_id": data.loan_id})
    db.commit()
    db.refresh(dispute)

    return {"status": 200, "message": "Dispute filed — our team will review it shortly.", "dispute": _dispute_response(dispute)}


@router.get("/mine")
def my_disputes(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    disputes = (
        db.query(Dispute)
        .filter(Dispute.user_id == user.id)
        .order_by(Dispute.created_at.desc())
        .all()
    )
    return {"disputes": [_dispute_response(d) for d in disputes]}
