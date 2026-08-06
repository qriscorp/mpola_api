"""
Support tickets — the in-app "Help & Support" flow. Not live chat, but a
real threaded ticket a user can open, reply to, and get an admin reply on
(see routers/admin.py for the admin side of the inbox).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.tables import User, SupportTicket, SupportMessage
from repository.auth_repo import _audit
from repository.dependencies import get_db, current_active_user
from repository.models import SupportTicketCreate, SupportMessageCreate

router = APIRouter(prefix="/support", tags=["Support"])


def _message_response(m: SupportMessage) -> dict:
    return {
        "id": m.id,
        "message": m.message,
        "is_admin": m.is_admin,
        "sender_name": m.sender.full_name if m.sender else None,
        "created_at": str(m.created_at),
    }


def _ticket_response(t: SupportTicket, include_messages: bool = False) -> dict:
    out = {
        "id": t.id,
        "subject": t.subject,
        "category": t.category,
        "status": t.status,
        "created_at": str(t.created_at),
        "message_count": len(t.messages) if t.messages else 0,
    }
    if include_messages:
        out["messages"] = [_message_response(m) for m in t.messages]
    return out


@router.post("")
def create_ticket(
    data: SupportTicketCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    ticket = SupportTicket(user_id=user.id, subject=data.subject, category=data.category)
    db.add(ticket)
    db.flush()
    db.add(SupportMessage(ticket_id=ticket.id, sender_id=user.id, is_admin=False, message=data.message))
    _audit(db, "support_ticket_opened", username=user.username, user_id=user.id,
           resource_type="support_ticket", resource_id=ticket.id, details={"category": data.category})
    db.commit()
    db.refresh(ticket)
    return {"status": 200, "message": "Ticket submitted", "ticket": _ticket_response(ticket, include_messages=True)}


@router.get("/mine")
def my_tickets(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    tickets = (
        db.query(SupportTicket)
        .filter(SupportTicket.user_id == user.id)
        .order_by(SupportTicket.created_at.desc())
        .all()
    )
    return {"tickets": [_ticket_response(t) for t in tickets]}


@router.get("/{ticket_id}")
def get_ticket(
    ticket_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    ticket = db.query(SupportTicket).filter(
        SupportTicket.id == ticket_id, SupportTicket.user_id == user.id
    ).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return {"ticket": _ticket_response(ticket, include_messages=True)}


@router.post("/{ticket_id}/messages")
def reply_to_ticket(
    ticket_id: str,
    data: SupportMessageCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    ticket = db.query(SupportTicket).filter(
        SupportTicket.id == ticket_id, SupportTicket.user_id == user.id
    ).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if ticket.status == "closed":
        raise HTTPException(status_code=400, detail="This ticket is closed")

    db.add(SupportMessage(ticket_id=ticket.id, sender_id=user.id, is_admin=False, message=data.message))
    if ticket.status == "resolved":
        ticket.status = "open"  # user replying to a "resolved" ticket reopens it
    db.commit()
    db.refresh(ticket)
    return {"status": 200, "ticket": _ticket_response(ticket, include_messages=True)}
