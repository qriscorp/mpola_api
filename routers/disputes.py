"""
Dispute flow — borrowers/lenders can flag a problem with a specific loan.
The filer and the other party on that loan (the "respondent", auto-derived
from the loan) are expected to try to work it out directly first — via
messages and a propose/accept settlement flow — before either one escalates
to admin. Admin can also step in and resolve directly at any point (see
routers/admin.py's resolve_dispute).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.tables import User, Dispute, DisputeMessage, Loan, Wallet, WalletTransaction
from helpers import safe_isoformat
from repository.auth_repo import _audit, _notify, _notify_admins
from repository.dependencies import get_db, current_active_user
from repository.models import DisputeCreate, DisputeMessageCreate, DisputeProposalCreate, DisputeProposalRespond
from routers.wallet import _ensure_wallet_not_frozen

router = APIRouter(prefix="/disputes", tags=["Disputes"])


def _party_label(dispute: Dispute, user_id: str) -> str:
    if user_id == dispute.user_id:
        return "filer"
    if user_id == dispute.respondent_id:
        return "respondent"
    return "admin"


def _require_party_or_admin(dispute: Dispute, user: User) -> None:
    if user.id in (dispute.user_id, dispute.respondent_id) or user.has_admin_access:
        return
    raise HTTPException(status_code=403, detail="Not authorized to view this dispute")


def _dispute_response(d: Dispute, include_messages: bool = False) -> dict:
    resp = {
        "id": d.id,
        "user_id": d.user_id,
        "filer_name": d.user.full_name or d.user.username if d.user else None,
        "respondent_id": d.respondent_id,
        "respondent_name": d.respondent.full_name or d.respondent.username if d.respondent else None,
        "category": d.category,
        "description": d.description,
        "status": d.status,
        "loan_id": d.loan_id,
        "resolution_note": d.resolution_note,
        "resolved_by": d.resolved_by,
        "resolved_at": safe_isoformat(d.resolved_at),
        "created_at": safe_isoformat(d.created_at),
        "proposal": None,
        "message_count": len(d.messages) if d.messages else 0,
    }
    if d.proposal_status:
        resp["proposal"] = {
            "proposed_by_id": d.proposed_by_id,
            "proposed_by_name": d.proposed_by.full_name or d.proposed_by.username if d.proposed_by else None,
            "note": d.proposal_note,
            "settlement_amount": d.settlement_amount,
            "settlement_payer_id": d.settlement_payer_id,
            "settlement_payer_name": d.settlement_payer.full_name or d.settlement_payer.username if d.settlement_payer else None,
            "status": d.proposal_status,
        }
    if include_messages:
        resp["messages"] = [
            {
                "id": m.id,
                "sender_id": m.sender_id,
                "sender_name": (m.sender.full_name or m.sender.username) if m.sender else None,
                "is_admin": m.is_admin,
                "message": m.message,
                "created_at": safe_isoformat(m.created_at),
            }
            for m in d.messages
        ]
    return resp


def _execute_dispute_settlement(db: Session, dispute: Dispute, payer_id: str, payee_id: str, amount: float, note: str) -> None:
    """Real wallet-to-wallet transfer, not an admin credit/debit out of thin
    air — money actually moves from the payer's wallet to the payee's,
    exactly like a repayment or disbursement does. Caller must db.commit()."""
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Settlement amount must be positive")

    first_uid, second_uid = sorted([payer_id, payee_id])
    wallets_by_uid = {
        w.user_id: w
        for w in db.query(Wallet).filter(Wallet.user_id.in_([first_uid, second_uid])).with_for_update().all()
    }
    payer_wallet = wallets_by_uid.get(payer_id)
    payee_wallet = wallets_by_uid.get(payee_id)
    if not payer_wallet or not payer_wallet.is_wallet_setup:
        raise HTTPException(status_code=400, detail="The paying party's wallet is not set up")
    if not payee_wallet or not payee_wallet.is_wallet_setup:
        raise HTTPException(status_code=400, detail="The receiving party's wallet is not set up")
    _ensure_wallet_not_frozen(payer_wallet, label="The paying party's")
    _ensure_wallet_not_frozen(payee_wallet, label="The receiving party's")
    if payer_wallet.balance < amount:
        raise HTTPException(
            status_code=400,
            detail=f"The paying party's wallet doesn't have enough balance for this settlement (needs UGX {amount:,.0f})",
        )

    payer = db.query(User).filter(User.id == payer_id).first()
    payee = db.query(User).filter(User.id == payee_id).first()

    payer_wallet.balance -= amount
    payee_wallet.balance += amount
    db.add(WalletTransaction(
        wallet_id=payer_wallet.id, amount=amount, type="dispute_settlement", direction="debit",
        status="completed", description=f"Dispute settlement: {note}"[:255],
        counterparty=payee.username if payee else None,
    ))
    db.add(WalletTransaction(
        wallet_id=payee_wallet.id, amount=amount, type="dispute_settlement", direction="credit",
        status="completed", description=f"Dispute settlement: {note}"[:255],
        counterparty=payer.username if payer else None,
    ))
    _audit(db, "dispute_settlement_executed", resource_type="dispute", resource_id=dispute.id,
           details={"amount": amount, "payer_id": payer_id, "payee_id": payee_id})


@router.post("")
def file_dispute(
    data: DisputeCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    respondent_id = None
    if data.loan_id:
        loan = db.query(Loan).filter(
            Loan.id == data.loan_id,
            (Loan.borrower_id == user.id) | (Loan.lender_id == user.id),
        ).first()
        if not loan:
            raise HTTPException(status_code=404, detail="Loan not found")
        respondent_id = loan.lender_id if loan.borrower_id == user.id else loan.borrower_id

    dispute = Dispute(
        user_id=user.id,
        respondent_id=respondent_id,
        loan_id=data.loan_id,
        category=data.category,
        description=data.description,
    )
    db.add(dispute)
    _audit(db, "dispute_filed", username=user.username, user_id=user.id,
           resource_type="dispute", details={"category": data.category, "loan_id": data.loan_id})

    if respondent_id:
        _notify(
            db, respondent_id,
            title="A dispute was filed about a loan you're on",
            message=f"{user.full_name or user.username} filed a dispute regarding a loan you share: \"{data.description[:150]}\". "
                    "Please review and respond — most disputes can be resolved directly between you two.",
            type="dispute",
        )
    else:
        _notify_admins(
            db,
            title="New dispute filed",
            message=f"{user.full_name or user.username} filed a dispute not tied to a specific loan: \"{data.description[:150]}\"",
            type="dispute",
        )

    db.commit()
    db.refresh(dispute)

    return {"status": 200, "message": "Dispute filed.", "dispute": _dispute_response(dispute)}


@router.get("/mine")
def my_disputes(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    disputes = (
        db.query(Dispute)
        .filter((Dispute.user_id == user.id) | (Dispute.respondent_id == user.id))
        .order_by(Dispute.created_at.desc())
        .all()
    )
    return {"disputes": [_dispute_response(d) for d in disputes]}


@router.get("/{dispute_id}")
def get_dispute(
    dispute_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    dispute = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    _require_party_or_admin(dispute, user)
    return {"dispute": _dispute_response(dispute, include_messages=True)}


@router.post("/{dispute_id}/messages")
def post_dispute_message(
    dispute_id: str,
    data: DisputeMessageCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    dispute = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    _require_party_or_admin(dispute, user)
    if dispute.status in ("resolved", "rejected"):
        raise HTTPException(status_code=400, detail="This dispute is already closed")

    msg = DisputeMessage(
        dispute_id=dispute.id,
        sender_id=user.id,
        is_admin=user.has_admin_access and user.id not in (dispute.user_id, dispute.respondent_id),
        message=data.message,
    )
    db.add(msg)

    # Notify everyone else on the dispute — both the other party and, once
    # escalated, admins should see new activity without having to poll.
    recipients = {dispute.user_id, dispute.respondent_id} - {user.id}
    recipients.discard(None)
    for uid in recipients:
        _notify(
            db, uid,
            title="New message on your dispute",
            message=f"{user.full_name or user.username}: {data.message[:150]}",
            type="dispute_update",
            data={"dispute_id": dispute.id},
        )
    if dispute.status == "investigating" and not msg.is_admin:
        _notify_admins(
            db,
            title="New message on an escalated dispute",
            message=f"{user.full_name or user.username}: {data.message[:150]}",
            type="dispute_update",
        )

    db.commit()
    db.refresh(msg)
    return {
        "status": 200,
        "message_data": {
            "id": msg.id,
            "sender_id": msg.sender_id,
            "sender_name": user.full_name or user.username,
            "is_admin": msg.is_admin,
            "message": msg.message,
            "created_at": safe_isoformat(msg.created_at),
        },
    }


@router.post("/{dispute_id}/propose")
def propose_resolution(
    dispute_id: str,
    data: DisputeProposalCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    dispute = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    if user.id not in (dispute.user_id, dispute.respondent_id):
        raise HTTPException(status_code=403, detail="Only the two parties on this dispute can propose a resolution")
    if not dispute.respondent_id:
        raise HTTPException(status_code=400, detail="This dispute has no counterparty to negotiate with — contact support")
    if dispute.status in ("resolved", "rejected"):
        raise HTTPException(status_code=400, detail="This dispute is already closed")
    if dispute.proposal_status == "pending":
        raise HTTPException(status_code=400, detail="There's already a pending proposal — it needs to be accepted or declined before a new one can be sent")
    if data.payer not in ("self", "other"):
        raise HTTPException(status_code=400, detail="payer must be 'self' or 'other'")

    counterparty_id = dispute.respondent_id if user.id == dispute.user_id else dispute.user_id
    payer_id = user.id if data.payer == "self" else counterparty_id

    dispute.proposed_by_id = user.id
    dispute.proposal_note = data.note
    dispute.settlement_amount = data.settlement_amount
    dispute.settlement_payer_id = payer_id if data.settlement_amount else None
    dispute.proposal_status = "pending"

    _audit(db, "dispute_proposal_made", username=user.username, user_id=user.id,
           resource_type="dispute", resource_id=dispute.id,
           details={"note": data.note, "settlement_amount": data.settlement_amount})
    _notify(
        db, counterparty_id,
        title="Resolution proposed on your dispute",
        message=f"{user.full_name or user.username} proposed: \"{data.note[:150]}\""
                + (f" (settlement: UGX {data.settlement_amount:,.0f})" if data.settlement_amount else ""),
        type="dispute_update",
        data={"dispute_id": dispute.id},
    )
    db.commit()
    return {"status": 200, "message": "Proposal sent.", "dispute": _dispute_response(dispute)}


@router.post("/{dispute_id}/respond-proposal")
def respond_to_proposal(
    dispute_id: str,
    data: DisputeProposalRespond,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    dispute = db.query(Dispute).filter(Dispute.id == dispute_id).with_for_update().first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    if dispute.proposal_status != "pending":
        raise HTTPException(status_code=400, detail="There is no pending proposal on this dispute")
    if user.id == dispute.proposed_by_id or user.id not in (dispute.user_id, dispute.respondent_id):
        raise HTTPException(status_code=403, detail="Only the other party can respond to this proposal")

    proposer_id = dispute.proposed_by_id
    note = dispute.proposal_note

    if data.accept:
        if dispute.settlement_amount:
            payer_id = dispute.settlement_payer_id
            payee_id = dispute.user_id if payer_id == dispute.respondent_id else dispute.respondent_id
            _execute_dispute_settlement(db, dispute, payer_id, payee_id, dispute.settlement_amount, note or "")

        dispute.status = "resolved"
        dispute.resolution_note = f"Resolved directly between both parties: {note}" if note else "Resolved directly between both parties."
        dispute.resolved_by = user.username
        dispute.resolved_at = datetime.now(timezone.utc)
        dispute.proposal_status = "accepted"

        _audit(db, "dispute_resolved", username=user.username, user_id=user.id,
               resource_type="dispute", resource_id=dispute.id, details={"status": "resolved", "by": "party"})
        for uid in (dispute.user_id, dispute.respondent_id):
            _notify(
                db, uid,
                title="Dispute resolved",
                message="Your dispute has been resolved — both parties agreed on a resolution."
                        + (f" UGX {dispute.settlement_amount:,.0f} was transferred." if dispute.settlement_amount else ""),
                type="dispute_update",
                data={"dispute_id": dispute.id},
            )
    else:
        dispute.proposal_status = "declined"
        _audit(db, "dispute_proposal_declined", username=user.username, user_id=user.id,
               resource_type="dispute", resource_id=dispute.id)
        _notify(
            db, proposer_id,
            title="Your proposal was declined",
            message=f"{user.full_name or user.username} declined your proposed resolution. You can propose again or escalate to Mpola support.",
            type="dispute_update",
            data={"dispute_id": dispute.id},
        )
        # Cleared so a fresh proposal can be made — proposal_status above
        # already carries the "declined" signal in the notification/audit.
        dispute.proposed_by_id = None
        dispute.proposal_note = None
        dispute.settlement_amount = None
        dispute.settlement_payer_id = None
        dispute.proposal_status = None

    db.commit()
    return {"status": 200, "dispute": _dispute_response(dispute)}


@router.post("/{dispute_id}/escalate")
def escalate_dispute(
    dispute_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    dispute = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    if user.id not in (dispute.user_id, dispute.respondent_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    if dispute.status in ("resolved", "rejected"):
        raise HTTPException(status_code=400, detail="This dispute is already closed")
    if dispute.status == "investigating":
        raise HTTPException(status_code=400, detail="Already escalated — Mpola support has this")

    dispute.status = "investigating"
    _audit(db, "dispute_escalated", username=user.username, user_id=user.id,
           resource_type="dispute", resource_id=dispute.id)
    other_id = dispute.respondent_id if user.id == dispute.user_id else dispute.user_id
    if other_id:
        _notify(
            db, other_id,
            title="Dispute escalated to Mpola support",
            message=f"{user.full_name or user.username} escalated your dispute to Mpola support for review.",
            type="dispute_update",
            data={"dispute_id": dispute.id},
        )
    _notify_admins(
        db,
        title="Dispute escalated for review",
        message=f"{user.full_name or user.username} escalated a dispute: \"{dispute.description[:150]}\"",
        type="dispute_update",
    )
    db.commit()
    return {"status": 200, "message": "Escalated to Mpola support.", "dispute": _dispute_response(dispute)}
