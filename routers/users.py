"""
Users router — profile management.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database.tables import User
from repository.dependencies import get_db, current_active_user
from repository.models import UserUpdate, PushTokenUpdate
from repository.user_repo import UserRepo

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/me")
async def get_profile(db: Session = Depends(get_db), user: User = Depends(current_active_user)):
    return UserRepo.get_user_by_username(db, user.username)


@router.put("/me")
async def update_profile(
    data: UserUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    return UserRepo.update_user(db, user.username, data)


@router.put("/me/push-token")
async def update_push_token(
    data: PushTokenUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Register (or clear, on sign-out) this device's Expo push token."""
    user.push_token = data.push_token
    db.commit()
    return {"status": 200, "message": "Push token updated"}


@router.get("/{username}")
async def get_user(username: str, db: Session = Depends(get_db), user: User = Depends(current_active_user)):
    """Get public profile of another user (limited fields)."""
    target = UserRepo.get_user_by_username(db, username)
    # Return only public fields for non-admin users
    if not user.has_admin_access and user.username != username:
        return {
            "username": target["username"],
            "full_name": target["full_name"],
            "profile_pic": target["profile_pic"],
            "role": target["role"],
            "is_kyc_verified": target["is_kyc_verified"],
            "credit_score": target["credit_score"],
            "created_at": target["created_at"],
        }
    return target
