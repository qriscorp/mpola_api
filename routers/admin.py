import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from database.tables import (
    User, DeactivatedAccount, Wallet, WalletTransaction,
    LoanApplication, LoanOffer, Loan, Repayment,
    Notification, PlatformSetting, AuditLog, LoginAttempt,
)
from repository.auth_repo import get_password_hash, _audit
from repository.dependencies import get_db
from repository.models import AuthUser, AdminRoleUpdate, PlatformSettingUpdate
from repository.security import require_admin
from repository.user_repo import UserRepo

router = APIRouter(prefix="/admin", tags=["Admin"])


# ═══════════════════════════════════════════════
#  DASHBOARD STATS
# ═══════════════════════════════════════════════

@router.get("/stats")
def get_dashboard_stats(
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Platform-wide statistics for admin dashboard."""
    total_users = db.query(func.count(User.id)).scalar()
    total_borrowers = db.query(func.count(User.id)).filter(User.role == "borrower").scalar()
    total_lenders = db.query(func.count(User.id)).filter(User.role == "lender").scalar()
    active_users = db.query(func.count(User.id)).filter(User.is_active == True).scalar()
    suspended_users = db.query(func.count(User.id)).filter(User.is_active == False).scalar()

    total_applications = db.query(func.count(LoanApplication.id)).scalar()
    pending_applications = db.query(func.count(LoanApplication.id)).filter(
        LoanApplication.status == "pending"
    ).scalar()

    total_active_loans = db.query(func.count(Loan.id)).filter(Loan.status == "active").scalar()
    total_loan_volume = db.query(func.sum(Loan.amount)).scalar() or 0
    total_repaid = db.query(func.sum(Loan.total_paid)).scalar() or 0

    total_wallet_balance = db.query(func.sum(Wallet.balance)).scalar() or 0

    return {
        "users": {
            "total": total_users,
            "borrowers": total_borrowers,
            "lenders": total_lenders,
            "active": active_users,
            "suspended": suspended_users,
        },
        "applications": {
            "total": total_applications,
            "pending": pending_applications,
        },
        "loans": {
            "active": total_active_loans,
            "total_volume": total_loan_volume,
            "total_repaid": total_repaid,
        },
        "platform": {
            "total_wallet_balance": total_wallet_balance,
        },
    }


# ═══════════════════════════════════════════════
#  USER MANAGEMENT
# ═══════════════════════════════════════════════

@router.get("/users")
def get_users(
    skip: int = 0,
    limit: int = 50,
    search: str = Query(None),
    status: str = Query(None),
    role: str = Query(None),  # borrower, lender, admin — SEPARATE filtering
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """List all users with role-based filtering (separate borrowers, lenders, admins)."""
    query = db.query(User)

    # Role filtering — the key feature for separating borrowers vs lenders
    if role:
        query = query.filter(func.lower(User.role) == role.lower())

    if search:
        filt = f"%{search}%"
        query = query.filter(
            User.username.ilike(filt)
            | User.email.ilike(filt)
            | User.full_name.ilike(filt)
            | User.phone_number.ilike(filt)
        )

    if status:
        if status == "active":
            query = query.filter(User.is_active == True)
        elif status == "suspended":
            query = query.filter(User.is_active == False)
        elif status == "verified":
            query = query.filter(User.is_kyc_verified == True)
        elif status == "unverified":
            query = query.filter(User.is_kyc_verified == False)

    total = query.count()
    users = query.order_by(User.created_at.desc()).offset(skip).limit(limit).all()

    return {
        "total": total,
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "email": u.email,
                "full_name": u.full_name,
                "phone_number": u.phone_number,
                "role": u.role,
                "is_active": u.is_active,
                "is_verified": u.is_verified,
                "is_kyc_verified": u.is_kyc_verified,
                "kyc_status": u.kyc_status,
                "credit_score": u.credit_score,
                "created_at": str(u.created_at),
            }
            for u in users
        ],
    }


@router.get("/users/{username}")
def get_user_detail(
    username: str,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Full user detail (admin view)."""
    return UserRepo.get_user_by_username(db, username)


@router.patch("/users/{username}/suspend")
def suspend_user(
    username: str,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Toggle suspend/unsuspend a user account."""
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent suspending other admins
    if user.role in ("admin", "super_admin") and admin.user_category != "super_admin":
        raise HTTPException(status_code=403, detail="Cannot suspend admin accounts")

    user.is_active = not user.is_active
    # Invalidate session on suspend
    if not user.is_active:
        user.refresh_token = None
        user.refresh_token_expires_at = None

    action = "unsuspended" if user.is_active else "suspended"
    _audit(db, f"user_{action}", username=admin.username,
           resource_type="user", resource_id=user.id,
           details={"target_user": username})
    db.commit()

    return {
        "success": True,
        "username": username,
        "is_active": user.is_active,
        "action": action,
    }


@router.patch("/users/{username}/role")
def change_user_role(
    username: str,
    data: AdminRoleUpdate,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Change a user's role (make admin, change to lender/borrower)."""
    # Only super_admin can create other admins
    if data.role in ("admin", "super_admin") and admin.user_category != "super_admin":
        raise HTTPException(status_code=403, detail="Only super admins can grant admin roles")

    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    old_role = user.role
    user.role = data.role

    _audit(db, "role_change", username=admin.username,
           resource_type="user", resource_id=user.id,
           details={"target_user": username, "old_role": old_role, "new_role": data.role})
    db.commit()

    return {
        "success": True,
        "username": username,
        "old_role": old_role,
        "new_role": user.role,
    }


@router.post("/users/{username}/deactivate")
def deactivate_user(
    username: str,
    reason: str = Query(None),
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Permanently deactivate a user (soft delete with 30 day retention)."""
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Cannot deactivate admin accounts")

    record = DeactivatedAccount(
        original_username=user.username,
        original_email=user.email,
        original_phone_number=user.phone_number,
        deactivated_by=admin.username,
        scheduled_deletion_date=datetime.now(timezone.utc) + timedelta(days=30),
        reason=reason,
    )
    db.add(record)

    # Clean up wallet data
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if wallet:
        db.query(WalletTransaction).filter(WalletTransaction.wallet_id == wallet.id).delete(synchronize_session=False)
        db.delete(wallet)

    db.delete(user)
    _audit(db, "user_deactivated", username=admin.username,
           resource_type="user", details={"target_user": username, "reason": reason})
    db.commit()

    return {"success": True, "username": username, "message": "Account deactivated. Data purged in 30 days."}


@router.post("/users/{username}/restore")
def restore_user(
    username: str,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Restore a deactivated user account."""
    return UserRepo.restore_deactivated_account(db, username)


@router.get("/users/deactivated/list")
def list_deactivated(
    skip: int = 0,
    limit: int = 50,
    search: str = Query(None),
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    return UserRepo.get_deactivated_accounts(db, skip=skip, limit=limit, search=search)


# ═══════════════════════════════════════════════
#  LOAN APPLICATION MANAGEMENT
# ═══════════════════════════════════════════════

@router.get("/applications")
def list_applications(
    status: str = Query(None),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    query = db.query(LoanApplication)
    if status:
        query = query.filter(LoanApplication.status == status)
    total = query.count()
    apps = query.order_by(LoanApplication.created_at.desc()).offset(skip).limit(limit).all()

    return {
        "total": total,
        "applications": [
            {
                "id": a.id,
                "reference_number": a.reference_number,
                "borrower_id": a.borrower_id,
                "amount": a.amount,
                "duration": a.duration,
                "loan_type": a.loan_type,
                "status": a.status,
                "interest_rate": a.interest_rate,
                "offer_count": len(a.offers) if a.offers else 0,
                "created_at": str(a.created_at),
            }
            for a in apps
        ],
    }


@router.patch("/applications/{app_id}")
def admin_update_application(
    app_id: str,
    action: str = Query(..., description="approve or reject"),
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Admin approve/reject a loan application."""
    app = db.query(LoanApplication).filter(LoanApplication.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    if action == "approve":
        app.status = "approved"
    elif action == "reject":
        app.status = "rejected"
    else:
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'")

    _audit(db, f"application_{action}", username=admin.username,
           resource_type="loan_application", resource_id=app.id)
    db.commit()
    return {"success": True, "status": app.status}


# ═══════════════════════════════════════════════
#  LOANS & PAYMENTS OVERVIEW
# ═══════════════════════════════════════════════

@router.get("/loans")
def list_all_loans(
    status: str = Query(None),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    query = db.query(Loan)
    if status:
        query = query.filter(Loan.status == status)
    total = query.count()
    loans = query.order_by(Loan.created_at.desc()).offset(skip).limit(limit).all()

    return {
        "total": total,
        "loans": [
            {
                "id": l.id,
                "borrower_id": l.borrower_id,
                "lender_id": l.lender_id,
                "amount": l.amount,
                "interest_rate": l.interest_rate,
                "total_repayable": l.total_repayable,
                "total_paid": l.total_paid,
                "status": l.status,
                "created_at": str(l.created_at),
            }
            for l in loans
        ],
    }


@router.get("/payments")
def list_all_payments(
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    total = db.query(func.count(WalletTransaction.id)).scalar()
    txs = db.query(WalletTransaction).order_by(
        WalletTransaction.created_at.desc()
    ).offset(skip).limit(limit).all()

    return {
        "total": total,
        "transactions": [
            {
                "id": tx.id,
                "wallet_id": tx.wallet_id,
                "amount": tx.amount,
                "type": tx.type,
                "status": tx.status,
                "description": tx.description,
                "reference": tx.reference,
                "created_at": str(tx.created_at),
            }
            for tx in txs
        ],
    }


# ═══════════════════════════════════════════════
#  PLATFORM SETTINGS
# ═══════════════════════════════════════════════

@router.get("/settings")
def get_settings(
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    settings = db.query(PlatformSetting).all()
    return {s.key: {"value": s.value, "description": s.description} for s in settings}


@router.put("/settings/{key}")
def update_setting(
    key: str,
    data: PlatformSettingUpdate,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    setting = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
    if not setting:
        setting = PlatformSetting(key=key, value=data.value)
        db.add(setting)
    else:
        setting.value = data.value

    _audit(db, "setting_update", username=admin.username,
           resource_type="platform_setting", resource_id=key,
           details={"value": data.value})
    db.commit()
    return {"success": True, "key": key, "value": data.value}


# ═══════════════════════════════════════════════
#  AUDIT LOGS (read-only for super admins)
# ═══════════════════════════════════════════════

@router.get("/audit-logs")
def get_audit_logs(
    action: str = Query(None),
    username: str = Query(None),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """View immutable audit trail — critical for fintech compliance."""
    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action == action)
    if username:
        query = query.filter(AuditLog.username == username)

    total = query.count()
    logs = query.order_by(AuditLog.created_at.desc()).offset(skip).limit(limit).all()

    return {
        "total": total,
        "logs": [
            {
                "id": l.id,
                "username": l.username,
                "action": l.action,
                "resource_type": l.resource_type,
                "resource_id": l.resource_id,
                "ip_address": l.ip_address,
                "details": json.loads(l.details) if l.details else None,
                "created_at": str(l.created_at),
            }
            for l in logs
        ],
    }


# ═══════════════════════════════════════════════
#  LOCKOUT MANAGEMENT
# ═══════════════════════════════════════════════

@router.delete("/lockout/{identifier}")
def clear_lockout(
    identifier: str,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Clear login lockout for a given identifier (email, username, or phone)."""
    deleted = (
        db.query(LoginAttempt)
        .filter(
            LoginAttempt.identifier == identifier.lower(),
            LoginAttempt.success == False,
        )
        .delete()
    )
    db.commit()
    _audit(db, "clear_lockout", username=admin.username,
           resource_type="user", details={"identifier": identifier, "records_removed": deleted})
    db.commit()
    return {"message": f"Lockout cleared. {deleted} failed attempt(s) removed.", "identifier": identifier}
