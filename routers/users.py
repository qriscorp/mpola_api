"""
Users router — profile management.
"""

import os
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import BASE_URL
from database.tables import (
    User, KYCDocument, BorrowerDocument, Wallet, WalletTransaction, Loan,
    LoanApplication, Repayment, SupportTicket, DeactivatedAccount,
)
from helpers import generateUniqueId, normalizePhoneNumber, safe_isoformat, DOCUMENT_LABEL_MAP
from repository.auth_repo import _audit, verify_password
from repository.dependencies import get_db, current_active_user
from repository.models import UserUpdate, PushTokenUpdate, SelfDeactivateModel
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

# Once a specific document is individually verified by admin, THAT document
# is locked against re-upload for this long — prevents a verified identity
# document from being quietly swapped out — after which the holder can
# refresh an aging document (e.g. an expiring ID) on their own without
# needing to fail/rejoin KYC first. Applies per-document: an already-
# verified document is locked immediately even while the rest of the
# account is still pending review (see _kyc_doc_response/upload_kyc_document).
KYC_REVERIFICATION_LOCK_DAYS = 730

# The non-identity half of DOCUMENT_LABEL_MAP (helpers.py) — supporting
# financial/business documents a lender's standing offer might ask for.
# Derived from the same map a lender's required_documents resolves against
# (routers/loans.py), so this can never drift out of sync with what's
# actually satisfiable.
BORROWER_DOCUMENT_TYPES = {
    type_key for source, type_key in DOCUMENT_LABEL_MAP.values() if source == "borrower_doc"
}
MAX_BORROWER_DOCUMENT_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
ALLOWED_BORROWER_DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


def _kyc_doc_response(d: KYCDocument) -> dict:
    locked_until = None
    if d.verified and d.verified_at:
        until = d.verified_at + timedelta(days=KYC_REVERIFICATION_LOCK_DAYS)
        if datetime.utcnow() < until:
            locked_until = until.date().isoformat()
    return {
        "id": d.id,
        "document_type": d.document_type,
        "file_url": d.file_url,
        "file_name": d.file_name,
        "verified": d.verified,
        "rejection_reason": d.rejection_reason,
        "locked_until": locked_until,
    }


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


@router.post("/me/sign-lender-agreement")
async def sign_lender_agreement(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """A lender (re-)accepting the Platform Terms/Privacy Policy/Lender Code
    of Conduct after registration — this is what issues their Mpola Licence
    the first time (for accounts verified before this field existed) and
    what "renewing" means afterwards (see LENDER_LICENCE_VALIDITY_DAYS in
    repository/user_repo.py — the licence's 2-year clock IS this
    timestamp). Requires KYC to already be verified; signing alone doesn't
    grant a licence, admin approval does."""
    if user.role != "lender":
        raise HTTPException(status_code=400, detail="Only lenders have a Mpola Licence")
    if user.kyc_status != "verified":
        raise HTTPException(status_code=400, detail="Complete KYC verification before signing the lender agreement")

    user.terms_accepted_at = datetime.utcnow()
    _audit(db, "lender_agreement_signed", username=user.username, user_id=user.id,
           resource_type="user", resource_id=user.id)
    db.commit()
    return UserRepo.get_user_by_username(db, user.username)


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

    # Locked per-document, not per-account — a document an admin already
    # verified is locked from the moment it's verified, even while the rest
    # of the account is still pending (kyc_status only reaches "verified"
    # once every required document is). Anything not yet individually
    # verified (pending review, or rejected) can always be replaced.
    existing_doc = db.query(KYCDocument).filter(
        KYCDocument.user_id == user.id, KYCDocument.document_type == document_type
    ).first()
    if existing_doc and existing_doc.verified and existing_doc.verified_at:
        locked_until = existing_doc.verified_at + timedelta(days=KYC_REVERIFICATION_LOCK_DAYS)
        if datetime.utcnow() < locked_until:
            raise HTTPException(
                status_code=400,
                detail=f"This document is verified — locked until {locked_until.date().isoformat()}. "
                       "Contact support if you need to update it sooner.",
            )

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
    if existing_doc:
        existing_doc.file_url = f"{BASE_URL}/uploads/{stored_name}"
        existing_doc.file_name = file.filename
        existing_doc.verified = False
        existing_doc.verified_at = None
        existing_doc.rejection_reason = None
        doc = existing_doc
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
        "document": _kyc_doc_response(doc),
    }


@router.get("/me/kyc-documents")
async def list_my_kyc_documents(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    docs = db.query(KYCDocument).filter(KYCDocument.user_id == user.id).all()
    return {"documents": [_kyc_doc_response(d) for d in docs]}


@router.post("/me/documents")
async def upload_borrower_document(
    document_type: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Uploads (or replaces) one of the account's reusable supporting
    documents — bank statement, payslip/business proof, land title, URA
    TIN. Account-wide, not per-loan: once uploaded here, it satisfies every
    current and future lender offer that asks for the same thing (see
    DOCUMENT_LABEL_MAP / _required_documents_status in routers/loans.py)."""
    if document_type not in BORROWER_DOCUMENT_TYPES:
        raise HTTPException(status_code=400, detail=f"document_type must be one of {sorted(BORROWER_DOCUMENT_TYPES)}")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_BORROWER_DOCUMENT_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext or 'unknown'}")

    contents = await file.read()
    if len(contents) > MAX_BORROWER_DOCUMENT_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File exceeds 10MB limit")

    os.makedirs("uploads", exist_ok=True)
    stored_name = f"{generateUniqueId(20)}{ext}"
    with open(os.path.join("uploads", stored_name), "wb") as f:
        f.write(contents)

    # Replace any existing upload of the same type rather than piling up.
    existing = db.query(BorrowerDocument).filter(
        BorrowerDocument.user_id == user.id, BorrowerDocument.document_type == document_type
    ).first()
    if existing:
        existing.file_url = f"{BASE_URL}/uploads/{stored_name}"
        existing.file_name = file.filename
        existing.verified = False
        doc = existing
    else:
        doc = BorrowerDocument(
            user_id=user.id,
            document_type=document_type,
            file_url=f"{BASE_URL}/uploads/{stored_name}",
            file_name=file.filename,
        )
        db.add(doc)

    _audit(db, "borrower_document_uploaded", username=user.username, user_id=user.id,
           resource_type="borrower_document", details={"document_type": document_type})
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


@router.get("/me/documents")
async def list_my_borrower_documents(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    docs = db.query(BorrowerDocument).filter(BorrowerDocument.user_id == user.id).all()
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


@router.get("/me/export")
async def export_my_data(
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Self-service data export — a JSON snapshot of everything Mpola holds
    tied to this account: profile, KYC/supporting documents (metadata, not
    the files themselves), wallet + transaction history, every loan the
    user is on as borrower or lender, applications filed, and support
    tickets. Read-only, no side effects."""
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    transactions = []
    if wallet:
        transactions = db.query(WalletTransaction).filter(WalletTransaction.wallet_id == wallet.id).all()

    loans_as_borrower = db.query(Loan).filter(Loan.borrower_id == user.id).all()
    loans_as_lender = db.query(Loan).filter(Loan.lender_id == user.id).all()
    applications = db.query(LoanApplication).filter(LoanApplication.borrower_id == user.id).all()
    kyc_docs = db.query(KYCDocument).filter(KYCDocument.user_id == user.id).all()
    borrower_docs = db.query(BorrowerDocument).filter(BorrowerDocument.user_id == user.id).all()
    tickets = db.query(SupportTicket).filter(SupportTicket.user_id == user.id).all()

    def _loan_summary(l: Loan) -> dict:
        return {
            "id": l.id,
            "role": "borrower" if l.borrower_id == user.id else "lender",
            "amount": l.amount,
            "interest_rate": l.interest_rate,
            "status": l.status,
            "total_paid": l.total_paid,
            "total_repayable": l.total_repayable,
            "disbursed_at": safe_isoformat(l.disbursed_at),
            "created_at": safe_isoformat(l.created_at),
        }

    return {
        "exported_at": safe_isoformat(datetime.utcnow()),
        "profile": {
            "username": user.username,
            "full_name": user.full_name,
            "email": user.email,
            "phone_number": user.phone_number,
            "role": user.role,
            "account_type": user.account_type,
            "nin": user.nin,
            "kyc_status": user.kyc_status,
            "created_at": safe_isoformat(user.created_at),
        },
        "kyc_documents": [
            {"document_type": d.document_type, "verified": d.verified, "uploaded_at": safe_isoformat(d.created_at)}
            for d in kyc_docs
        ],
        "supporting_documents": [
            {"document_type": d.document_type, "verified": d.verified, "uploaded_at": safe_isoformat(d.created_at)}
            for d in borrower_docs
        ],
        "wallet": {
            "balance": wallet.balance if wallet else 0,
            "currency": wallet.currency if wallet else "UGX",
            "transactions": [
                {
                    "amount": t.amount,
                    "type": t.type,
                    "direction": t.direction,
                    "status": t.status,
                    "description": t.description,
                    "created_at": safe_isoformat(t.created_at),
                }
                for t in transactions
            ],
        },
        "loans": [_loan_summary(l) for l in [*loans_as_borrower, *loans_as_lender]],
        "applications": [
            {
                "id": a.id,
                "amount": a.amount,
                "loan_type": a.loan_type,
                "status": a.status,
                "created_at": safe_isoformat(a.created_at),
            }
            for a in applications
        ],
        "support_tickets": [
            {"subject": t.subject, "category": t.category, "status": t.status, "created_at": safe_isoformat(t.created_at)}
            for t in tickets
        ],
    }


@router.post("/me/deactivate")
async def deactivate_my_account(
    data: SelfDeactivateModel,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Self-service account deactivation — same effect as an admin's
    PUT /admin/users/{username}/deactivate (soft delete: wallet purged, a
    DeactivatedAccount stub kept for 30 days, the User row itself deleted),
    but a self-service action doesn't have an admin's judgment behind it,
    so it adds the guardrails that matter most: confirmed password, no
    money left in the wallet, and no loan currently in flight on either
    side of the platform (both would otherwise be silently cascade-deleted
    along with the account, in whichever wallet/loan owns them)."""
    if user.has_admin_access:
        raise HTTPException(status_code=400, detail="Admin accounts can't self-deactivate — ask another admin.")
    if not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Incorrect password")

    active_loan = db.query(Loan).filter(
        (Loan.borrower_id == user.id) | (Loan.lender_id == user.id),
        Loan.status.in_(["pending_disbursement", "active", "overdue"]),
    ).first()
    if active_loan:
        raise HTTPException(
            status_code=400,
            detail="You have a loan in progress — it needs to be fully repaid or completed before you can deactivate your account.",
        )

    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if wallet:
        if wallet.is_frozen:
            raise HTTPException(status_code=400, detail="Your wallet is frozen — contact support before deactivating.")
        if wallet.balance and wallet.balance > 0:
            raise HTTPException(
                status_code=400,
                detail=f"Withdraw your wallet balance (UGX {wallet.balance:,.0f}) before deactivating your account.",
            )

    record = DeactivatedAccount(
        original_username=user.username,
        original_email=user.email,
        original_phone_number=user.phone_number,
        deactivated_by=user.username,
        scheduled_deletion_date=datetime.utcnow() + timedelta(days=30),
        reason=data.reason or "Self-service deactivation",
    )
    db.add(record)

    if wallet:
        db.query(WalletTransaction).filter(WalletTransaction.wallet_id == wallet.id).delete(synchronize_session=False)
        db.delete(wallet)

    _audit(db, "user_deactivated", username=user.username, user_id=user.id,
           resource_type="user", details={"target_user": user.username, "reason": data.reason, "self_service": True})
    db.delete(user)
    db.commit()

    return {"status": 200, "message": "Account deactivated. Your data will be purged in 30 days."}


@router.get("/search-guarantor-candidate")
async def search_guarantor_candidate(
    email: str = Query(...),
    phone_number: str = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Find a real Mpola account to invite as a guarantor — requires BOTH
    email and phone number to match the same account (mirrors the same
    two-factor pattern used for password-reset lookups in auth_repo.py),
    a stronger check than either field alone. Exact match only, no fuzzy
    search, and 404s rather than returning an empty/null result on no
    match — a single field alone should never reveal whether an account
    exists."""
    normalized_phone = normalizePhoneNumber(phone_number) or phone_number
    candidate = db.query(User).filter(
        func.lower(User.email) == email.lower().strip(),
        User.phone_number == normalized_phone,
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="No account found matching that email and phone number")
    if candidate.id == user.id:
        raise HTTPException(status_code=400, detail="You can't add yourself as a guarantor")

    return {
        "id": candidate.id,
        "username": candidate.username,
        "full_name": candidate.full_name,
        "role": candidate.role,
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
