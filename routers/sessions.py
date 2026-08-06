"""
Login session visibility. Mpola stores a single active refresh token per
user (see User.refresh_token) — there's no independent per-device session
store, so "sign out everywhere" invalidates that one token rather than
revoking an individually-selected device. This still gives users real
visibility into recent logins (device/IP/time), just not per-session revoke.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database.tables import User, LoginSession
from repository.auth_repo import _audit
from repository.dependencies import get_db, current_active_user

router = APIRouter(prefix="/sessions", tags=["Sessions"])


@router.get("/")
def list_sessions(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    sessions = (
        db.query(LoginSession)
        .filter(LoginSession.user_id == user.id)
        .order_by(LoginSession.created_at.desc())
        .limit(10)
        .all()
    )
    return {
        "sessions": [
            {
                "id": s.id,
                "device_label": s.device_label,
                "ip_address": s.ip_address,
                "user_agent": s.user_agent,
                "created_at": str(s.created_at),
                "is_most_recent": i == 0,
            }
            for i, s in enumerate(sessions)
        ],
    }


@router.post("/sign-out-everywhere")
def sign_out_everywhere(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    user.refresh_token = None
    user.refresh_token_expires_at = None
    _audit(db, "sign_out_everywhere", username=user.username, user_id=user.id, resource_type="user")
    db.commit()
    return {
        "status": 200,
        "message": "Signed out everywhere. Your current session stays active until its access token expires.",
    }
