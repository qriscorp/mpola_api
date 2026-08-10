"""
Guarantors router — responding to a per-application guarantor request,
replacing a declined one, and reminding one who hasn't responded yet.
Adding guarantors happens inline in routers/loans.py (POST
/loans/applications/{app_id}/guarantors), right after the application
itself is created — this file covers everything that happens next.
"""
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database.tables import User, Guarantor, LoanApplication, PlatformSetting
from repository.auth_repo import _audit, _notify
from repository.dependencies import get_db, current_active_user
from repository.models import GuarantorRespond, GuarantorReplace

router = APIRouter(prefix="/guarantors", tags=["Guarantors"])

REMINDER_COOLDOWN_HOURS_DEFAULT = 24


def _reminder_cooldown_hours(db: Session) -> float:
    """Shared by the manual remind endpoint and the scheduled job below, so
    a manual nudge and the automatic one can't be combined to spam the
    guarantor faster than the configured rate."""
    setting = db.query(PlatformSetting).filter(PlatformSetting.key == "guarantor_reminder_cooldown_hours").first()
    if not setting:
        return REMINDER_COOLDOWN_HOURS_DEFAULT
    try:
        return float(setting.value)
    except (TypeError, ValueError):
        return REMINDER_COOLDOWN_HOURS_DEFAULT


@router.get("/requests")
async def list_guarantor_requests(
    status: str = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Requests where the caller is the invited guarantor — surfaced as a
    banner on the Notifications page (any role, since a lender can be
    asked to guarantee just as easily as a borrower)."""
    query = db.query(Guarantor).filter(Guarantor.guarantor_user_id == user.id)
    if status:
        query = query.filter(Guarantor.status == status)
    rows = query.order_by(Guarantor.created_at.desc()).all()
    return {
        "requests": [
            {
                "id": g.id,
                "application_id": g.application_id,
                "status": g.status,
                "amount": g.application.amount if g.application else None,
                "loan_type": g.application.loan_type if g.application else None,
                "duration": g.application.duration if g.application else None,
                "borrower_name": g.application.borrower.full_name if g.application and g.application.borrower else None,
                "created_at": str(g.created_at),
            }
            for g in rows
        ]
    }


@router.put("/{guarantor_id}/respond")
async def respond_to_guarantor_request(
    guarantor_id: str,
    data: GuarantorRespond,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """The invited guarantor accepts or declines. Accepting doesn't
    guarantee matching by itself — it's the LAST guarantor accepting
    that flips the application to matching-eligible and actually
    triggers auto_match_offers_for_application."""
    if data.status not in ("accepted", "declined"):
        raise HTTPException(status_code=400, detail="Status must be 'accepted' or 'declined'")

    g = db.query(Guarantor).filter(Guarantor.id == guarantor_id).first()
    if not g:
        raise HTTPException(status_code=404, detail="Guarantor request not found")
    if g.guarantor_user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    if g.status != "pending":
        raise HTTPException(status_code=400, detail=f"Already {g.status}")

    g.status = data.status
    g.responded_at = datetime.now(timezone.utc)

    app = db.query(LoanApplication).filter(LoanApplication.id == g.application_id).first()
    if app:
        _notify(
            db, app.borrower_id,
            title="Guarantor responded",
            message=f"{user.full_name or user.username} {data.status} your guarantor request "
                    f"on your UGX {app.amount:,.0f} loan request.",
            type="guarantor_response",
            data={"application_id": app.id, "guarantor_id": g.id, "status": data.status},
        )

        if data.status == "accepted" and app.status == "awaiting_guarantors":
            # This session disables autoflush (see database/__init__.py), so
            # the in-memory `g.status = data.status` above isn't visible to a
            # fresh query on the same row until flushed — without this, the
            # count below always finds this very row still "pending" and the
            # last acceptance never flips the application to matching-eligible.
            db.flush()
            still_pending_or_declined = db.query(Guarantor).filter(
                Guarantor.application_id == app.id,
                Guarantor.status != "accepted",
            ).count()
            if still_pending_or_declined == 0:
                # Import here, not at module level — routers/loans.py doesn't
                # import this module, so this avoids introducing a circular
                # import between the two guarantor-adjacent routers.
                from routers.loans import auto_match_offers_for_application
                app.status = "pending"
                auto_match_offers_for_application(db, app)

    _audit(db, "guarantor_responded", username=user.username, user_id=user.id,
           resource_type="loan_application", resource_id=g.application_id,
           details={"guarantor_id": g.id, "status": data.status})
    db.commit()

    return {"status": 200, "message": f"Guarantor request {data.status}"}


@router.post("/{guarantor_id}/remind")
async def remind_guarantor(
    guarantor_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Borrower manually nudges a guarantor who hasn't responded yet —
    re-fires the same real-time notification. Rate-limited by
    last_reminded_at, shared with the automatic scheduler reminder
    (scheduler._remind_pending_guarantors) so the two can't be combined to
    spam the guarantor faster than the configured cooldown."""
    g = db.query(Guarantor).filter(Guarantor.id == guarantor_id).first()
    if not g:
        raise HTTPException(status_code=404, detail="Guarantor request not found")
    app = db.query(LoanApplication).filter(LoanApplication.id == g.application_id).first()
    if not app or app.borrower_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    if g.status != "pending":
        raise HTTPException(status_code=400, detail=f"Already {g.status}")

    cooldown_hours = _reminder_cooldown_hours(db)
    if g.last_reminded_at:
        elapsed = datetime.now(timezone.utc) - g.last_reminded_at.replace(tzinfo=timezone.utc)
        remaining = timedelta(hours=cooldown_hours) - elapsed
        if remaining > timedelta(0):
            hours_left = max(1, round(remaining.total_seconds() / 3600))
            raise HTTPException(status_code=400, detail=f"You already reminded them recently — try again in about {hours_left}h")

    g.last_reminded_at = datetime.now(timezone.utc)
    _notify(
        db, g.guarantor_user_id,
        title="Reminder: guarantor request pending",
        message=(
            f"{user.full_name or user.username} is still waiting for you to approve or decline "
            f"their UGX {app.amount:,.0f} loan request."
        ),
        type="guarantor_invite_received",
        data={"application_id": app.id},
    )
    _audit(db, "guarantor_reminded", username=user.username, user_id=user.id,
           resource_type="loan_application", resource_id=app.id, details={"guarantor_id": g.id})
    db.commit()

    return {"status": 200, "message": "Reminder sent"}


@router.put("/applications/{app_id}/{guarantor_id}/replace")
async def replace_guarantor(
    app_id: str,
    guarantor_id: str,
    data: GuarantorReplace,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Swap out a guarantor who declined for a different candidate —
    only while the application is still awaiting_guarantors. Confirm the
    replacement is a real account via GET /users/search-guarantor-candidate
    before calling this."""
    app = db.query(LoanApplication).filter(
        LoanApplication.id == app_id, LoanApplication.borrower_id == user.id
    ).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    if app.status != "awaiting_guarantors":
        raise HTTPException(status_code=400, detail="This application is no longer awaiting guarantors")

    old = db.query(Guarantor).filter(
        Guarantor.id == guarantor_id, Guarantor.application_id == app_id
    ).first()
    if not old:
        raise HTTPException(status_code=404, detail="Guarantor not found on this application")
    if old.status != "declined":
        raise HTTPException(status_code=400, detail="Only a declined guarantor can be replaced")

    if data.new_guarantor_user_id == user.id:
        raise HTTPException(status_code=400, detail="You can't be your own guarantor")
    other_guarantor_ids = {
        gid for (gid,) in db.query(Guarantor.guarantor_user_id).filter(
            Guarantor.application_id == app_id, Guarantor.id != guarantor_id,
        ).all()
    }
    if data.new_guarantor_user_id in other_guarantor_ids:
        raise HTTPException(status_code=400, detail="Guarantors must be two different people")

    new_user = db.query(User).filter(User.id == data.new_guarantor_user_id).first()
    if not new_user:
        raise HTTPException(status_code=404, detail="That account no longer exists")

    db.delete(old)
    new_guarantor = Guarantor(application_id=app_id, guarantor_user_id=new_user.id)
    db.add(new_guarantor)

    _notify(
        db, new_user.id,
        title="Guarantor request",
        message=(
            f"{user.full_name or user.username} added you as a guarantor on their "
            f"UGX {app.amount:,.0f} loan request. Approve to help them get matched, "
            f"or decline if you're not interested."
        ),
        type="guarantor_invite_received",
        data={"application_id": app.id},
    )
    _audit(db, "guarantor_replaced", username=user.username, user_id=user.id,
           resource_type="loan_application", resource_id=app_id,
           details={"old_guarantor_user_id": old.guarantor_user_id, "new_guarantor_user_id": new_user.id})
    db.commit()

    return {"status": 200, "message": "Guarantor replaced — waiting for them to respond"}
