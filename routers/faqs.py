"""
FAQ content for the Help & Support page — single backend source of truth,
replacing what used to be hardcoded, independently-drifting Q&A arrays
duplicated in the website and the app. Public read (any authenticated
user, filtered by their role); admin-only write.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database.tables import Faq, User
from repository.dependencies import get_db, current_active_user
from repository.models import AuthUser, FaqCreate, FaqUpdate
from repository.security import require_admin

router = APIRouter(prefix="/faqs", tags=["FAQ"])
admin_router = APIRouter(prefix="/admin/faqs", tags=["Admin"])


def _faq_response(f: Faq) -> dict:
    return {
        "id": f.id,
        "category": f.category,
        "role": f.role,
        "question": f.question,
        "answer": f.answer,
        "sort_order": f.sort_order,
        "is_active": f.is_active,
    }


@router.get("")
def list_faqs(
    q: str = Query(None, description="Search text, matched against question and answer"),
    category: str = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    query = db.query(Faq).filter(Faq.is_active == True)  # noqa: E712
    query = query.filter(or_(Faq.role == "all", Faq.role == user.role))
    if category:
        query = query.filter(Faq.category == category)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Faq.question.ilike(like), Faq.answer.ilike(like)))
    faqs = query.order_by(Faq.sort_order.asc(), Faq.created_at.asc()).all()
    return {"faqs": [_faq_response(f) for f in faqs]}


@admin_router.get("")
def admin_list_faqs(
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    faqs = db.query(Faq).order_by(Faq.sort_order.asc(), Faq.created_at.asc()).all()
    return {"faqs": [_faq_response(f) for f in faqs]}


@admin_router.post("")
def admin_create_faq(
    data: FaqCreate,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    faq = Faq(**data.model_dump())
    db.add(faq)
    db.commit()
    db.refresh(faq)
    return {"status": 200, "faq": _faq_response(faq)}


@admin_router.put("/{faq_id}")
def admin_update_faq(
    faq_id: str,
    data: FaqUpdate,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    faq = db.query(Faq).filter(Faq.id == faq_id).first()
    if not faq:
        raise HTTPException(status_code=404, detail="FAQ not found")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(faq, key, value)
    db.commit()
    db.refresh(faq)
    return {"status": 200, "faq": _faq_response(faq)}


@admin_router.delete("/{faq_id}")
def admin_delete_faq(
    faq_id: str,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    faq = db.query(Faq).filter(Faq.id == faq_id).first()
    if not faq:
        raise HTTPException(status_code=404, detail="FAQ not found")
    db.delete(faq)
    db.commit()
    return {"status": 200, "message": "FAQ deleted"}
