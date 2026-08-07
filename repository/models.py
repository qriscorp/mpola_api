"""
Pydantic request/response models for Mpola API.
"""

import re
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator


# ─── Auth ──────────────────────────────────────

class AuthUser(BaseModel):
    username: str
    user_category: str
    is_admin: bool = False
    is_super_admin: bool = False


class Login(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)  # email or username
    password: str = Field(..., min_length=1, max_length=128)
    portal: Optional[str] = Field(None, description="borrower or lender")

    @field_validator('portal')
    @classmethod
    def validate_portal(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        portal = v.lower().strip()
        if portal not in {'borrower', 'lender'}:
            raise ValueError('portal must be either borrower or lender')
        return portal


class UserCreate(BaseModel):
    username: Optional[str] = Field(None, max_length=100)
    email: str = Field(..., max_length=255)
    full_name: Optional[str] = Field(None, max_length=200)
    phone_number: Optional[str] = Field(None, max_length=20)
    password: str = Field(..., min_length=8, max_length=128)
    nin: Optional[str] = Field(None, max_length=50)
    account_type: Optional[str] = "individual"
    role: Optional[str] = "borrower"  # borrower or lender
    referred_by_code: Optional[str] = Field(None, max_length=20)

    @field_validator('email')
    @classmethod
    def validate_email(cls, v: str) -> str:
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(pattern, v):
            raise ValueError('Invalid email format')
        return v.lower().strip()

    @field_validator('username')
    @classmethod
    def validate_username(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = re.sub(r'[^a-zA-Z0-9_.]', '', v)
            if len(v) < 3:
                raise ValueError('Username must be at least 3 characters')
        return v

    @field_validator('role')
    @classmethod
    def validate_role(cls, v: Optional[str]) -> Optional[str]:
        """Prevent role escalation at input level."""
        allowed = {'borrower', 'lender'}
        if v and v.lower() not in allowed:
            return 'borrower'
        return v.lower() if v else 'borrower'

    class Config:
        from_attributes = True


class RegisterStart(BaseModel):
    email: str = Field(..., max_length=255)
    full_name: Optional[str] = Field(None, max_length=200)
    phone_number: Optional[str] = Field(None, max_length=20)
    password: str = Field(..., min_length=8, max_length=128)
    nin: Optional[str] = Field(None, max_length=50)
    account_type: Optional[str] = "individual"
    role: Optional[str] = "borrower"  # borrower or lender
    referred_by_code: Optional[str] = Field(None, max_length=20)

    @field_validator('email')
    @classmethod
    def validate_email(cls, v: str) -> str:
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(pattern, v):
            raise ValueError('Invalid email format')
        return v.lower().strip()

    @field_validator('role')
    @classmethod
    def validate_role(cls, v: Optional[str]) -> Optional[str]:
        allowed = {'borrower', 'lender'}
        if v and v.lower() not in allowed:
            return 'borrower'
        return v.lower() if v else 'borrower'


class SignupDraftRequest(BaseModel):
    draft_id: str


class SignupDraftPhoneRequest(BaseModel):
    draft_id: str
    phone_number: str


class SignupDraftVerifyRequest(BaseModel):
    draft_id: str
    code: str


class SignupDraftVerifyPhoneRequest(BaseModel):
    draft_id: str
    phone_number: str
    code: str


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    bio: Optional[str] = None
    profile_pic: Optional[str] = None
    nin: Optional[str] = None
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    account_type: Optional[str] = None
    two_factor_enabled: Optional[bool] = None
    notif_new_application: Optional[bool] = None
    notif_repayment_received: Optional[bool] = None
    notif_loan_overdue: Optional[bool] = None
    notif_portfolio_digest: Optional[bool] = None
    notif_login_alerts: Optional[bool] = None


class PushTokenUpdate(BaseModel):
    push_token: Optional[str] = None  # Expo push token; null clears it (e.g. on sign-out)


class ResetPasswordModel(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=128)
    access_token: str = Field(...)


class ChangePasswordModel(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class SendPasswordResetCodeModel(BaseModel):
    email: str
    phone_number: str  # both must match the SAME account — stronger than either alone
    portal: str | None = None  # "borrower" | "lender" — which reset page this came from


class VerifyPasswordResetCodeModel(BaseModel):
    identifier: str  # same email or phone used in send step
    code: str


class SendLoginPhoneOTPModel(BaseModel):
    phone_number: str


class VerifyLogin2FAModel(BaseModel):
    username: str
    code: str = Field(..., min_length=4, max_length=8)


class VerifyLoginPhoneOTPModel(BaseModel):
    phone_number: str
    code: str
    portal: Optional[str] = Field(None, description="borrower or lender")

    @field_validator('portal')
    @classmethod
    def validate_portal(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        portal = v.lower().strip()
        if portal not in {'borrower', 'lender'}:
            raise ValueError('portal must be either borrower or lender')
        return portal


class SendOTPModel(BaseModel):
    username: str


class SendPhoneOTPModel(BaseModel):
    username: str
    phone_number: str


class VerifyOTPModel(BaseModel):
    username: str
    code: str


class VerifyPhoneOTPModel(BaseModel):
    username: str
    phone_number: str
    code: str


# ─── Loan Application ─────────────────────────

class LoanApplicationCreate(BaseModel):
    amount: float = Field(..., ge=100000, le=50000000)
    duration: int = Field(..., ge=3, le=24)
    loan_type: str  # personal, business, education, agricultural, emergency
    purpose: Optional[str] = None


class LoanApplicationUpdate(BaseModel):
    status: Optional[str] = None  # approved, rejected


class GuarantorCreate(BaseModel):
    name: str
    phone: str
    relationship_type: Optional[str] = None


class GuarantorRespond(BaseModel):
    status: str  # accepted or declined


class DocumentUpload(BaseModel):
    document_type: str
    file_url: str
    file_name: Optional[str] = None


# ─── Loan Offers ──────────────────────────────

class LoanOfferCreate(BaseModel):
    application_id: str
    amount: float = Field(..., ge=100000)
    interest_rate: float = Field(..., ge=0.1, le=25)
    duration: int = Field(..., ge=1, le=36)


class LoanOfferUpdate(BaseModel):
    status: str  # accepted, declined


class LenderOfferTemplateCreate(BaseModel):
    max_amount: float = Field(..., ge=100000)
    min_amount: float = Field(..., ge=0)
    interest_rate: float = Field(..., ge=0.1, le=25)
    max_duration: int = Field(..., ge=1, le=36)
    accepted_loan_types: list[str] = Field(default_factory=list)
    required_documents: list[str] = Field(default_factory=list)
    description: Optional[str] = None
    valid_until: Optional[datetime] = None
    max_concurrent_loans: Optional[int] = None
    is_draft: bool = False


# ─── Wallet ───────────────────────────────────

class WalletSetupModel(BaseModel):
    pin: str = Field(..., min_length=4, max_length=6)


class WalletDepositModel(BaseModel):
    amount: float = Field(..., ge=1000)
    phone_number: Optional[str] = None
    carrier: Optional[str] = None  # MTN or AIRTEL; auto-detected if omitted


class WalletWithdrawModel(BaseModel):
    amount: float = Field(..., ge=1000)
    phone_number: str
    carrier: Optional[str] = None  # MTN or AIRTEL; auto-detected if omitted


class WalletTransferModel(BaseModel):
    recipient_identifier: str
    amount: float = Field(..., ge=1000)


class WalletCardDepositInitiateModel(BaseModel):
    amount: float = Field(..., ge=1000)
    redirect_url: str


class WalletBankWithdrawInitiateModel(BaseModel):
    amount: float = Field(..., ge=1000)
    account_bank: str
    account_number: str
    beneficiary_name: str = Field(..., min_length=1)
    narration: Optional[str] = None


class RepaymentCreate(BaseModel):
    loan_id: str
    amount: float
    payment_method: str = "wallet"  # wallet or mobile_money
    phone_number: Optional[str] = None  # required when payment_method=mobile_money
    carrier: Optional[str] = None       # MTN or AIRTEL; auto-detected if omitted


# ─── Notifications ────────────────────────────

class NotificationSettingsUpdate(BaseModel):
    push_enabled: Optional[bool] = None
    email_enabled: Optional[bool] = None


# ─── Disputes ─────────────────────────────────

class DisputeCreate(BaseModel):
    category: str = Field(..., max_length=50)  # payment, loan_terms, fraud, disbursement, other
    description: str = Field(..., min_length=10, max_length=4000)
    loan_id: Optional[str] = None


class DisputeResolve(BaseModel):
    status: str  # investigating, resolved, rejected
    resolution_note: Optional[str] = None


# ─── Support tickets ───────────────────────────

class SupportTicketCreate(BaseModel):
    subject: str = Field(..., max_length=255)
    category: str = Field("general", max_length=50)
    message: str = Field(..., min_length=5, max_length=4000)


class SupportMessageCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class SupportTicketStatusUpdate(BaseModel):
    status: str  # open, in_progress, resolved, closed


# ─── Admin ────────────────────────────────────

class AdminUserStatusUpdate(BaseModel):
    is_active: bool


class AdminRoleUpdate(BaseModel):
    role: str  # borrower, lender, admin


class KYCReviewUpdate(BaseModel):
    status: str  # verified, rejected
    note: Optional[str] = None


class DocumentVerifyUpdate(BaseModel):
    verified: bool


class AdminAccessUpdate(BaseModel):
    """Grants/revokes admin access WITHOUT touching the account's borrower/lender
    portal role — lets an existing lender or borrower also become an admin."""
    is_admin: bool
    is_super_admin: bool = False


class PlatformSettingUpdate(BaseModel):
    value: str
