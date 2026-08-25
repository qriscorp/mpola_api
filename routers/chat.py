"""
Loan chat — a borrower and lender messaging each other about one specific
loan. Deliberately scoped to a Loan, not an open DM system — same reasoning
as disputes.py: a real-money platform needs an evidence trail, not
unscoped messaging that invites off-platform pressure/circumvention.
Mirrors disputes.py's message flow closely, minus the admin/resolution-lock
concepts that don't apply here.
"""

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from config import BASE_URL
from database.tables import User, Loan, LoanChatMessage, AdminChatMessage
from helpers import generateUniqueId, safe_isoformat
from repository.auth_repo import _notify, _notify_admins
from repository.dependencies import get_db, current_active_user
from repository.models import AuthUser
from repository.security import require_admin
from routers.users import ALLOWED_BORROWER_DOCUMENT_EXTENSIONS, MAX_BORROWER_DOCUMENT_SIZE_BYTES

router = APIRouter(prefix="/chat", tags=["Chat"])


def _preview_text(message: str | None, file_name: str | None) -> str:
    if message:
        return message
    return f"Sent an attachment: {file_name}" if file_name else "Sent an attachment"


async def _save_chat_attachment(file: UploadFile) -> tuple[str, str]:
    """Mirrors submit_custom_document_response's save block (routers/loans.py)
    — same disk location, same size/extension limits as every other upload
    in this codebase."""
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
    return f"{BASE_URL}/uploads/{stored_name}", file.filename


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
            "last_message": _preview_text(last_message.message, last_message.file_name) if last_message else None,
            "last_message_at": safe_isoformat(last_message.created_at) if last_message else safe_isoformat(loan.created_at),
            "unread_count": _unread_count(loan, user),
        })

    conversations.sort(key=lambda c: c["last_message_at"] or "", reverse=True)

    admin_last = (
        db.query(AdminChatMessage)
        .filter(AdminChatMessage.user_id == user.id)
        .order_by(AdminChatMessage.created_at.desc())
        .first()
    )
    return {
        "conversations": conversations,
        # Powers the pinned "Mpola Support" row's preview — deliberately
        # never touches admin_chat_read_at (unlike GET /chat/admin), since
        # just seeing this list shouldn't clear the unread badge.
        "admin_chat": {
            "last_message": _preview_text(admin_last.message, admin_last.file_name) if admin_last else None,
            "last_message_at": safe_isoformat(admin_last.created_at) if admin_last else None,
            "unread_count": _admin_unread_count(db, user),
        },
    }


def _admin_unread_count(db: Session, user: User) -> int:
    q = db.query(AdminChatMessage).filter(
        AdminChatMessage.user_id == user.id,
        AdminChatMessage.is_admin == True,
    )
    if user.admin_chat_read_at is not None:
        q = q.filter(AdminChatMessage.created_at > user.admin_chat_read_at)
    return q.count()


@router.get("/unread-count")
def get_chat_unread_count(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Single aggregate number for the floating chat button's badge —
    loan conversations plus the Mpola Support thread, so one badge means
    'any unread message, from anyone.'"""
    loans = db.query(Loan).filter(
        (Loan.borrower_id == user.id) | (Loan.lender_id == user.id)
    ).all()
    total = sum(_unread_count(loan, user) for loan in loans) + _admin_unread_count(db, user)
    return {"unread_count": total}


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

    other_read_at = loan.lender_chat_read_at if user.id == loan.borrower_id else loan.borrower_chat_read_at

    return {
        "other_party": {
            "id": other.id if other else None,
            "name": (other.full_name or other.username) if other else None,
            "kyc_status": other.kyc_status if other else None,
        },
        # The OTHER party's read timestamp — lets the caller's own client
        # show a read tick on messages IT sent (compare against each
        # message's created_at), not on what it received.
        "other_party_read_at": safe_isoformat(other_read_at),
        "messages": [_loan_message_response(m) for m in loan.chat_messages],
    }


def _loan_message_response(m: LoanChatMessage) -> dict:
    return {
        "id": m.id,
        "sender_id": m.sender_id,
        "sender_name": (m.sender.full_name or m.sender.username) if m.sender else None,
        "message": m.message,
        "file_url": m.file_url,
        "file_name": m.file_name,
        "created_at": safe_isoformat(m.created_at),
    }


@router.post("/loans/{loan_id}")
async def post_loan_chat_message(
    loan_id: str,
    message: str | None = Form(None),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    if not message and not file:
        raise HTTPException(status_code=400, detail="Provide a message, a file, or both")
    loan = _get_loan_as_party(db, loan_id, user)

    file_url, file_name = (await _save_chat_attachment(file)) if file else (None, None)
    msg = LoanChatMessage(loan_id=loan.id, sender_id=user.id, message=message, file_url=file_url, file_name=file_name)
    db.add(msg)

    _notify(
        db, _other_party_id(loan, user),
        title="New message",
        message=f"{user.full_name or user.username}: {_preview_text(message, file_name)[:150]}",
        type="chat_message",
        data={"loan_id": loan.id},
    )

    db.commit()
    db.refresh(msg)
    return {"status": 200, "message_data": _loan_message_response(msg)}


# ═══════════════════════════════════════
#  ADMIN CHAT — live chat with Mpola Support, alongside (not replacing)
#  the SupportTicket system. One persistent conversation per user; any
#  admin/super admin can reply, no per-admin ownership — same shared-inbox
#  model SupportTicket already uses.
# ═══════════════════════════════════════

def _admin_message_response(m: AdminChatMessage) -> dict:
    return {
        "id": m.id,
        "sender_id": m.sender_id,
        "sender_name": (m.sender.full_name or m.sender.username) if m.sender else None,
        "is_admin": m.is_admin,
        "message": m.message,
        "file_url": m.file_url,
        "file_name": m.file_name,
        "created_at": safe_isoformat(m.created_at),
    }


@router.get("/admin")
def get_my_admin_chat(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """The caller's own conversation with Mpola Support. Lazily created —
    an empty thread is valid, the first POST creates the first row."""
    messages = (
        db.query(AdminChatMessage)
        .filter(AdminChatMessage.user_id == user.id)
        .order_by(AdminChatMessage.created_at)
        .all()
    )
    user.admin_chat_read_at = datetime.now(timezone.utc)
    db.commit()

    return {
        "other_party": {"name": "Mpola Support"},
        # Has any admin opened this thread — lets the caller see a read
        # tick on messages THEY sent to support.
        "admin_last_seen_at": safe_isoformat(user.admin_chat_seen_by_admin_at),
        "messages": [_admin_message_response(m) for m in messages],
    }


@router.post("/admin")
async def post_my_admin_chat_message(
    message: str | None = Form(None),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    if not message and not file:
        raise HTTPException(status_code=400, detail="Provide a message, a file, or both")

    file_url, file_name = (await _save_chat_attachment(file)) if file else (None, None)
    msg = AdminChatMessage(user_id=user.id, sender_id=user.id, is_admin=False, message=message, file_url=file_url, file_name=file_name)
    db.add(msg)

    _notify_admins(
        db,
        title=f"New message from {user.full_name or user.username}",
        message=_preview_text(message, file_name)[:150],
        type="admin_chat_message",
        data={"user_id": user.id},
    )

    db.commit()
    db.refresh(msg)
    return {"status": 200, "message_data": _admin_message_response(msg)}


@router.get("/admin/conversations")
def get_admin_chat_conversations(
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Every user with a Mpola Support conversation, sorted by latest
    activity — powers the admin dashboard's Live Chat inbox."""
    user_ids = [
        row[0]
        for row in db.query(AdminChatMessage.user_id).distinct().all()
    ]
    conversations = []
    for uid in user_ids:
        u = db.query(User).filter(User.id == uid).first()
        last_message = (
            db.query(AdminChatMessage)
            .filter(AdminChatMessage.user_id == uid)
            .order_by(AdminChatMessage.created_at.desc())
            .first()
        )
        if not last_message:
            continue
        conversations.append({
            "user_id": uid,
            "name": (u.full_name or u.username) if u else None,
            "role": u.role if u else None,
            "last_message": _preview_text(last_message.message, last_message.file_name),
            "last_message_at": safe_isoformat(last_message.created_at),
            "needs_reply": last_message.is_admin is False,
        })

    conversations.sort(key=lambda c: c["last_message_at"] or "", reverse=True)
    return {"conversations": conversations}


@router.get("/admin/conversations/{user_id}")
def get_admin_chat_conversation(
    user_id: str,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    messages = (
        db.query(AdminChatMessage)
        .filter(AdminChatMessage.user_id == user_id)
        .order_by(AdminChatMessage.created_at)
        .all()
    )

    # Mark "seen by an admin" — shared across every admin (no per-admin
    # ownership), exact counterpart to how get_my_admin_chat/get_loan_chat
    # mark their own caller's side.
    u.admin_chat_seen_by_admin_at = datetime.now(timezone.utc)
    db.commit()

    return {
        "other_party": {
            "id": u.id,
            "name": u.full_name or u.username,
            "role": u.role,
            "kyc_status": u.kyc_status,
        },
        # Lets the admin see a read tick on THEIR OWN replies once this
        # user opens the thread.
        "user_read_at": safe_isoformat(u.admin_chat_read_at),
        "messages": [_admin_message_response(m) for m in messages],
    }


@router.post("/admin/conversations/{user_id}")
async def reply_admin_chat_conversation(
    user_id: str,
    message: str | None = Form(None),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    if not message and not file:
        raise HTTPException(status_code=400, detail="Provide a message, a file, or both")
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    admin_user = db.query(User).filter(User.username == admin.username).first()
    file_url, file_name = (await _save_chat_attachment(file)) if file else (None, None)
    msg = AdminChatMessage(
        user_id=user_id,
        sender_id=admin_user.id if admin_user else None,
        is_admin=True,
        message=message,
        file_url=file_url,
        file_name=file_name,
    )
    db.add(msg)

    _notify(
        db, user_id,
        title="New message from Mpola Support",
        message=_preview_text(message, file_name)[:150],
        type="admin_chat_message",
        data={},
    )

    db.commit()
    db.refresh(msg)
    return {"status": 200, "message_data": _admin_message_response(msg)}
