"""
Loan chat — a borrower and lender messaging each other about one specific
loan. Deliberately scoped to a Loan, not an open DM system — same reasoning
as disputes.py: a real-money platform needs an evidence trail, not
unscoped messaging that invites off-platform pressure/circumvention.
Mirrors disputes.py's message flow closely, minus the admin/resolution-lock
concepts that don't apply here.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.tables import User, Loan, LoanChatMessage
from helpers import safe_isoformat
from repository.auth_repo import _notify
from repository.dependencies import get_db, current_active_user
from repository.models import ChatMessageCreate

router = APIRouter(prefix="/chat", tags=["Chat"])


def _get_loan_as_party(db: Session, loan_id: str, user: User) -> Loan:
    loan = db.query(Loan).filter(Loan.id == loan_id).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    if user.id not in (loan.borrower_id, loan.lender_id):
        raise HTTPException(status_code=403, detail="Not authorized to view this conversation")
    return loan


def _other_party_id(loan: Loan, user: User) -> str:
    return loan.lender_id if user.id == loan.borrower_id else loan.borrower_id


def _unread_count(loan: Loan, user: User) -> int:
    my_read_at = loan.borrower_chat_read_at if user.id == loan.borrower_id else loan.lender_chat_read_at
    other_id = _other_party_id(loan, user)
    count = 0
    for m in loan.chat_messages:
        if m.sender_id == other_id and (my_read_at is None or m.created_at > my_read_at):
            count += 1
    return count


@router.get("/conversations")
def get_chat_conversations(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Every loan the caller is a party to (either role, any status — chat
    stays open after a loan completes, there's no 'closed' concept here
    unlike a resolved dispute). Powers the floating chat button's
    conversation list."""
    loans = db.query(Loan).filter(
        (Loan.borrower_id == user.id) | (Loan.lender_id == user.id)
    ).all()

    conversations = []
    for loan in loans:
        other_id = _other_party_id(loan, user)
        other = db.query(User).filter(User.id == other_id).first()
        last_message = loan.chat_messages[-1] if loan.chat_messages else None
        conversations.append({
            "loan_id": loan.id,
            "other_party_id": other_id,
            "other_party_name": (other.full_name or other.username) if other else None,
            "loan_amount": loan.amount,
            "loan_status": loan.status,
            "last_message": last_message.message if last_message else None,
            "last_message_at": safe_isoformat(last_message.created_at) if last_message else safe_isoformat(loan.created_at),
            "unread_count": _unread_count(loan, user),
        })

    conversations.sort(key=lambda c: c["last_message_at"] or "", reverse=True)
    return {"conversations": conversations}


@router.get("/unread-count")
def get_chat_unread_count(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Single aggregate number for the floating chat button's badge."""
    loans = db.query(Loan).filter(
        (Loan.borrower_id == user.id) | (Loan.lender_id == user.id)
    ).all()
    return {"unread_count": sum(_unread_count(loan, user) for loan in loans)}


@router.get("/loans/{loan_id}")
def get_loan_chat(
    loan_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    loan = _get_loan_as_party(db, loan_id, user)
    other = db.query(User).filter(User.id == _other_party_id(loan, user)).first()

    # Mark caller's own side as read — doesn't touch the other party's
    # column, so this never clears THEIR unread count.
    if user.id == loan.borrower_id:
        loan.borrower_chat_read_at = datetime.now(timezone.utc)
    else:
        loan.lender_chat_read_at = datetime.now(timezone.utc)
    db.commit()

    return {
        "other_party": {
            "id": other.id if other else None,
            "name": (other.full_name or other.username) if other else None,
            "kyc_status": other.kyc_status if other else None,
        },
        "messages": [
            {
                "id": m.id,
                "sender_id": m.sender_id,
                "sender_name": (m.sender.full_name or m.sender.username) if m.sender else None,
                "message": m.message,
                "created_at": safe_isoformat(m.created_at),
            }
            for m in loan.chat_messages
        ],
    }


@router.post("/loans/{loan_id}")
def post_loan_chat_message(
    loan_id: str,
    data: ChatMessageCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    loan = _get_loan_as_party(db, loan_id, user)

    msg = LoanChatMessage(loan_id=loan.id, sender_id=user.id, message=data.message)
    db.add(msg)

    _notify(
        db, _other_party_id(loan, user),
        title="New message",
        message=f"{user.full_name or user.username}: {data.message[:150]}",
        type="chat_message",
        data={"loan_id": loan.id},
    )

    db.commit()
    db.refresh(msg)
    return {
        "status": 200,
        "message_data": {
            "id": msg.id,
            "sender_id": msg.sender_id,
            "sender_name": user.full_name or user.username,
            "message": msg.message,
            "created_at": safe_isoformat(msg.created_at),
        },
    }
