"""
Role-based access control – follows kumpi_api pattern.
"""

from fastapi import Depends, HTTPException, status
from repository.dependencies import get_current_user
from repository.models import AuthUser


def require_roles(allowed: list[str]):
    """FastAPI dependency that restricts access to users with the specified roles."""

    def _check(current_user: AuthUser = Depends(get_current_user)):
        category = (current_user.user_category or "").lower()
        # Admins always permitted
        if "admin" in category:
            return current_user
        if category not in [r.lower() for r in allowed]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized for this resource",
            )
        return current_user

    return _check


def require_admin(current_user: AuthUser = Depends(get_current_user)):
    """Dependency that ensures the user is an admin or super_admin."""
    category = (current_user.user_category or "").lower()
    if "admin" not in category:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user
