"""
User repository — profile CRUD + admin user management.
"""

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from database.tables import User, DeactivatedAccount, Wallet
from logging_module import logger
from repository.auth_repo import get_password_hash, _generate_unique_referral_code


class UserRepo:

    @staticmethod
    def get_user_by_username(db: Session, username: str):
        user = db.query(User).filter(User.username == username).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "full_name": user.full_name,
            "phone_number": user.phone_number,
            "role": user.role,
            "is_admin": user.has_admin_access,
            "is_super_admin": user.has_super_admin_access,
            "is_verified": user.is_verified,
            "is_phone_verified": user.is_phone_verified,
            "is_kyc_verified": user.is_kyc_verified,
            "kyc_status": user.kyc_status,
            "account_type": user.account_type,
            "profile_pic": user.profile_pic,
            "credit_score": user.credit_score,
            "bio": user.bio,
            "nin": user.nin,
            "gender": user.gender,
            "date_of_birth": str(user.date_of_birth) if user.date_of_birth else None,
            "two_factor_enabled": user.two_factor_enabled,
            "created_at": str(user.created_at),
        }

    @staticmethod
    def update_user(db: Session, username: str, data):
        user = db.query(User).filter(User.username == username).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        update_dict = data.model_dump(exclude_unset=True)
        for key, val in update_dict.items():
            setattr(user, key, val)

        db.commit()
        db.refresh(user)
        return UserRepo.get_user_by_username(db, username)

    @staticmethod
    def get_deactivated_accounts(
        db: Session,
        skip: int = 0,
        limit: int = 50,
        search: str | None = None,
        reason: str | None = None,
        deactivated_by: str | None = None,
        sort_by: str | None = None,
        sort_dir: str | None = None,
    ):
        query = db.query(DeactivatedAccount)
        if search:
            filt = f"%{search}%"
            query = query.filter(
                DeactivatedAccount.original_username.ilike(filt)
                | DeactivatedAccount.original_email.ilike(filt)
            )
        if reason:
            query = query.filter(DeactivatedAccount.reason.ilike(f"%{reason}%"))
        if deactivated_by:
            query = query.filter(DeactivatedAccount.deactivated_by == deactivated_by)

        total = query.count()

        if sort_by:
            col = getattr(DeactivatedAccount, sort_by, DeactivatedAccount.created_at)
            query = query.order_by(col.desc() if sort_dir == "desc" else col.asc())
        else:
            query = query.order_by(DeactivatedAccount.created_at.desc())

        users = query.offset(skip).limit(limit).all()
        return {
            "total": total,
            "users": [
                {
                    "id": u.id,
                    "original_username": u.original_username,
                    "original_email": u.original_email,
                    "original_phone_number": u.original_phone_number,
                    "deactivated_by": u.deactivated_by,
                    "reason": u.reason,
                    "scheduled_deletion_date": str(u.scheduled_deletion_date) if u.scheduled_deletion_date else None,
                    "created_at": str(u.created_at) if u.created_at else None,
                }
                for u in users
            ],
        }

    @staticmethod
    def restore_deactivated_account(db: Session, username: str):
        record = db.query(DeactivatedAccount).filter(
            DeactivatedAccount.original_username == username
        ).first()
        if not record:
            raise HTTPException(status_code=404, detail="Deactivated account not found")

        existing = db.query(User).filter(User.username == username).first()
        if existing:
            raise HTTPException(status_code=400, detail="Username already in use")

        temp_password = secrets.token_urlsafe(10)
        user = User(
            username=record.original_username,
            email=record.original_email,
            phone_number=record.original_phone_number,
            password_hash=get_password_hash(temp_password),
            role="borrower",
            is_active=True,
            referral_code=_generate_unique_referral_code(db),
        )
        db.add(user)
        db.delete(record)
        db.commit()

        return {
            "success": True,
            "username": username,
            "temporary_password": temp_password,
            "message": "Account restored",
        }
