"""
Role-based access control – follows kumpi_api pattern.
"""

from fastapi import Depends, HTTPException, status
from repository.dependencies import get_current_user
from repository.models import AuthUser


def require_roles(allowed: list[str]):
    """FastAPI dependency that restricts access to users with the specified roles."""

    def _check(current_user: AuthUser = Depends(get_current_user)):
        # Admins always permitted, regardless of their underlying portal role
        if current_user.is_admin:
            return current_user
        category = (current_user.user_category or "").lower()
        if category not in [r.lower() for r in allowed]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized for this resource",
            )
        return current_user

    return _check


def require_admin(current_user: AuthUser = Depends(get_current_user)):
    """Dependency that ensures the user has admin (or super_admin) access."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


def require_super_admin(current_user: AuthUser = Depends(get_current_user)):
    """Dependency that ensures the user has super_admin access."""
    if not current_user.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin access required",
        )
    return current_user
