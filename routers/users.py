"""
Users router — profile management.
"""

import os
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from config import BASE_URL
from database.tables import User, KYCDocument
from helpers import generateUniqueId
from repository.auth_repo import _audit
from repository.dependencies import get_db, current_active_user
from repository.models import UserUpdate, PushTokenUpdate
from repository.user_repo import UserRepo

router = APIRouter(prefix="/users", tags=["Users"])

# The standard identity-verification document set — national_id and passport
# are alternatives (either satisfies "government ID"), profile_photo is a
# selfie for identity matching, proof_of_address is optional supporting
# evidence. Kept separate from LoanDocument, which is per-application
# paperwork rather than account-level identity verification.
KYC_DOCUMENT_TYPES = {"national_id", "passport", "profile_photo", "proof_of_address"}
MAX_KYC_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
ALLOWED_KYC_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


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


@router.post("/me/kyc-documents")
async def upload_kyc_document(
    document_type: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Uploads (or replaces) one of the account's KYC documents. Uploading
    doesn't change kyc_status by itself — an admin still has to review and
    approve/reject via PATCH /admin/users/{username}/kyc."""
    if document_type not in KYC_DOCUMENT_TYPES:
        raise HTTPException(status_code=400, detail=f"document_type must be one of {sorted(KYC_DOCUMENT_TYPES)}")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_KYC_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext or 'unknown'}")

    contents = await file.read()
    if len(contents) > MAX_KYC_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File exceeds 10MB limit")

    os.makedirs("uploads", exist_ok=True)
    stored_name = f"{generateUniqueId(20)}{ext}"
    with open(os.path.join("uploads", stored_name), "wb") as f:
        f.write(contents)

    # Replace any existing upload of the same type rather than piling up —
    # re-uploading also resets it to unverified so a stale approval can't
    # silently carry over to a new file.
    existing = db.query(KYCDocument).filter(
        KYCDocument.user_id == user.id, KYCDocument.document_type == document_type
    ).first()
    if existing:
        existing.file_url = f"{BASE_URL}/uploads/{stored_name}"
        existing.file_name = file.filename
        existing.verified = False
        doc = existing
    else:
        doc = KYCDocument(
            user_id=user.id,
            document_type=document_type,
            file_url=f"{BASE_URL}/uploads/{stored_name}",
            file_name=file.filename,
        )
        db.add(doc)

    _audit(db, "kyc_document_uploaded", username=user.username, user_id=user.id,
           resource_type="kyc_document", details={"document_type": document_type})
    db.commit()
    db.refresh(doc)

    return {
        "status": 200,
        "message": "Document uploaded",
        "document": {
            "id": doc.id,
            "document_type": doc.document_type,
            "file_url": doc.file_url,
            "file_name": doc.file_name,
            "verified": doc.verified,
        },
    }


@router.get("/me/kyc-documents")
async def list_my_kyc_documents(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    docs = db.query(KYCDocument).filter(KYCDocument.user_id == user.id).all()
    return {
        "documents": [
            {
                "id": d.id,
                "document_type": d.document_type,
                "file_url": d.file_url,
                "file_name": d.file_name,
                "verified": d.verified,
            }
            for d in docs
        ]
    }


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
