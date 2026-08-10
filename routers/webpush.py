"""
Browser push subscription management for mpola_website — the desktop/
browser counterpart to the Expo push token stored on User for mpola_app.
A user can have several subscriptions (one per browser they've granted
permission on); _notify() (repository/auth_repo.py) sends to all of them.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from config import VAPID_PUBLIC_KEY
from database.tables import User, WebPushSubscription
from repository.dependencies import get_db, current_active_user
from repository.models import WebPushSubscribe, WebPushUnsubscribe

router = APIRouter(prefix="/webpush", tags=["Web Push"])


@router.get("/vapid-public-key")
async def get_vapid_public_key():
    """Unauthenticated on purpose — a VAPID public key is meant to be
    public; the frontend needs it before the user is necessarily logged
    in to nothing (though in practice this is only called once signed in)."""
    return {"public_key": VAPID_PUBLIC_KEY}


@router.post("/subscribe")
async def subscribe(
    data: WebPushSubscribe,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Upsert by endpoint — re-subscribing the same browser (e.g. after
    clearing the permission prompt again) updates its keys in place
    instead of accumulating duplicate rows."""
    existing = db.query(WebPushSubscription).filter(
        WebPushSubscription.user_id == user.id,
        WebPushSubscription.endpoint == data.endpoint,
    ).first()
    if existing:
        existing.p256dh = data.p256dh
        existing.auth = data.auth
    else:
        db.add(WebPushSubscription(
            user_id=user.id,
            endpoint=data.endpoint,
            p256dh=data.p256dh,
            auth=data.auth,
        ))
    db.commit()
    return {"status": 200, "message": "Subscribed"}


@router.post("/unsubscribe")
async def unsubscribe(
    data: WebPushUnsubscribe,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    db.query(WebPushSubscription).filter(
        WebPushSubscription.user_id == user.id,
        WebPushSubscription.endpoint == data.endpoint,
    ).delete()
    db.commit()
    return {"status": 200, "message": "Unsubscribed"}
