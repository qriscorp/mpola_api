import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, aliased

from database.tables import (
    User, DeactivatedAccount, Wallet, WalletTransaction,
    LoanApplication, LoanOffer, LenderOfferTemplate, Loan, Repayment,
    Notification, PlatformSetting, AuditLog, LoginAttempt, PlatformFeeTransaction,
    Dispute, SupportTicket, SupportMessage, LoanDocument, KYCDocument,
)
from repository.auth_repo import get_password_hash, _audit, _notify
from repository.dependencies import get_db
from repository.models import (
    AuthUser, AdminRoleUpdate, AdminAccessUpdate, PlatformSettingUpdate,
    DisputeResolve, SupportMessageCreate, SupportTicketStatusUpdate,
    KYCReviewUpdate, DocumentVerifyUpdate, WalletAdjustmentModel,
)
from repository.security import require_admin, require_super_admin
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
    verified_users = db.query(func.count(User.id)).filter(User.is_kyc_verified == True).scalar()

    total_applications = db.query(func.count(LoanApplication.id)).scalar()
    pending_applications = db.query(func.count(LoanApplication.id)).filter(
        LoanApplication.status == "pending"
    ).scalar()

    total_active_loans = db.query(func.count(Loan.id)).filter(Loan.status == "active").scalar()
    total_completed_loans = db.query(func.count(Loan.id)).filter(Loan.status == "completed").scalar()
    total_defaulted_loans = db.query(func.count(Loan.id)).filter(Loan.status == "defaulted").scalar()
    # Excludes pending_disbursement — no money has moved on those loans yet,
    # so counting their amount/rate in these totals would overstate real
    # platform lending activity.
    _funded_loans = db.query(Loan).filter(Loan.status != "pending_disbursement")
    total_loans_count = _funded_loans.with_entities(func.count(Loan.id)).scalar()
    total_loan_volume = _funded_loans.with_entities(func.sum(Loan.amount)).scalar() or 0
    total_repaid = _funded_loans.with_entities(func.sum(Loan.total_paid)).scalar() or 0
    total_repayable = _funded_loans.with_entities(func.sum(Loan.total_repayable)).scalar() or 0
    avg_interest_rate = _funded_loans.with_entities(func.avg(Loan.interest_rate)).scalar() or 0.0

    total_wallet_balance = db.query(func.sum(Wallet.balance)).scalar() or 0

    # Interest generated platform-wide — goes to lenders, not Mpola. Shown
    # alongside platform revenue for context, but it's the lenders' earnings,
    # not ours. Same approximation used in the lender earnings endpoint:
    # every repayment carries the same interest/principal split as the loan.
    total_interest_generated = 0.0
    for amount, total_repayable_, total_paid in _funded_loans.with_entities(Loan.amount, Loan.total_repayable, Loan.total_paid).all():
        if total_repayable_:
            total_interest_generated += total_paid * (total_repayable_ - amount) / total_repayable_

    pending_offer_templates = db.query(func.count(LenderOfferTemplate.id)).filter(
        LenderOfferTemplate.status == "pending_review"
    ).scalar()

    # Real platform revenue — the 0.5% fee only (see utils/fee.py). Excludes
    # Interswitch/Flutterwave provider surcharges: those are collected from
    # withdrawing users but passed straight through to the provider, so
    # they're not Mpola's money and must never be counted as revenue.
    total_platform_revenue = db.query(func.sum(PlatformFeeTransaction.platform_fee)).scalar() or 0.0

    repayment_rate = round((total_repaid / total_repayable) * 100, 1) if total_repayable else 0.0
    default_rate = round((total_defaulted_loans / total_loans_count) * 100, 1) if total_loans_count else 0.0
    kyc_completion_rate = round((verified_users / total_users) * 100, 1) if total_users else 0.0

    application_status_rows = (
        db.query(LoanApplication.status, func.count(LoanApplication.id))
        .group_by(LoanApplication.status)
        .all()
    )
    status_counts = {s: c for s, c in application_status_rows}
    application_status_breakdown = [
        {"status": s.capitalize(), "count": status_counts.get(s, 0)}
        for s in ("pending", "funded", "completed", "rejected", "defaulted")
    ]

    # Loan type mix — sourced from applications that actually got funded/completed.
    type_rows = (
        db.query(LoanApplication.loan_type, func.count(LoanApplication.id))
        .filter(LoanApplication.status.in_(["funded", "completed"]))
        .group_by(LoanApplication.loan_type)
        .all()
    )
    total_typed = sum(c for _, c in type_rows) or 1
    loan_type_mix = [
        {"type": t or "unknown", "count": c, "percentage": round((c / total_typed) * 100, 1)}
        for t, c in type_rows
    ]

    # Last 6 months — disbursed volume vs. repayments collected, and signups by role.
    now = datetime.now(timezone.utc)
    months = []
    y, m = now.year, now.month
    for _ in range(6):
        months.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    months.reverse()

    disbursed_by_month: dict = {}
    for dt, amt in db.query(Loan.disbursed_at, Loan.amount).filter(Loan.disbursed_at.isnot(None)).all():
        key = dt.strftime("%Y-%m")
        disbursed_by_month[key] = disbursed_by_month.get(key, 0.0) + amt

    collected_by_month: dict = {}
    for dt, amt in db.query(Repayment.created_at, Repayment.amount).all():
        key = dt.strftime("%Y-%m")
        collected_by_month[key] = collected_by_month.get(key, 0.0) + amt

    revenue_by_month: dict = {}
    for dt, amt in db.query(PlatformFeeTransaction.created_at, PlatformFeeTransaction.platform_fee).all():
        key = dt.strftime("%Y-%m")
        revenue_by_month[key] = revenue_by_month.get(key, 0.0) + amt

    monthly_trend = [
        {
            "month": f"{y:04d}-{m:02d}",
            "disbursed": round(disbursed_by_month.get(f"{y:04d}-{m:02d}", 0.0), 2),
            "collected": round(collected_by_month.get(f"{y:04d}-{m:02d}", 0.0), 2),
            "revenue": round(revenue_by_month.get(f"{y:04d}-{m:02d}", 0.0), 2),
        }
        for (y, m) in months
    ]

    growth_by_month: dict = {}
    for dt, role in db.query(User.created_at, User.role).all():
        key = dt.strftime("%Y-%m")
        bucket = growth_by_month.setdefault(key, {"borrowers": 0, "lenders": 0})
        if role == "borrower":
            bucket["borrowers"] += 1
        elif role == "lender":
            bucket["lenders"] += 1

    user_growth = [
        {
            "month": f"{y:04d}-{m:02d}",
            "borrowers": growth_by_month.get(f"{y:04d}-{m:02d}", {}).get("borrowers", 0),
            "lenders": growth_by_month.get(f"{y:04d}-{m:02d}", {}).get("lenders", 0),
        }
        for (y, m) in months
    ]

    return {
        "users": {
            "total": total_users,
            "borrowers": total_borrowers,
            "lenders": total_lenders,
            "active": active_users,
            "suspended": suspended_users,
            "verified": verified_users,
        },
        "applications": {
            "total": total_applications,
            "pending": pending_applications,
        },
        "loans": {
            "active": total_active_loans,
            "completed": total_completed_loans,
            "defaulted": total_defaulted_loans,
            "total_volume": total_loan_volume,
            "total_repaid": total_repaid,
            "avg_interest_rate": round(avg_interest_rate, 2),
        },
        "platform": {
            "total_wallet_balance": total_wallet_balance,
            "total_interest_generated": round(total_interest_generated, 2),
            "total_platform_revenue": round(total_platform_revenue, 2),
            "repayment_rate": repayment_rate,
            "default_rate": default_rate,
            "kyc_completion_rate": kyc_completion_rate,
            "pending_offer_templates": pending_offer_templates,
        },
        "loan_type_mix": loan_type_mix,
        "application_status_breakdown": application_status_breakdown,
        "monthly_trend": monthly_trend,
        "user_growth": user_growth,
    }


# ═══════════════════════════════════════════════
#  GROWTH & ACTIVITY (simple, at-a-glance for admin)
# ═══════════════════════════════════════════════

@router.get("/activity")
def get_activity(
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """New users, transactions, and offers — today, this week vs last week,
    this month vs last month — plus a 14-day daily trend. One place for the
    admin to see how fast the platform is growing, no digging required.
    """
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=7)
    prev_week_start = today_start - timedelta(days=14)
    month_start = today_start - timedelta(days=30)
    prev_month_start = today_start - timedelta(days=60)

    def _count(model, date_col, start, end=None):
        q = db.query(func.count(model.id)).filter(date_col >= start)
        if end:
            q = q.filter(date_col < end)
        return q.scalar() or 0

    def _pct_change(current, previous):
        if previous == 0:
            return 100.0 if current > 0 else 0.0
        return round(((current - previous) / previous) * 100, 1)

    def _period_summary(model, date_col):
        this_week = _count(model, date_col, week_start)
        last_week = _count(model, date_col, prev_week_start, week_start)
        this_month = _count(model, date_col, month_start)
        last_month = _count(model, date_col, prev_month_start, month_start)
        return {
            "today": _count(model, date_col, today_start),
            "yesterday": _count(model, date_col, yesterday_start, today_start),
            "this_week": this_week,
            "last_week": last_week,
            "week_change_pct": _pct_change(this_week, last_week),
            "this_month": this_month,
            "last_month": last_month,
            "month_change_pct": _pct_change(this_month, last_month),
        }

    # 14-day daily trend, for a simple at-a-glance sparkline.
    day_keys = [(today_start - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
    trend_start = today_start - timedelta(days=13)

    def _daily_counts(model, date_col):
        by_day: dict = {}
        for (dt,) in db.query(date_col).filter(date_col >= trend_start).all():
            key = dt.strftime("%Y-%m-%d")
            by_day[key] = by_day.get(key, 0) + 1
        return by_day

    users_by_day = _daily_counts(User, User.created_at)
    tx_by_day = _daily_counts(WalletTransaction, WalletTransaction.created_at)
    offers_by_day = _daily_counts(LoanOffer, LoanOffer.created_at)

    daily_activity = [
        {
            "date": k,
            "new_users": users_by_day.get(k, 0),
            "transactions": tx_by_day.get(k, 0),
            "offers": offers_by_day.get(k, 0),
        }
        for k in day_keys
    ]

    return {
        "users": _period_summary(User, User.created_at),
        "transactions": _period_summary(WalletTransaction, WalletTransaction.created_at),
        "offers": _period_summary(LoanOffer, LoanOffer.created_at),
        "daily_activity": daily_activity,
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

    user_ids = [u.id for u in users]
    active_loans_by_borrower: dict = {}
    borrowed_by_borrower: dict = {}
    if user_ids:
        loan_rows = (
            db.query(Loan.borrower_id, Loan.status, Loan.amount)
            .filter(Loan.borrower_id.in_(user_ids))
            .all()
        )
        for borrower_id, status_, amount in loan_rows:
            borrowed_by_borrower[borrower_id] = borrowed_by_borrower.get(borrower_id, 0.0) + amount
            if status_ in ("active", "overdue"):
                active_loans_by_borrower[borrower_id] = active_loans_by_borrower.get(borrower_id, 0) + 1

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
                "is_admin": u.has_admin_access,
                "is_super_admin": u.has_super_admin_access,
                "is_active": u.is_active,
                "is_verified": u.is_verified,
                "is_kyc_verified": u.is_kyc_verified,
                "kyc_status": u.kyc_status,
                "credit_score": u.credit_score,
                "active_loans": active_loans_by_borrower.get(u.id, 0),
                "total_borrowed": round(borrowed_by_borrower.get(u.id, 0.0), 2),
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
    """Full user detail (admin view) — profile plus everything they've done
    on the platform, as borrower and/or lender.
    """
    profile = UserRepo.get_user_by_username(db, username)
    user_id = profile["id"]

    loans_as_borrower = db.query(Loan).filter(Loan.borrower_id == user_id).order_by(Loan.created_at.desc()).all()
    loans_as_lender = db.query(Loan).filter(Loan.lender_id == user_id).order_by(Loan.created_at.desc()).all()
    applications = (
        db.query(LoanApplication)
        .filter(LoanApplication.borrower_id == user_id)
        .order_by(LoanApplication.created_at.desc())
        .all()
    )

    documents = []
    if applications:
        documents = (
            db.query(LoanDocument)
            .filter(LoanDocument.application_id.in_([a.id for a in applications]))
            .order_by(LoanDocument.created_at.desc())
            .all()
        )

    kyc_documents = (
        db.query(KYCDocument)
        .filter(KYCDocument.user_id == user_id)
        .order_by(KYCDocument.created_at.desc())
        .all()
    )

    wallet = db.query(Wallet).filter(Wallet.user_id == user_id).first()
    transactions = []
    if wallet:
        transactions = (
            db.query(WalletTransaction)
            .filter(WalletTransaction.wallet_id == wallet.id)
            .order_by(WalletTransaction.created_at.desc())
            .limit(50)
            .all()
        )

    def _loan_summary(l: Loan, other_party_name) -> dict:
        return {
            "id": l.id,
            "other_party": other_party_name,
            "amount": l.amount,
            "interest_rate": l.interest_rate,
            "total_repayable": l.total_repayable,
            "total_paid": l.total_paid,
            "status": l.status,
            "disbursed_at": str(l.disbursed_at) if l.disbursed_at else None,
            "created_at": str(l.created_at),
        }

    return {
        "profile": profile,
        "loans_as_borrower": [
            _loan_summary(l, l.lender_user.full_name if l.lender_user else None)
            for l in loans_as_borrower
        ],
        "loans_as_lender": [
            _loan_summary(l, l.borrower.full_name if l.borrower else None)
            for l in loans_as_lender
        ],
        "applications": [
            {
                "id": a.id,
                "reference_number": a.reference_number,
                "amount": a.amount,
                "loan_type": a.loan_type,
                "status": a.status,
                "created_at": str(a.created_at),
            }
            for a in applications
        ],
        "documents": [
            {
                "id": d.id,
                "application_id": d.application_id,
                "document_type": d.document_type,
                "file_url": d.file_url,
                "file_name": d.file_name,
                "verified": d.verified,
                "created_at": str(d.created_at),
            }
            for d in documents
        ],
        "kyc_documents": [
            {
                "id": d.id,
                "document_type": d.document_type,
                "file_url": d.file_url,
                "file_name": d.file_name,
                "verified": d.verified,
                "created_at": str(d.created_at),
            }
            for d in kyc_documents
        ],
        "wallet": {
            "balance": wallet.balance if wallet else 0,
            "is_wallet_setup": wallet.is_wallet_setup if wallet else False,
        },
        "transactions": [
            {
                "id": tx.id,
                "amount": tx.amount,
                "type": tx.type,
                "status": tx.status,
                "description": tx.description,
                "created_at": str(tx.created_at),
            }
            for tx in transactions
        ],
    }


@router.put("/users/{username}/kyc")
def review_kyc(
    username: str,
    data: KYCReviewUpdate,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Admin approves or rejects a user's KYC — the only place in the codebase
    that ever changes is_kyc_verified/kyc_status away from their defaults."""
    if data.status not in ("verified", "rejected"):
        raise HTTPException(status_code=400, detail="status must be 'verified' or 'rejected'")

    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.kyc_status = data.status
    user.is_kyc_verified = data.status == "verified"

    _audit(db, f"kyc_{data.status}", username=admin.username,
           resource_type="user", resource_id=user.id,
           details={"target_user": username, "note": data.note})

    _notify(
        db, user.id,
        title="KYC verified" if data.status == "verified" else "KYC rejected",
        message=(
            "Your identity verification is complete — you now have full access to Mpola."
            if data.status == "verified"
            else f"Your identity verification was rejected.{f' Reason: {data.note}' if data.note else ' Please contact support for details.'}"
        ),
        type="kyc_update",
    )
    db.commit()

    return {
        "success": True,
        "username": username,
        "kyc_status": user.kyc_status,
        "is_kyc_verified": user.is_kyc_verified,
    }


@router.put("/documents/{document_id}/verify")
def verify_document(
    document_id: str,
    data: DocumentVerifyUpdate,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Marks a single uploaded document as verified/unverified — part of the
    evidence an admin reviews before approving or rejecting KYC overall."""
    document = db.query(LoanDocument).filter(LoanDocument.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    document.verified = data.verified
    _audit(db, "document_verified" if data.verified else "document_unverified",
           username=admin.username, resource_type="loan_document", resource_id=document.id)
    db.commit()

    return {"success": True, "document_id": document.id, "verified": document.verified}


@router.put("/kyc-documents/{document_id}/verify")
def verify_kyc_document(
    document_id: str,
    data: DocumentVerifyUpdate,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Marks one of a user's account-level KYC documents as verified/unverified."""
    document = db.query(KYCDocument).filter(KYCDocument.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    document.verified = data.verified
    _audit(db, "kyc_document_verified" if data.verified else "kyc_document_unverified",
           username=admin.username, resource_type="kyc_document", resource_id=document.id)
    db.commit()

    return {"success": True, "document_id": document.id, "verified": document.verified}


@router.put("/users/{username}/suspend")
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
    if user.has_admin_access and not admin.is_super_admin:
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


@router.put("/users/{username}/role")
def change_user_role(
    username: str,
    data: AdminRoleUpdate,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Change a user's portal role (borrower/lender), or set a legacy pure-admin
    role with no portal identity. To grant admin access to an existing
    lender/borrower WITHOUT changing their portal role, use
    PATCH /admin/users/{username}/admin-access instead."""
    # Only super_admin can create other admins
    if data.role in ("admin", "super_admin") and not admin.is_super_admin:
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


@router.put("/users/{username}/admin-access")
def set_admin_access(
    username: str,
    data: AdminAccessUpdate,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_super_admin),
):
    """Grant or revoke admin access on an existing lender/borrower account
    WITHOUT changing their portal role — e.g. a lender who should also be
    able to use the admin dashboard. Only super admins can do this."""
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if data.is_super_admin and not data.is_admin:
        raise HTTPException(status_code=400, detail="is_super_admin requires is_admin")

    user.is_admin = data.is_admin
    user.is_super_admin = data.is_admin and data.is_super_admin

    # Revoking admin access on a session should force re-auth so a stale
    # token (still carrying the old admin claim) can't keep working.
    if not data.is_admin:
        user.refresh_token = None
        user.refresh_token_expires_at = None

    _audit(db, "admin_access_change", username=admin.username,
           resource_type="user", resource_id=user.id,
           details={"target_user": username, "is_admin": user.is_admin, "is_super_admin": user.is_super_admin})
    db.commit()

    return {
        "success": True,
        "username": username,
        "role": user.role,
        "is_admin": user.is_admin,
        "is_super_admin": user.is_super_admin,
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
    if user.has_admin_access:
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
    search: str = Query(None),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    query = db.query(LoanApplication)
    if status:
        query = query.filter(LoanApplication.status == status)
    if search:
        filt = f"%{search}%"
        query = query.join(User, LoanApplication.borrower_id == User.id).filter(
            LoanApplication.reference_number.ilike(filt) | User.full_name.ilike(filt)
        )
    total = query.count()
    apps = query.order_by(LoanApplication.created_at.desc()).offset(skip).limit(limit).all()

    return {
        "total": total,
        "applications": [
            {
                "id": a.id,
                "reference_number": a.reference_number,
                "borrower_id": a.borrower_id,
                "borrower_name": a.borrower.full_name if a.borrower else None,
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


@router.put("/applications/{app_id}")
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
    search: str = Query(None),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    query = db.query(Loan)
    if status:
        query = query.filter(Loan.status == status)
    if search:
        filt = f"%{search}%"
        Borrower = aliased(User)
        Lender = aliased(User)
        query = (
            query.join(Borrower, Loan.borrower_id == Borrower.id)
            .join(Lender, Loan.lender_id == Lender.id)
            .outerjoin(LoanApplication, Loan.application_id == LoanApplication.id)
            .filter(
                Borrower.full_name.ilike(filt)
                | Lender.full_name.ilike(filt)
                | LoanApplication.reference_number.ilike(filt)
            )
        )
    total = query.count()
    loans = query.order_by(Loan.created_at.desc()).offset(skip).limit(limit).all()

    return {
        "total": total,
        "loans": [
            {
                "id": l.id,
                "reference": l.application.reference_number if l.application else l.id[:10],
                "borrower_id": l.borrower_id,
                "borrower_name": l.borrower.full_name if l.borrower else None,
                "lender_id": l.lender_id,
                "lender_name": l.lender_user.full_name if l.lender_user else None,
                "amount": l.amount,
                "interest_rate": l.interest_rate,
                "total_repayable": l.total_repayable,
                "total_paid": l.total_paid,
                "paid_instalments": l.paid_instalments,
                "total_instalments": l.total_instalments,
                "disbursed_at": str(l.disbursed_at) if l.disbursed_at else None,
                "next_payment_date": str(l.next_payment_date) if l.next_payment_date else None,
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

    inflow_types = ("deposit", "repayment", "top_up")
    outflow_types = ("withdrawal", "disbursement")
    total_in = db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.type.in_(inflow_types), WalletTransaction.status == "completed"
    ).scalar() or 0
    total_out = db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.type.in_(outflow_types), WalletTransaction.status == "completed"
    ).scalar() or 0

    return {
        "total": total,
        "totals": {"in": total_in, "out": total_out},
        "transactions": [
            {
                "id": tx.id,
                "wallet_id": tx.wallet_id,
                "username": tx.wallet.user.full_name if tx.wallet and tx.wallet.user else None,
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
#  PLATFORM REVENUE (fees charged on withdrawals)
# ═══════════════════════════════════════════════

@router.get("/revenue")
def get_revenue(
    category: str = Query(None, description="mobile_money_withdrawal or bank_withdrawal"),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Platform revenue ledger — the 0.5% fee Mpola actually keeps, charged on
    withdrawals, loan disbursements, and loan repayments (see utils/fee.py).
    Interswitch/Flutterwave provider surcharges are collected from users on
    withdrawals but passed straight through to the provider — that's not
    Mpola revenue, so it's deliberately excluded from every figure here.
    """
    query = db.query(PlatformFeeTransaction)
    if category:
        query = query.filter(PlatformFeeTransaction.category == category)
    total = query.count()
    rows = query.order_by(PlatformFeeTransaction.created_at.desc()).offset(skip).limit(limit).all()

    total_revenue = db.query(func.sum(PlatformFeeTransaction.platform_fee)).scalar() or 0.0

    by_category_rows = (
        db.query(PlatformFeeTransaction.category, func.sum(PlatformFeeTransaction.platform_fee), func.count(PlatformFeeTransaction.id))
        .group_by(PlatformFeeTransaction.category)
        .all()
    )
    by_category = [
        {"category": cat, "revenue": round(amt or 0.0, 2), "count": cnt}
        for cat, amt, cnt in by_category_rows
    ]

    now = datetime.now(timezone.utc)
    months = []
    y, m = now.year, now.month
    for _ in range(6):
        months.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    months.reverse()

    revenue_by_month: dict = {}
    for dt, amt in db.query(PlatformFeeTransaction.created_at, PlatformFeeTransaction.platform_fee).all():
        key = dt.strftime("%Y-%m")
        revenue_by_month[key] = revenue_by_month.get(key, 0.0) + amt

    monthly_revenue = [
        {"month": f"{y:04d}-{m:02d}", "revenue": round(revenue_by_month.get(f"{y:04d}-{m:02d}", 0.0), 2)}
        for (y, m) in months
    ]

    return {
        "total": total,
        "totals": {
            "revenue": round(total_revenue, 2),
        },
        "by_category": by_category,
        "monthly_revenue": monthly_revenue,
        "transactions": [
            {
                "id": r.id,
                "username": r.user.full_name if r.user else None,
                "category": r.category,
                "platform_fee": r.platform_fee,
                "created_at": str(r.created_at),
            }
            for r in rows
        ],
    }


@router.get("/reconciliation")
def get_reconciliation_report(
    lookback_days: int = Query(7, description="How far back to cross-check completed/failed transactions against UPG"),
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Financial integrity report, computed on demand (not stored/scheduled —
    this is an occasionally-viewed admin report, not a live/push feature).
    Two independent checks:

    1. Wallet drift — every wallet's stored balance should exactly equal the
       running sum of its own WalletTransaction rows. Any mismatch means a
       bug or an out-of-band DB edit happened; this should always come back
       empty on a healthy system.
    2. Gateway cross-check — for deposit/withdrawal transactions in the
       lookback window, re-ask UPG what it thinks the status is and flag any
       disagreement with what we have stored. Best-effort: UPG errors for an
       individual reference are skipped, not fatal to the whole report.
    """
    from utils.upg_client import UPGClient

    # ── 1. Wallet ledger drift ──────────────────────────────────────────
    # deposit is always a credit, withdrawal always a debit (both are pure
    # external<->wallet movement, unambiguous). repayment/disbursement are
    # each written as TWO WalletTransaction rows sharing the same `type` —
    # one debit (payer), one credit (payee) — with no direction column, so
    # direction has to be inferred: the 0.5% TX_FEE_RATE platform fee is
    # only ever attached (via PlatformFeeTransaction.wallet_transaction_id)
    # to the PAYING side's own transaction (category "loan_repayment" for
    # the borrower's repayment row, "loan_disbursement" for the lender's
    # disbursement row — see routers/loans.py's make_repayment and
    # approve_disbursement). The "late_fee_platform_cut" category is
    # deliberately excluded here — it attaches to the *credit*-side
    # (lender's) repayment row, not the debit side, so including it would
    # misclassify that row as a debit.
    debit_marker_categories = ("loan_repayment", "loan_disbursement")
    debit_marked_tx_ids = {
        row[0] for row in db.query(PlatformFeeTransaction.wallet_transaction_id)
        .filter(PlatformFeeTransaction.category.in_(debit_marker_categories))
        .all()
        if row[0]
    }

    wallet_drift = []
    wallets = db.query(Wallet).all()
    for wallet in wallets:
        txs = db.query(WalletTransaction).filter(WalletTransaction.wallet_id == wallet.id).all()
        ledger_balance = 0.0
        for tx in txs:
            if tx.status != "completed":
                continue
            if tx.type in ("withdrawal", "admin_debit"):
                is_debit = True
            elif tx.type in ("deposit", "admin_credit"):
                is_debit = False
            else:  # repayment or disbursement — direction inferred above
                is_debit = tx.id in debit_marked_tx_ids
            ledger_balance += -tx.amount if is_debit else tx.amount
        delta = round(wallet.balance - ledger_balance, 2)
        if abs(delta) > 0.01:
            user = db.query(User).filter(User.id == wallet.user_id).first()
            wallet_drift.append({
                "user_id": wallet.user_id,
                "username": user.username if user else None,
                "stored_balance": round(wallet.balance, 2),
                "ledger_balance": round(ledger_balance, 2),
                "delta": delta,
            })

    # ── 2. Gateway cross-check ──────────────────────────────────────────
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    recent_txs = db.query(WalletTransaction).filter(
        WalletTransaction.type.in_(["deposit", "withdrawal"]),
        WalletTransaction.status.in_(["completed", "failed"]),
        WalletTransaction.created_at >= cutoff,
        WalletTransaction.reference.isnot(None),
    ).all()
    gateway_mismatches = []
    client = UPGClient()
    for tx in recent_txs:
        try:
            if tx.type == "deposit":
                resp = client.get_transaction(tx.reference)
                gateway_status = (resp.get("status") or "").upper()
                agrees = (gateway_status == "SUCCESS" and tx.status == "completed") or \
                         (gateway_status in ("FAILED", "REVERSED") and tx.status == "failed")
            else:
                resp = client.get_payout_status(tx.reference)
                gateway_status = (resp.get("status") or "").lower()
                agrees = (gateway_status == "success" and tx.status == "completed") or \
                         (gateway_status == "failed" and tx.status == "failed")
            if not agrees:
                gateway_mismatches.append({
                    "transaction_id": tx.id,
                    "reference": tx.reference,
                    "type": tx.type,
                    "our_status": tx.status,
                    "gateway_status": gateway_status,
                })
        except Exception as e:
            logger_msg = str(e)
            gateway_mismatches.append({
                "transaction_id": tx.id,
                "reference": tx.reference,
                "type": tx.type,
                "our_status": tx.status,
                "gateway_status": f"error: {logger_msg[:200]}",
            })

    return {
        "wallet_drift": wallet_drift,
        "gateway_mismatches": gateway_mismatches,
        "checked_count": len(wallets) + len(recent_txs),
        "generated_at": str(datetime.now(timezone.utc)),
    }


@router.post("/wallets/{username}/adjust")
def adjust_wallet_balance(
    username: str,
    data: WalletAdjustmentModel,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_super_admin),
):
    """Manual correction tool for support/dispute resolution — the
    reconciliation report (GET /admin/reconciliation) can DETECT a wrong
    balance, but until this existed there was no way to actually FIX one.
    Gated at super-admin since it moves real money with no gateway/borrower
    counterpart to double-check it — every use is fully audited (before/after
    balance, reason, which admin) and the user is notified with a real
    WalletTransaction entry, so nothing here is silent or untraceable.
    """
    target = db.query(User).filter(User.username == username).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Locked — the same lost-update/overdraft class of bug this session
    # already fixed on the ordinary money-movement endpoints applies here too.
    wallet = db.query(Wallet).filter(Wallet.user_id == target.id).with_for_update().first()
    if not wallet:
        raise HTTPException(status_code=404, detail="User has no wallet")

    balance_before = wallet.balance
    wallet.balance += data.amount
    is_credit = data.amount > 0

    tx = WalletTransaction(
        wallet_id=wallet.id,
        amount=abs(data.amount),
        type="admin_credit" if is_credit else "admin_debit",
        status="completed",
        description=f"Balance adjustment by {admin.username}: {data.reason}",
        counterparty=admin.username,
    )
    db.add(tx)

    _audit(db, "wallet_adjusted", username=admin.username, user_id=target.id,
           resource_type="wallet", resource_id=wallet.id,
           details={
               "amount": data.amount,
               "reason": data.reason,
               "balance_before": balance_before,
               "balance_after": wallet.balance,
           })
    _notify(
        db, target.id,
        title="Wallet balance adjusted",
        message=(
            f"Your wallet was {'credited' if is_credit else 'debited'} UGX {abs(data.amount):,.0f} "
            f"by support: {data.reason}"
        ),
        type="payment",
    )
    db.commit()

    return {
        "status": 200,
        "message": "Wallet adjusted",
        "balance_before": balance_before,
        "balance_after": wallet.balance,
    }


# ═══════════════════════════════════════════════
#  LENDER STANDING OFFER REVIEW
# ═══════════════════════════════════════════════

@router.get("/offer-templates")
def list_offer_templates(
    status: str = Query(None),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """List lender-submitted standing offers awaiting (or already given) review."""
    query = db.query(LenderOfferTemplate)
    if status:
        query = query.filter(LenderOfferTemplate.status == status)
    total = query.count()
    templates = query.order_by(LenderOfferTemplate.created_at.desc()).offset(skip).limit(limit).all()

    return {
        "total": total,
        "templates": [
            {
                "id": t.id,
                "lender_id": t.lender_id,
                "lender_name": t.lender.full_name if t.lender else None,
                "max_amount": t.max_amount,
                "min_amount": t.min_amount,
                "interest_rate": t.interest_rate,
                "max_duration": t.max_duration,
                "accepted_loan_types": json.loads(t.accepted_loan_types) if t.accepted_loan_types else [],
                "required_documents": json.loads(t.required_documents) if t.required_documents else [],
                "description": t.description,
                "valid_until": str(t.valid_until) if t.valid_until else None,
                "max_concurrent_loans": t.max_concurrent_loans,
                "status": t.status,
                "is_frozen": t.is_frozen,
                "frozen_by": t.frozen_by,
                "created_at": str(t.created_at),
            }
            for t in templates
        ],
    }


@router.put("/offer-templates/{template_id}")
def review_offer_template(
    template_id: str,
    action: str = Query(..., description="approve or reject"),
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Admin approves or rejects a lender's submitted standing offer."""
    template = db.query(LenderOfferTemplate).filter(LenderOfferTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Offer template not found")

    if action == "approve":
        template.status = "approved"
    elif action == "reject":
        template.status = "rejected"
    else:
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'")

    _audit(db, f"offer_template_{action}", username=admin.username,
           resource_type="lender_offer_template", resource_id=template.id)

    matched = 0
    if action == "approve":
        from routers.loans import auto_match_offers_for_template
        matched = auto_match_offers_for_template(db, template)

    db.commit()
    return {"success": True, "status": template.status, "offers_created": matched}


@router.post("/offer-templates/{template_id}/freeze")
def freeze_offer_template(
    template_id: str,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Admin pauses ANY lender's approved standing offer — e.g. for abuse
    or a dispute. Only an admin can undo this (see unfreeze below)."""
    template = db.query(LenderOfferTemplate).filter(LenderOfferTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Offer template not found")
    if template.status != "approved":
        raise HTTPException(status_code=400, detail="Only approved offers can be frozen")

    template.is_frozen = True
    template.frozen_by = "admin"
    _audit(db, "offer_template_frozen_by_admin", username=admin.username,
           resource_type="lender_offer_template", resource_id=template.id)
    db.commit()
    return {"status": 200, "message": "Frozen"}


@router.post("/offer-templates/{template_id}/unfreeze")
def unfreeze_offer_template(
    template_id: str,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """Admin un-pauses a standing offer — works regardless of whether the
    lender or a previous admin action froze it."""
    template = db.query(LenderOfferTemplate).filter(LenderOfferTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Offer template not found")
    if not template.is_frozen:
        raise HTTPException(status_code=400, detail="Not frozen")

    template.is_frozen = False
    template.frozen_by = None
    _audit(db, "offer_template_unfrozen_by_admin", username=admin.username,
           resource_type="lender_offer_template", resource_id=template.id)
    db.commit()
    return {"status": 200, "message": "Unfrozen"}


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
    search: str = Query(None),
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
    if search:
        filt = f"%{search}%"
        query = query.filter(
            AuditLog.username.ilike(filt)
            | AuditLog.action.ilike(filt)
            | AuditLog.resource_type.ilike(filt)
        )

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


# ═══════════════════════════════════════════════
#  DISPUTES
# ═══════════════════════════════════════════════

@router.get("/disputes")
def list_disputes(
    status: str = Query(None),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    query = db.query(Dispute)
    if status:
        query = query.filter(Dispute.status == status)
    total = query.count()
    disputes = query.order_by(Dispute.created_at.desc()).offset(skip).limit(limit).all()

    return {
        "total": total,
        "disputes": [
            {
                "id": d.id,
                "user_id": d.user_id,
                "username": d.user.full_name or d.user.username if d.user else None,
                "loan_id": d.loan_id,
                "category": d.category,
                "description": d.description,
                "status": d.status,
                "resolution_note": d.resolution_note,
                "resolved_by": d.resolved_by,
                "resolved_at": str(d.resolved_at) if d.resolved_at else None,
                "created_at": str(d.created_at),
            }
            for d in disputes
        ],
    }


@router.put("/disputes/{dispute_id}")
def resolve_dispute(
    dispute_id: str,
    data: DisputeResolve,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    dispute = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    if data.status not in ("investigating", "resolved", "rejected"):
        raise HTTPException(status_code=400, detail="Invalid status")

    dispute.status = data.status
    dispute.resolution_note = data.resolution_note
    if data.status in ("resolved", "rejected"):
        dispute.resolved_by = admin.username
        dispute.resolved_at = datetime.now(timezone.utc)

    _audit(db, "dispute_resolved", username=admin.username,
           resource_type="dispute", resource_id=dispute.id, details={"status": data.status})
    _notify(
        db, dispute.user_id,
        title="Update on your dispute",
        message=f"Your dispute has been marked as {data.status}." + (f" {data.resolution_note}" if data.resolution_note else ""),
        type="dispute_update",
        data={"dispute_id": dispute.id},
    )
    db.commit()
    return {"status": 200, "message": "Dispute updated"}


# ═══════════════════════════════════════════════
#  SUPPORT TICKETS
# ═══════════════════════════════════════════════

@router.get("/support-tickets")
def list_support_tickets(
    status: str = Query(None),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    query = db.query(SupportTicket)
    if status:
        query = query.filter(SupportTicket.status == status)
    total = query.count()
    tickets = query.order_by(SupportTicket.created_at.desc()).offset(skip).limit(limit).all()

    return {
        "total": total,
        "tickets": [
            {
                "id": t.id,
                "username": t.user.full_name or t.user.username if t.user else None,
                "subject": t.subject,
                "category": t.category,
                "status": t.status,
                "message_count": len(t.messages) if t.messages else 0,
                "created_at": str(t.created_at),
            }
            for t in tickets
        ],
    }


@router.get("/support-tickets/{ticket_id}")
def get_support_ticket(
    ticket_id: str,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    return {
        "ticket": {
            "id": ticket.id,
            "username": ticket.user.full_name or ticket.user.username if ticket.user else None,
            "subject": ticket.subject,
            "category": ticket.category,
            "status": ticket.status,
            "created_at": str(ticket.created_at),
            "messages": [
                {
                    "id": m.id,
                    "message": m.message,
                    "is_admin": m.is_admin,
                    "sender_name": m.sender.full_name if m.sender else None,
                    "created_at": str(m.created_at),
                }
                for m in ticket.messages
            ],
        },
    }


@router.post("/support-tickets/{ticket_id}/reply")
def reply_to_support_ticket(
    ticket_id: str,
    data: SupportMessageCreate,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    admin_user = db.query(User).filter(User.username == admin.username).first()
    db.add(SupportMessage(ticket_id=ticket.id, sender_id=admin_user.id if admin_user else None, is_admin=True, message=data.message))
    ticket.status = "in_progress"

    _notify(
        db, ticket.user_id,
        title=f"New reply on: {ticket.subject}",
        message=data.message[:200],
        type="support_reply",
        data={"ticket_id": ticket.id},
    )
    db.commit()
    return {"status": 200, "message": "Reply sent"}


@router.put("/support-tickets/{ticket_id}/status")
def update_support_ticket_status(
    ticket_id: str,
    data: SupportTicketStatusUpdate,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if data.status not in ("open", "in_progress", "resolved", "closed"):
        raise HTTPException(status_code=400, detail="Invalid status")
    ticket.status = data.status
    db.commit()
    return {"status": 200, "message": "Ticket status updated"}
