"""
Notifications router.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database.tables import User, Notification
from helpers import safe_isoformat
from repository.dependencies import get_db, current_active_user

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("/")
async def list_notifications(
    skip: int = 0,
    limit: int = 50,
    is_read: bool = Query(None),
    types: str = Query(None, description="Comma-separated notification types to filter to"),
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    base_query = db.query(Notification).filter(Notification.user_id == user.id)
    # total_all/unread are always the TRUE counts regardless of any filter
    # applied below — the "All"/"Unread" filter pills' own badges shouldn't
    # shrink just because a *different* filter happens to be active.
    total_all = base_query.count()
    unread = base_query.filter(Notification.is_read == False).count()

    query = base_query
    if is_read is not None:
        query = query.filter(Notification.is_read == is_read)
    if types:
        type_list = [t.strip() for t in types.split(",") if t.strip()]
        if type_list:
            query = query.filter(Notification.type.in_(type_list))

    total = query.count()
    items = query.order_by(Notification.created_at.desc()).offset(skip).limit(limit).all()

    return {
        "total": total,
        "total_all": total_all,
        "unread": unread,
        "notifications": [
            {
                "id": n.id,
                "title": n.title,
                "message": n.message,
                "type": n.type,
                "is_read": n.is_read,
                "data": n.data,
                "created_at": safe_isoformat(n.created_at),
            }
            for n in items
        ],
    }


@router.put("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    n = db.query(Notification).filter(
        Notification.id == notification_id, Notification.user_id == user.id
    ).first()
    if not n:
        raise HTTPException(status_code=404, detail="Notification not found")
    n.is_read = True
    db.commit()
    return {"status": 200, "message": "Marked as read"}


@router.post("/read-all")
async def mark_all_read(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    db.query(Notification).filter(
        Notification.user_id == user.id, Notification.is_read == False
    ).update({"is_read": True})
    db.commit()
    return {"status": 200, "message": "All notifications marked as read"}
