"""
Pydantic request/response models for Mpola API.
"""

import re
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator, model_validator

# CustomDocumentResponse.label (database/tables.py) is a VARCHAR(255) —
# a required_documents label longer than that would get truncated (or
# rejected, depending on SQL mode) when the borrower's fulfillment tries to
# key off the exact same string, permanently mismatching and leaving a
# real submission stuck "unsatisfied" forever. Reject it here instead,
# where every caller (not just the frontends that happen to add a
# maxLength) is covered.
MAX_DOCUMENT_LABEL_LENGTH = 255


def _validate_document_labels(labels: list[str]) -> list[str]:
    for label in labels:
        if len(label) > MAX_DOCUMENT_LABEL_LENGTH:
            raise ValueError(
                f"Document label too long (max {MAX_DOCUMENT_LABEL_LENGTH} characters): "
                f"{label[:50]}..."
            )
    return labels


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
    # Required click-wrap acceptance of the Platform Terms, Privacy Policy,
    # and role-specific Code of Conduct (the "agreement") shown right above
    # the submit button on both frontends — stamped onto SignupDraft/User as
    # terms_accepted_at so admins can see it during KYC review, not just a
    # client-side UI gate.
    agree_to_terms: bool = Field(...)

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

    @field_validator('agree_to_terms')
    @classmethod
    def validate_agree_to_terms(cls, v: bool) -> bool:
        if not v:
            raise ValueError('You must agree to the Terms of Service, Privacy Policy, and Code of Conduct to register')
        return v


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
    amount: float = Field(..., ge=1000, le=50000000)
    # Exactly one of these two — duration (months, multi-instalment) for a
    # standard loan, duration_days (single bullet repayment, interest
    # prorated from the monthly rate) for a short-term "emergency" loan.
    # See check_duration below and routers/loans.py.
    duration: Optional[int] = Field(None, ge=1, le=24)
    duration_days: Optional[int] = Field(None, ge=1, le=29)
    loan_type: str  # personal, business, education, agricultural, emergency
    purpose: Optional[str] = None
    max_interest_rate: Optional[float] = Field(None, ge=0.1, le=25)  # borrower's optional cap, %/month
    valid_until: Optional[datetime] = None  # borrower's optional urgency cap — None means it never expires

    @model_validator(mode="after")
    def check_duration(self):
        if (self.duration is None) == (self.duration_days is None):
            raise ValueError("Provide exactly one of duration (months) or duration_days (1-29, emergency loan)")
        return self


class LoanApplicationUpdate(BaseModel):
    """Borrower edits their own application — only while it's still
    awaiting_guarantors/pending (see PUT /loans/applications/{id})."""
    amount: Optional[float] = Field(None, ge=1000, le=50000000)
    duration: Optional[int] = Field(None, ge=1, le=24)
    duration_days: Optional[int] = Field(None, ge=1, le=29)
    loan_type: Optional[str] = None
    purpose: Optional[str] = None
    max_interest_rate: Optional[float] = Field(None, ge=0.1, le=25)
    valid_until: Optional[datetime] = None

    @model_validator(mode="after")
    def check_duration(self):
        # Both are Optional here (partial update) — only reject the
        # nonsensical case where a caller tries to set both in one request;
        # setting neither (unchanged) or just one (switching modes) is fine.
        if self.duration is not None and self.duration_days is not None:
            raise ValueError("Provide at most one of duration (months) or duration_days")
        return self


class GuarantorAttach(BaseModel):
    guarantor_user_ids: list[str] = Field(..., min_length=2, max_length=2)


class GuarantorRespond(BaseModel):
    status: str  # accepted or declined


class GuarantorReplace(BaseModel):
    new_guarantor_user_id: str


class DocumentUpload(BaseModel):
    document_type: str
    file_url: str
    file_name: Optional[str] = None


# ─── Loan Offers ──────────────────────────────

class LoanOfferCreate(BaseModel):
    application_id: str
    amount: float = Field(..., ge=1000)
    interest_rate: float = Field(..., ge=0.1, le=25)
    # Exactly one — see LoanApplicationCreate.check_duration for why. A
    # lender can counter an emergency (days) request with a month-based
    # offer or vice versa; the two don't have to match the application's own.
    duration: Optional[int] = Field(None, ge=1, le=36)
    duration_days: Optional[int] = Field(None, ge=1, le=29)
    required_documents: list[str] = Field(default_factory=list)

    @field_validator("required_documents")
    @classmethod
    def check_document_labels(cls, v):
        return _validate_document_labels(v)

    @model_validator(mode="after")
    def check_duration(self):
        if (self.duration is None) == (self.duration_days is None):
            raise ValueError("Provide exactly one of duration (months) or duration_days (1-29)")
        return self


class LoanOfferUpdate(BaseModel):
    status: str  # accepted, declined
    # Optional note the borrower can leave for the lender when accepting —
    # e.g. context on a custom document, or anything worth flagging before
    # the lender approves disbursement. Only meaningful when status=accepted.
    note: Optional[str] = None


class LenderOfferTemplateCreate(BaseModel):
    max_amount: float = Field(..., ge=1000)
    min_amount: float = Field(..., ge=0)
    interest_rate: float = Field(..., ge=0.1, le=25)
    max_duration: Optional[int] = Field(None, ge=1, le=36)
    # A day-based standing offer, matched against "emergency" (duration_days)
    # applications the same way max_duration matches month-based ones —
    # exactly one of the two term shapes is ever set. See _template_matches.
    max_duration_days: Optional[int] = Field(None, ge=1, le=29)
    accepted_loan_types: list[str] = Field(default_factory=list)
    required_documents: list[str] = Field(default_factory=list)
    description: Optional[str] = None
    valid_until: Optional[datetime] = None
    max_concurrent_loans: Optional[int] = None
    is_draft: bool = False

    @field_validator("required_documents")
    @classmethod
    def check_document_labels(cls, v):
        return _validate_document_labels(v)

    @model_validator(mode="after")
    def check_amount_range(self):
        # Both fields are always present on create (neither is Optional),
        # so this alone is authoritative here — no template row to merge
        # against yet, unlike the Update model below.
        if self.min_amount >= self.max_amount:
            raise ValueError("Min loan amount must be less than max loan amount")
        return self

    @model_validator(mode="after")
    def check_duration(self):
        if (self.max_duration is None) == (self.max_duration_days is None):
            raise ValueError("Set exactly one of max_duration (months) or max_duration_days (1-29 days)")
        return self


class LenderOfferTemplateUpdate(BaseModel):
    max_amount: Optional[float] = Field(None, ge=1000)
    min_amount: Optional[float] = Field(None, ge=0)
    interest_rate: Optional[float] = Field(None, ge=0.1, le=25)
    max_duration: Optional[int] = Field(None, ge=1, le=36)
    max_duration_days: Optional[int] = Field(None, ge=1, le=29)
    accepted_loan_types: Optional[list[str]] = None
    required_documents: Optional[list[str]] = None
    description: Optional[str] = None
    valid_until: Optional[datetime] = None
    max_concurrent_loans: Optional[int] = None

    @field_validator("required_documents")
    @classmethod
    def check_document_labels(cls, v):
        return v if v is None else _validate_document_labels(v)

    @model_validator(mode="after")
    def check_duration_switch(self):
        # Partial-update friendly, like LoanApplicationUpdate — only rejects
        # when BOTH are sent non-None in the same request. Switching term
        # shape requires the caller to explicitly null out the other one
        # (the frontend always sends both together when switching modes).
        if self.max_duration is not None and self.max_duration_days is not None:
            raise ValueError("Set at most one of max_duration or max_duration_days in a single update")
        return self

    @model_validator(mode="after")
    def check_amount_range(self):
        # Only catches the case where both are changed in the same request —
        # this is a partial update, so if just one of the two is being
        # edited, only the router (which has the existing template row to
        # merge against) can know whether the resulting range is still
        # valid. See update_offer_template in routers/loans.py.
        if self.min_amount is not None and self.max_amount is not None and self.min_amount >= self.max_amount:
            raise ValueError("Min loan amount must be less than max loan amount")
        return self


class LenderOfferTemplateExpiryUpdate(BaseModel):
    valid_until: Optional[datetime] = None  # null clears the expiry (no longer time-limited)


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


class WalletCardDepositConfirmModel(BaseModel):
    """Shape of UPG's server-to-server webhook payload for a card deposit —
    see unified_payment_gateway app/api/v1/webhooks.py's _handle_charge_webhook."""
    request_reference: str
    amount: float
    provider_ref: str
    status: str  # "success" or "failed"


class WalletBankWithdrawInitiateModel(BaseModel):
    amount: float = Field(..., ge=1000)
    account_bank: str
    account_number: str
    beneficiary_name: str = Field(..., min_length=1)
    narration: Optional[str] = None


class RepaymentCreate(BaseModel):
    loan_id: str
    amount: float = Field(..., gt=0)
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
    # Admin can settle money as part of resolving, same mechanism as the
    # party-to-party propose/accept flow but under admin authority — no
    # counterparty consent needed. payer must be "filer" or "respondent".
    settlement_amount: Optional[float] = Field(None, gt=0)
    settlement_payer: Optional[str] = None


class DisputeMessageCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class DisputeProposalCreate(BaseModel):
    note: str = Field(..., min_length=1, max_length=2000)
    settlement_amount: Optional[float] = Field(None, gt=0)
    # Who would pay if this proposal is accepted — "self" (the proposer) or
    # "other" (the counterparty). Irrelevant if settlement_amount is unset.
    payer: str = "self"


class DisputeProposalRespond(BaseModel):
    accept: bool


class WalletAdjustmentModel(BaseModel):
    # Positive = credit the user, negative = debit — one field covers both
    # directions so the audit trail reads as a single signed delta.
    amount: float
    reason: str = Field(..., min_length=3, max_length=500)

    @field_validator('amount')
    @classmethod
    def amount_not_zero(cls, v: float) -> float:
        # Field(..., ne=0) looks like it should do this but Pydantic v2 has
        # no such numeric constraint — 'ne' is silently accepted as an
        # unrecognized kwarg and never validated, letting amount=0 through
        # to create a no-op WalletTransaction. Caught via live-testing this
        # exact endpoint, not by inspection.
        if v == 0:
            raise ValueError("amount must not be zero")
        return v


class WalletFreezeUpdate(BaseModel):
    reason: Optional[str] = None


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
    reason: Optional[str] = None  # required in practice when verified=False


class AdminAccessUpdate(BaseModel):
    """Grants/revokes admin access WITHOUT touching the account's borrower/lender
    portal role — lets an existing lender or borrower also become an admin."""
    is_admin: bool
    is_super_admin: bool = False


class PlatformSettingUpdate(BaseModel):
    value: str


class WebPushSubscribe(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


class WebPushUnsubscribe(BaseModel):
    endpoint: str
