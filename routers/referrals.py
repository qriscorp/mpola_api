"""
Referral program — every user gets a shareable code at signup;
invite-a-friend just means sharing the link.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from config import FRONTEND_URL
from database.tables import User
from repository.auth_repo import _generate_unique_referral_code
from repository.dependencies import get_db, current_active_user

router = APIRouter(prefix="/referrals", tags=["Referrals"])


@router.get("/me")
def my_referrals(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    if not user.referral_code:
        user.referral_code = _generate_unique_referral_code(db)
        db.commit()

    referred = (
        db.query(User)
        .filter(User.referred_by_id == user.id)
        .order_by(User.created_at.desc())
        .all()
    )

    return {
        "referral_code": user.referral_code,
        "referral_link": f"{FRONTEND_URL}/auth/register?ref={user.referral_code}",
        "total_referred": len(referred),
        "referred_users": [
            {
                "full_name": r.full_name,
                "role": r.role,
                "created_at": str(r.created_at),
            }
            for r in referred
        ],
    }
