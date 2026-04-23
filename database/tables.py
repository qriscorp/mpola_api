from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text, DateTime,
    ForeignKey, Enum, func,
)
from sqlalchemy.orm import declarative_base, relationship
from helpers import generateUniqueId

Base = declarative_base()


def _utc_now():
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


# ═══════════════════════════════════════
#  USERS
# ═══════════════════════════════════════

class User(Base, TimestampMixin):
    __tablename__ = "users"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    username = Column(String(100), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    phone_number = Column(String(20), unique=True, nullable=True, index=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(200), nullable=True)
    nin = Column(String(50), nullable=True)
    date_of_birth = Column(DateTime, nullable=True)
    gender = Column(String(20), nullable=True)
    profile_pic = Column(String(500), nullable=True)
    bio = Column(Text, nullable=True)
    account_type = Column(String(20), default="individual")  # individual, business, company
    role = Column(String(30), default="borrower")  # borrower, lender, admin, super_admin
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    is_phone_verified = Column(Boolean, default=False)
    is_kyc_verified = Column(Boolean, default=False)
    kyc_status = Column(String(20), default="pending")  # pending, verified, rejected
    credit_score = Column(Integer, default=0)
    fcm_token = Column(Text, nullable=True)
    # JWT refresh tokens can exceed 255 chars once claims/signature are included.
    refresh_token = Column(Text, nullable=True)
    refresh_token_expires_at = Column(DateTime, nullable=True)
    two_factor_enabled = Column(Boolean, default=False)

    # Relationships
    wallets = relationship("Wallet", back_populates="user", cascade="all, delete-orphan")
    loan_applications = relationship("LoanApplication", back_populates="borrower", foreign_keys="LoanApplication.borrower_id")
    offers_made = relationship("LoanOffer", back_populates="lender", foreign_keys="LoanOffer.lender_id")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")


class DeactivatedAccount(Base, TimestampMixin):
    __tablename__ = "deactivated_accounts"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    original_username = Column(String(100), nullable=False)
    original_email = Column(String(255), nullable=False)
    original_phone_number = Column(String(20), nullable=True)
    deactivated_by = Column(String(100), nullable=True)
    reason = Column(Text, nullable=True)
    scheduled_deletion_date = Column(DateTime, nullable=True)


class SignupDraft(Base, TimestampMixin):
    __tablename__ = "signup_drafts"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    username = Column(String(100), nullable=False, index=True)
    email = Column(String(255), nullable=False, index=True)
    phone_number = Column(String(20), nullable=True, index=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(200), nullable=True)
    nin = Column(String(50), nullable=True)
    account_type = Column(String(20), default="individual")
    role = Column(String(30), default="borrower")
    email_verified = Column(Boolean, default=False)
    phone_verified = Column(Boolean, default=False)
    is_completed = Column(Boolean, default=False)
    created_user_id = Column(String(50), nullable=True)
    expires_at = Column(DateTime, nullable=False)


# ═══════════════════════════════════════
#  OTP
# ═══════════════════════════════════════

class OTP(Base, TimestampMixin):
    __tablename__ = "otps"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    username = Column(String(100), nullable=False, index=True)
    phone_number = Column(String(20), nullable=True)
    code_hash = Column(String(255), nullable=False)  # bcrypt-hashed OTP code
    expires_at = Column(DateTime, nullable=False)  # OTP expiration (10 min)
    attempts = Column(Integer, default=0)  # brute-force protection (max 5)
    purpose = Column(String(30), default="verification")  # verification, password_reset, phone


class LoginAttempt(Base, TimestampMixin):
    """Track failed login attempts for account lockout."""
    __tablename__ = "login_attempts"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    identifier = Column(String(255), nullable=False, index=True)  # email or username
    ip_address = Column(String(50), nullable=True)
    success = Column(Boolean, default=False)


class AuditLog(Base, TimestampMixin):
    """Immutable audit trail for sensitive operations."""
    __tablename__ = "audit_logs"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    user_id = Column(String(50), nullable=True)
    username = Column(String(100), nullable=True)
    action = Column(String(100), nullable=False)  # login, register, password_change, role_change, suspend, etc.
    resource_type = Column(String(50), nullable=True)  # user, loan, wallet, etc.
    resource_id = Column(String(50), nullable=True)
    ip_address = Column(String(50), nullable=True)
    details = Column(Text, nullable=True)  # JSON string with extra context


# ═══════════════════════════════════════
#  WALLET
# ═══════════════════════════════════════

class Wallet(Base, TimestampMixin):
    __tablename__ = "wallets"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    user_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    balance = Column(Float, default=0.0)
    currency = Column(String(10), default="UGX")
    is_wallet_setup = Column(Boolean, default=False)
    pin_hash = Column(String(255), nullable=True)

    user = relationship("User", back_populates="wallets")
    transactions = relationship("WalletTransaction", back_populates="wallet", cascade="all, delete-orphan")


class WalletTransaction(Base, TimestampMixin):
    __tablename__ = "wallet_transactions"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    wallet_id = Column(String(50), ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Float, nullable=False)
    type = Column(String(30), nullable=False)  # deposit, withdrawal, repayment, disbursement, top_up
    status = Column(String(20), default="completed")  # pending, completed, failed
    description = Column(Text, nullable=True)
    reference = Column(String(100), nullable=True)
    counterparty = Column(String(100), nullable=True)

    wallet = relationship("Wallet", back_populates="transactions")


# ═══════════════════════════════════════
#  LOAN APPLICATION
# ═══════════════════════════════════════

class LoanApplication(Base, TimestampMixin):
    __tablename__ = "loan_applications"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    borrower_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reference_number = Column(String(50), unique=True, nullable=False)
    amount = Column(Float, nullable=False)
    duration = Column(Integer, nullable=False)  # months
    loan_type = Column(String(30), nullable=False)  # personal, business, education, agricultural, emergency
    purpose = Column(Text, nullable=True)
    status = Column(String(30), default="pending")  # pending, approved, rejected, funded, completed, defaulted
    monthly_payment = Column(Float, nullable=True)
    interest_rate = Column(Float, nullable=True)
    total_repayable = Column(Float, nullable=True)

    borrower = relationship("User", back_populates="loan_applications", foreign_keys=[borrower_id])
    offers = relationship("LoanOffer", back_populates="application", cascade="all, delete-orphan")
    documents = relationship("LoanDocument", back_populates="application", cascade="all, delete-orphan")
    guarantors = relationship("Guarantor", back_populates="application", cascade="all, delete-orphan")
    loan = relationship("Loan", back_populates="application", uselist=False)


class LoanDocument(Base, TimestampMixin):
    __tablename__ = "loan_documents"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    application_id = Column(String(50), ForeignKey("loan_applications.id", ondelete="CASCADE"), nullable=False)
    document_type = Column(String(50), nullable=False)  # national_id, proof_of_income, business_license, etc.
    file_url = Column(String(500), nullable=False)
    file_name = Column(String(255), nullable=True)
    verified = Column(Boolean, default=False)

    application = relationship("LoanApplication", back_populates="documents")


class Guarantor(Base, TimestampMixin):
    __tablename__ = "guarantors"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    application_id = Column(String(50), ForeignKey("loan_applications.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(200), nullable=False)
    phone = Column(String(20), nullable=False)
    relationship_type = Column(String(50), nullable=True)  # friend, family, colleague
    status = Column(String(20), default="pending")  # pending, accepted, declined

    application = relationship("LoanApplication", back_populates="guarantors")


# ═══════════════════════════════════════
#  LOAN OFFERS
# ═══════════════════════════════════════

class LoanOffer(Base, TimestampMixin):
    __tablename__ = "loan_offers"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    application_id = Column(String(50), ForeignKey("loan_applications.id", ondelete="CASCADE"), nullable=False)
    lender_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Float, nullable=False)
    interest_rate = Column(Float, nullable=False)
    duration = Column(Integer, nullable=False)  # months
    monthly_payment = Column(Float, nullable=True)
    total_repayable = Column(Float, nullable=True)
    status = Column(String(20), default="pending")  # pending, accepted, declined, expired

    application = relationship("LoanApplication", back_populates="offers")
    lender = relationship("User", back_populates="offers_made", foreign_keys=[lender_id])


# ═══════════════════════════════════════
#  ACTIVE LOAN (FUNDED)
# ═══════════════════════════════════════

class Loan(Base, TimestampMixin):
    __tablename__ = "loans"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    application_id = Column(String(50), ForeignKey("loan_applications.id", ondelete="SET NULL"), nullable=True)
    borrower_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    lender_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Float, nullable=False)
    interest_rate = Column(Float, nullable=False)
    duration = Column(Integer, nullable=False)
    monthly_payment = Column(Float, nullable=False)
    total_repayable = Column(Float, nullable=False)
    total_paid = Column(Float, default=0.0)
    paid_instalments = Column(Integer, default=0)
    total_instalments = Column(Integer, nullable=False)
    next_payment_date = Column(DateTime, nullable=True)
    next_payment_amount = Column(Float, nullable=True)
    status = Column(String(20), default="active")  # active, completed, overdue, defaulted
    disbursed_at = Column(DateTime, nullable=True)

    application = relationship("LoanApplication", back_populates="loan")
    borrower = relationship("User", foreign_keys=[borrower_id])
    lender_user = relationship("User", foreign_keys=[lender_id])
    repayments = relationship("Repayment", back_populates="loan", cascade="all, delete-orphan")


# ═══════════════════════════════════════
#  REPAYMENTS
# ═══════════════════════════════════════

class Repayment(Base, TimestampMixin):
    __tablename__ = "repayments"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    loan_id = Column(String(50), ForeignKey("loans.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Float, nullable=False)
    instalment_number = Column(Integer, nullable=False)
    status = Column(String(20), default="completed")  # completed, pending, late
    payment_method = Column(String(30), nullable=True)  # wallet, mobile_money
    transaction_id = Column(String(100), nullable=True)

    loan = relationship("Loan", back_populates="repayments")


# ═══════════════════════════════════════
#  NOTIFICATIONS
# ═══════════════════════════════════════

class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    user_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    type = Column(String(50), nullable=True)  # loan_offer, payment, approval, system
    is_read = Column(Boolean, default=False)
    data = Column(Text, nullable=True)  # JSON string for extra payload

    user = relationship("User", back_populates="notifications")


# ═══════════════════════════════════════
#  PLATFORM SETTINGS
# ═══════════════════════════════════════

class PlatformSetting(Base, TimestampMixin):
    __tablename__ = "platform_settings"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
