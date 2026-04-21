"""
Pydantic request/response models for LendFlow API.
"""

import re
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator


# ─── Auth ──────────────────────────────────────

class AuthUser(BaseModel):
    username: str
    user_category: str


class Login(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)  # email or username
    password: str = Field(..., min_length=1, max_length=128)


class UserCreate(BaseModel):
    username: Optional[str] = Field(None, max_length=100)
    email: str = Field(..., max_length=255)
    full_name: Optional[str] = Field(None, max_length=200)
    phone_number: Optional[str] = Field(None, max_length=20)
    password: str = Field(..., min_length=8, max_length=128)
    nin: Optional[str] = Field(None, max_length=50)
    account_type: Optional[str] = "individual"
    role: Optional[str] = "borrower"  # borrower or lender

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


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    bio: Optional[str] = None
    profile_pic: Optional[str] = None
    nin: Optional[str] = None
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    account_type: Optional[str] = None
    fcm_token: Optional[str] = None
    two_factor_enabled: Optional[bool] = None


class ResetPasswordModel(BaseModel):
    email: str = Field(..., max_length=255)
    new_password: str = Field(..., min_length=8, max_length=128)
    access_token: Optional[str] = None


class ChangePasswordModel(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class SendPasswordResetCodeModel(BaseModel):
    email: str


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


# ─── Wallet ───────────────────────────────────

class WalletSetupModel(BaseModel):
    pin: str = Field(..., min_length=4, max_length=6)


class WalletDepositModel(BaseModel):
    amount: float = Field(..., ge=1000)
    phone_number: Optional[str] = None


class WalletWithdrawModel(BaseModel):
    amount: float = Field(..., ge=1000)
    phone_number: str


class WalletTransferModel(BaseModel):
    recipient_identifier: str
    amount: float = Field(..., ge=1000)


class RepaymentCreate(BaseModel):
    loan_id: str
    amount: float
    payment_method: str = "wallet"


# ─── Notifications ────────────────────────────

class NotificationSettingsUpdate(BaseModel):
    push_enabled: Optional[bool] = None
    email_enabled: Optional[bool] = None


# ─── Admin ────────────────────────────────────

class AdminUserStatusUpdate(BaseModel):
    is_active: bool


class AdminRoleUpdate(BaseModel):
    role: str  # borrower, lender, admin


class PlatformSettingUpdate(BaseModel):
    value: str
