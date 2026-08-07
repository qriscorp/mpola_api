from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text, DateTime,
    ForeignKey, Enum, UniqueConstraint, func,
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
    # Admin access is orthogonal to `role`: an account keeps its borrower/lender
    # portal identity (role can never be both at once) and can ADDITIONALLY be
    # flagged as admin/super_admin here — e.g. a lender who also moderates the
    # platform. Legacy accounts with role="admin"/"super_admin" (no portal
    # identity) still work as before via has_admin_access/has_super_admin_access.
    is_admin = Column(Boolean, default=False)
    is_super_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    is_phone_verified = Column(Boolean, default=False)
    is_kyc_verified = Column(Boolean, default=False)
    kyc_status = Column(String(20), default="pending")  # pending, verified, rejected
    credit_score = Column(Integer, default=0)
    push_token = Column(Text, nullable=True)  # Expo push token
    # JWT refresh tokens can exceed 255 chars once claims/signature are included.
    refresh_token = Column(Text, nullable=True)
    refresh_token_expires_at = Column(DateTime, nullable=True)
    two_factor_enabled = Column(Boolean, default=False)
    # Per-user notification preferences (Settings page toggles). Independent
    # of the admin-level PlatformSetting kill switches in _notify_admins —
    # those gate whether a category sends AT ALL platform-wide; these gate
    # whether one specific user wants to receive it.
    notif_new_application = Column(Boolean, default=True)
    notif_repayment_received = Column(Boolean, default=True)
    notif_loan_overdue = Column(Boolean, default=True)
    notif_portfolio_digest = Column(Boolean, default=False)
    notif_login_alerts = Column(Boolean, default=True)
    referral_code = Column(String(20), unique=True, nullable=True, index=True)
    referred_by_id = Column(String(50), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Relationships
    wallets = relationship("Wallet", back_populates="user", cascade="all, delete-orphan")
    loan_applications = relationship("LoanApplication", back_populates="borrower", foreign_keys="LoanApplication.borrower_id")
    offers_made = relationship("LoanOffer", back_populates="lender", foreign_keys="LoanOffer.lender_id")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")
    referred_by = relationship("User", remote_side=[id])

    @property
    def has_admin_access(self) -> bool:
        return bool(self.is_admin) or (self.role or "").lower() in ("admin", "super_admin")

    @property
    def has_super_admin_access(self) -> bool:
        return bool(self.is_super_admin) or (self.role or "").lower() == "super_admin"

    @property
    def portal_role(self) -> str:
        """Borrower/lender identity used for dashboard routing — never 'admin'."""
        r = (self.role or "borrower").lower()
        return r if r in ("borrower", "lender") else "borrower"

    @property
    def has_portal_identity(self) -> bool:
        """True if this account has a real lender/borrower portal (not a pure legacy admin)."""
        return (self.role or "").lower() in ("borrower", "lender")


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
    referred_by_code = Column(String(20), nullable=True)
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


class PlatformFeeTransaction(Base, TimestampMixin):
    """Platform revenue ledger — one row per fee charged on a withdrawal.
    Kept separate from WalletTransaction (which belongs to a user's own
    wallet) since this represents Mpola's own cut, not a user balance change.
    """
    __tablename__ = "platform_fee_transactions"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    user_id = Column(String(50), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    wallet_transaction_id = Column(String(50), nullable=True)
    category = Column(String(30), nullable=False)  # mobile_money_withdrawal, bank_withdrawal
    platform_fee = Column(Float, default=0.0)
    provider_fee = Column(Float, default=0.0)
    total_fee = Column(Float, nullable=False)

    user = relationship("User")


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
    max_interest_rate = Column(Float, nullable=True)  # borrower's optional cap, %/month — enforced in _template_matches and make_offer

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


class KYCDocument(Base, TimestampMixin):
    """Account-level identity verification documents — separate from
    LoanDocument, which is paperwork attached to one specific loan
    application. These are what admin KYC review actually looks at."""
    __tablename__ = "kyc_documents"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    user_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    document_type = Column(String(50), nullable=False)  # national_id, passport, profile_photo, proof_of_address
    file_url = Column(String(500), nullable=False)
    file_name = Column(String(255), nullable=True)
    verified = Column(Boolean, default=False)

    user = relationship("User")


class Guarantor(Base, TimestampMixin):
    __tablename__ = "guarantors"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    application_id = Column(String(50), ForeignKey("loan_applications.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(200), nullable=False)
    phone = Column(String(20), nullable=False)
    relationship_type = Column(String(50), nullable=True)  # friend, family, colleague
    status = Column(String(20), default="pending")  # pending, accepted, declined
    # Secret, single-use link sent by SMS so the guarantor (who has no account) can respond.
    confirmation_token = Column(String(64), unique=True, nullable=True)
    responded_at = Column(DateTime, nullable=True)

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


class LenderApplicationSkip(Base, TimestampMixin):
    """A lender explicitly declining to offer on a marketplace application.
    Hides it from that lender's own marketplace/applications view only —
    the application stays open and visible to every other lender; this
    does not change the application's status or block other offers."""
    __tablename__ = "lender_application_skips"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    lender_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    application_id = Column(String(50), ForeignKey("loan_applications.id", ondelete="CASCADE"), nullable=False)

    __table_args__ = (UniqueConstraint("lender_id", "application_id", name="uq_lender_application_skip"),)


class LenderOfferTemplate(Base, TimestampMixin):
    """A lender's standing lending criteria (max/min amount, rate, accepted
    loan types, etc). Submissions sit as 'pending_review' until an admin
    approves them — once approved, routers/loans.py's auto-matching engine
    checks it against pending applications (and every new one going forward)
    and creates real LoanOffer rows automatically.
    """
    __tablename__ = "lender_offer_templates"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    lender_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    max_amount = Column(Float, nullable=False)
    min_amount = Column(Float, nullable=False)
    interest_rate = Column(Float, nullable=False)
    max_duration = Column(Integer, nullable=False)  # months
    accepted_loan_types = Column(Text, nullable=True)  # JSON list
    required_documents = Column(Text, nullable=True)  # JSON list
    description = Column(Text, nullable=True)
    valid_until = Column(DateTime, nullable=True)
    max_concurrent_loans = Column(Integer, nullable=True)
    status = Column(String(20), default="pending_review")  # pending_review, draft, approved, rejected
    # Freeze is orthogonal to the admin review lifecycle above — a template
    # stays "approved" while frozen, it just stops matching. frozen_by
    # records who paused it, since only admin can undo an admin-initiated
    # freeze (a lender can always undo their own).
    is_frozen = Column(Boolean, default=False)
    frozen_by = Column(String(20), nullable=True)  # "lender" or "admin"
    # Set once the collections job has pushed the "your offer expired" alert
    # for the current valid_until, so it doesn't re-fire every day the offer
    # sits expired. Reset to False whenever valid_until is changed (see
    # extend_offer_template_expiry) so a fresh expiry can notify again.
    expiry_notified = Column(Boolean, default=False)

    lender = relationship("User")


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
    status = Column(String(20), default="active")  # pending_disbursement, active, completed, overdue, defaulted
    disbursed_at = Column(DateTime, nullable=True)
    # One-time late fee applied when the loan first goes overdue (see
    # scheduler._flag_overdue) — kept as its own running total (not just
    # folded into total_repayable) so make_repayment can tell how much of an
    # incoming payment is "late fee" vs principal/interest, since only the
    # late-fee portion gets the platform's extra cut (see LATE_FEE_PLATFORM_CUT_RATE).
    late_fee_amount = Column(Float, default=0.0)
    late_fee_paid = Column(Float, default=0.0)

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


# ═══════════════════════════════════════
#  LOGIN SESSIONS (device visibility)
# ═══════════════════════════════════════

class LoginSession(Base, TimestampMixin):
    """One row per successful login — powers the 'Active Sessions' view.
    Mpola only keeps a single active refresh token per user (see User.refresh_token),
    so there is no per-session revocation; this is visibility plus a
    sign-out-everywhere action, not true independent multi-device sessions.
    """
    __tablename__ = "login_sessions"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    user_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_label = Column(String(200), nullable=True)
    ip_address = Column(String(50), nullable=True)
    user_agent = Column(Text, nullable=True)

    user = relationship("User")


# ═══════════════════════════════════════
#  DISPUTES
# ═══════════════════════════════════════

class Dispute(Base, TimestampMixin):
    __tablename__ = "disputes"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    user_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    loan_id = Column(String(50), ForeignKey("loans.id", ondelete="SET NULL"), nullable=True)
    category = Column(String(50), nullable=False)  # payment, loan_terms, fraud, disbursement, other
    description = Column(Text, nullable=False)
    status = Column(String(20), default="open")  # open, investigating, resolved, rejected
    resolution_note = Column(Text, nullable=True)
    resolved_by = Column(String(100), nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    loan = relationship("Loan")


# ═══════════════════════════════════════
#  SUPPORT TICKETS
# ═══════════════════════════════════════

class SupportTicket(Base, TimestampMixin):
    __tablename__ = "support_tickets"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    user_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    subject = Column(String(255), nullable=False)
    category = Column(String(50), default="general")  # general, wallet, loan, kyc, bug, other
    status = Column(String(20), default="open")  # open, in_progress, resolved, closed

    user = relationship("User")
    messages = relationship("SupportMessage", back_populates="ticket", cascade="all, delete-orphan", order_by="SupportMessage.created_at")


class SupportMessage(Base, TimestampMixin):
    __tablename__ = "support_messages"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    ticket_id = Column(String(50), ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False)
    sender_id = Column(String(50), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_admin = Column(Boolean, default=False)
    message = Column(Text, nullable=False)

    ticket = relationship("SupportTicket", back_populates="messages")
    sender = relationship("User")
