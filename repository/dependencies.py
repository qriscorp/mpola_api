"""
Core dependencies used throughout the API — follows kumpi_api pattern.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.orm import Session
from database import SessionLocal
from database.tables import User
from repository.models import AuthUser
from repository.auth_repo import AuthRepo

# HTTP Bearer token scheme
http_bearer = HTTPBearer(auto_error=False)


def get_db():
    """Yield a database session; close when request finishes.

    Also marks this connection as coming from the application — see the DB
    triggers on wallets/wallet_transactions/platform_fee_transactions
    (migration c9f4a2e7b6d1_wallets_balance_guard_trigger.py and its
    follow-up), which reject direct edits to sensitive financial fields
    (wallet balance, freeze status, transaction core facts, revenue
    records) unless this session variable is set. Every request that comes
    through the app sets it here, so legitimate code
    (deposit/withdraw/make_repayment/approve_disbursement/
    adjust_wallet_balance/toggle_wallet_freeze) is unaffected; a raw DB
    client (a leaked credential, phpMyAdmin, an ad-hoc script) that never
    runs this line gets rejected by the database itself, not just caught
    after the fact by the drift check. audit_logs has no bypass at all,
    for anyone — the app itself never updates or deletes an existing entry.
    """
    db = SessionLocal()
    try:
        db.execute(text("SET @app_authorized = 1"))
        yield db
    finally:
        db.close()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(http_bearer),
) -> AuthUser:
    """Extract user from JWT bearer token."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    token = credentials.credentials
    return AuthRepo.verify_token(token)


def current_active_user(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Load the full User ORM object and ensure the account is active."""
    user = db.query(User).filter(User.username == current_user.username).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account suspended")
    return user


def get_optional_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(http_bearer),
):
    """Return the AuthUser if a valid token is present, or None."""
    if credentials is None:
        return None
    try:
        return AuthRepo.verify_token(credentials.credentials)
    except Exception:
        return None
