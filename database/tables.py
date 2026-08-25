from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text, DateTime,
    ForeignKey, Enum, UniqueConstraint, Index, func,
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
    # District/town, e.g. "Kampala", "Mbarara" — shown alongside a lender's
    # offer or borrower's request on the public marketplace preview
    # (routers/public.py). Optional and editable from profile settings;
    # accounts created before this field existed are simply null, and the
    # frontend omits the location line entirely rather than showing a blank.
    city = Column(String(100), nullable=True)
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
    # When kyc_status last became "verified" — starts the 2-year re-upload
    # lock (KYC_REVERIFICATION_LOCK_DAYS in routers/users.py) and is cleared
    # whenever KYC is rejected, so a lock never survives a rejection.
    kyc_verified_at = Column(DateTime, nullable=True)
    credit_score = Column(Integer, default=0)
    push_token = Column(Text, nullable=True)  # Expo push token
    # This user's own read timestamp for their one persistent conversation
    # with Mpola Support (see AdminChatMessage) — mirrors Loan's
    # borrower_chat_read_at/lender_chat_read_at. No equivalent per-admin
    # column exists: any admin sees the same shared inbox, same as the
    # SupportTicket system.
    admin_chat_read_at = Column(DateTime, nullable=True)
    # "Has any admin opened this user's Mpola Support thread" — the admin
    # side's counterpart to admin_chat_read_at, but shared across every
    # admin/super admin rather than owned by one, since any of them can
    # reply (see AdminChatMessage). Set whenever any admin views the
    # thread (GET /chat/admin/conversations/{user_id}); read by this
    # user's own client to show read-receipt ticks on their sent messages.
    admin_chat_seen_by_admin_at = Column(DateTime, nullable=True)
    # JWT refresh tokens can exceed 255 chars once claims/signature are included.
    refresh_token = Column(Text, nullable=True)
    refresh_token_expires_at = Column(DateTime, nullable=True)
    two_factor_enabled = Column(Boolean, default=False)
    # Set when an admin restores a deactivated account (see UserRepo.
    # restore_deactivated_account) — the new account starts on a
    # server-generated temporary password, so both frontends need to know to
    # prompt "please change your password" right after this account's very
    # next successful login. Cleared by PUT /users/me/change-password.
    must_change_password = Column(Boolean, default=False)
    # Per-user notification preferences (Settings page toggles). Independent
    # of the admin-level PlatformSetting kill switches in _notify_admins —
    # those gate whether a category sends AT ALL platform-wide; these gate
    # whether one specific user wants to receive it.
    notif_new_application = Column(Boolean, default=True)
    notif_repayment_received = Column(Boolean, default=True)
    notif_loan_overdue = Column(Boolean, default=True)
    notif_portfolio_digest = Column(Boolean, default=False)
    notif_login_alerts = Column(Boolean, default=True)
    # Borrower-facing counterparts to the lender-facing fields above (those
    # gate lender-only notification sites — new application/repayment/
    # overdue/portfolio are all always sent to loan.lender_id, never
    # borrower_id — so a borrower toggling them would have no real effect).
    notif_offer_received = Column(Boolean, default=True)
    notif_payment_reminder = Column(Boolean, default=True)
    notif_application_status = Column(Boolean, default=True)
    referral_code = Column(String(20), unique=True, nullable=True, index=True)
    referred_by_id = Column(String(50), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Click-wrap acceptance of the Platform Terms, Privacy Policy, and
    # role-specific Code of Conduct at signup (RegisterStart.agree_to_terms,
    # required and validated true) — copied from SignupDraft.terms_accepted_at
    # at account-creation time. Null only for accounts created before this
    # field existed. Surfaced to admins on the user detail page as part of
    # KYC review.
    terms_accepted_at = Column(DateTime, nullable=True)

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
    # Stamped at register_start (RegisterStart.agree_to_terms is required and
    # validated true) — copied onto the created User at draft-completion
    # time so it survives past the draft's own lifetime.
    terms_accepted_at = Column(DateTime, nullable=True)


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
    # Set when the low-balance nudge fires (see scheduler._notify_low_balance_lenders),
    # cleared the moment balance recovers above threshold — re-arms a fresh dip
    # to notify immediately rather than waiting out a stale cooldown.
    low_balance_notified_at = Column(DateTime, nullable=True)
    # Admin-only freeze — blocks every money-moving action on this wallet
    # (deposit, withdraw, repayment, disbursement) without suspending the
    # account itself (see User.is_active for that). See
    # _ensure_wallet_not_frozen in routers/wallet.py, the single choke point
    # every wallet-touching endpoint calls before moving any money.
    is_frozen = Column(Boolean, default=False)
    frozen_reason = Column(String(500), nullable=True)
    frozen_at = Column(DateTime, nullable=True)
    frozen_by = Column(String(100), nullable=True)  # admin username

    user = relationship("User", back_populates="wallets")
    transactions = relationship("WalletTransaction", back_populates="wallet", cascade="all, delete-orphan")


class WalletTransaction(Base, TimestampMixin):
    __tablename__ = "wallet_transactions"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    wallet_id = Column(String(50), ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Float, nullable=False)
    type = Column(String(30), nullable=False)  # deposit, withdrawal, repayment, disbursement, top_up
    # "repayment"/"disbursement" are wallet-to-wallet — the same `type` is
    # used on both the sender's and receiver's row, so `type` alone can't
    # tell a debit from a credit for those. This is the explicit signal the
    # UI uses for the +/- sign and color, set once at creation and never
    # inferred from `type` or `amount` (which is always stored positive).
    direction = Column(String(10), nullable=True)  # credit or debit
    status = Column(String(20), default="completed")  # pending, completed, failed
    description = Column(Text, nullable=True)
    reference = Column(String(100), nullable=True)
    counterparty = Column(String(100), nullable=True)
    # Set for repayment/disbursement transactions so the detail view can show
    # the loan's own terms alongside the money movement — see GET
    # /wallet/transactions/{id}. Not a real FK (no ondelete behavior wanted;
    # a transaction record should outlive the loan row conceptually).
    loan_id = Column(String(50), nullable=True)

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
    duration = Column(Integer, nullable=True)  # months — exactly one of duration/duration_days is set
    duration_days = Column(Integer, nullable=True)  # short-term "emergency" loan (1-29 days), single bullet repayment
    loan_type = Column(String(30), nullable=False)  # personal, business, education, agricultural, emergency
    purpose = Column(Text, nullable=True)
    status = Column(String(30), default="pending")  # awaiting_guarantors, pending, approved, rejected, funded, completed, defaulted, expired
    monthly_payment = Column(Float, nullable=True)
    interest_rate = Column(Float, nullable=True)
    total_repayable = Column(Float, nullable=True)
    max_interest_rate = Column(Float, nullable=True)  # borrower's optional cap, %/month — enforced in _template_matches and make_offer
    # Optional — a borrower who needs funds urgently can cap how long their
    # request stays live; None means it never expires on its own (same
    # optional/nullable shape as LenderOfferTemplate.valid_until). Enforced
    # in _template_matches and auto-expired by scheduler._expire_stale_applications.
    valid_until = Column(DateTime, nullable=True)
    # Mirrors LenderOfferTemplate.is_frozen/frozen_by — pauses matching
    # without deleting or changing the application. "borrower" or "admin".
    is_frozen = Column(Boolean, default=False)
    frozen_by = Column(String(20), nullable=True)

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
    # Set only when an admin rejects this specific document; cleared on a
    # fresh upload or on verification. Presence of a reason (not `verified`
    # alone) is what distinguishes "rejected" from "still pending review".
    rejection_reason = Column(String(500), nullable=True)
    # When THIS document was individually verified — drives its own
    # re-upload lock (see KYC_REVERIFICATION_LOCK_DAYS in routers/users.py),
    # independent of the other documents on the same account or of whether
    # the account's overall kyc_status has reached "verified" yet. Cleared
    # on a fresh upload or rejection, same as `verified` itself.
    verified_at = Column(DateTime, nullable=True)

    user = relationship("User")


class BorrowerDocument(Base, TimestampMixin):
    """Account-level, reusable supporting documents — bank statements,
    payslips/business proof, land titles, URA TINs, etc. Separate from
    KYCDocument (identity verification, admin-reviewed) and from the now-
    retired per-application LoanDocument: a lender's standing offer names
    what it needs in plain-language labels (LenderOfferTemplate.
    required_documents), and DOCUMENT_LABEL_MAP (routers/loans.py) resolves
    each label to either a KYCDocument type ("National ID") or one of these
    types — uploaded once here, satisfies every current and future offer
    that asks for the same thing, exactly like KYC already does."""
    __tablename__ = "borrower_documents"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    user_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    document_type = Column(String(50), nullable=False)  # bank_statement, business_proof, land_title, ura_tin
    file_url = Column(String(500), nullable=False)
    file_name = Column(String(255), nullable=True)
    verified = Column(Boolean, default=False)

    user = relationship("User")


class CustomDocumentResponse(Base, TimestampMixin):
    """A borrower's fulfillment of a lender-specified custom ("Other: ...")
    document requirement that doesn't resolve to a known KYCDocument/
    BorrowerDocument type — either an uploaded file or a free-text
    explanation (at least one required). Scoped per-application (not
    account-level like BorrowerDocument) since the label is free text the
    lender typed for this specific request, not a stable document type."""
    __tablename__ = "custom_document_responses"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    application_id = Column(String(50), ForeignKey("loan_applications.id", ondelete="CASCADE"), nullable=False)
    label = Column(String(255), nullable=False)
    text_response = Column(Text, nullable=True)
    file_url = Column(String(500), nullable=True)
    file_name = Column(String(255), nullable=True)

    application = relationship("LoanApplication")

    __table_args__ = (
        # MySQL/InnoDB requires an index on a foreign key's leading column,
        # and reuses a compatible composite one instead of adding a second,
        # redundant single-column index — this composite index is that one.
        # Must be declared here (not just in the migration file) or
        # `alembic revision --autogenerate` sees it as an unmanaged index and
        # tries to drop it, which MySQL then refuses since the FK needs it.
        Index("ix_cdr_application_label", "application_id", "label"),
    )


class Guarantor(Base, TimestampMixin):
    """A guarantor is a real Mpola user (any role) vouching for one specific
    loan application — not a standing relationship and not a freeform
    name/phone entry. The borrower finds them by exact email+phone match
    (see GET /users/search-guarantor-candidate) and stages them client-side
    in the apply wizard; rows here are only created at application submit
    time, which is also when the invited user gets a real-time notification
    to accept or decline. Auto-matching for the application is gated on
    every row here reaching status == "accepted" (see auto_match_offers_for_application)."""
    __tablename__ = "guarantors"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    application_id = Column(String(50), ForeignKey("loan_applications.id", ondelete="CASCADE"), nullable=False)
    guarantor_user_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    relationship_type = Column(String(50), nullable=True)  # friend, family, colleague — optional label
    status = Column(String(20), default="pending")  # pending, accepted, declined
    responded_at = Column(DateTime, nullable=True)
    # Set on every reminder (manual, via POST /guarantors/{id}/remind, or
    # automatic, via scheduler._remind_pending_guarantors) — shared cooldown
    # so the two paths can't be combined to spam the guarantor.
    last_reminded_at = Column(DateTime, nullable=True)

    application = relationship("LoanApplication", back_populates="guarantors")
    guarantor_user = relationship("User", foreign_keys=[guarantor_user_id])


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
    duration = Column(Integer, nullable=True)  # months — exactly one of duration/duration_days is set
    duration_days = Column(Integer, nullable=True)  # short-term "emergency" offer (1-29 days), single bullet repayment
    monthly_payment = Column(Float, nullable=True)
    total_repayable = Column(Float, nullable=True)
    status = Column(String(20), default="pending")  # pending, accepted, declined, expired
    # Copied from the originating LenderOfferTemplate at creation time (see
    # _create_offer_from_template) — null for a lender's manual, non-
    # template offer (POST /loans/offers), which has no document
    # requirement concept today. Resolved against the borrower's KYC/
    # BorrowerDocument records at accept time (see respond_to_offer) —
    # accepting is blocked until every label here is satisfied.
    required_documents = Column(Text, nullable=True)  # JSON list of labels
    # Set only when this offer was auto-generated by a standing offer match
    # (see _create_offer_from_template) — null for a lender's manual,
    # hand-made offer. SET NULL on template delete: the offer itself must
    # survive (it may already be accepted/a real Loan), only the traceback
    # to its origin is lost.
    template_id = Column(String(50), ForeignKey("lender_offer_templates.id", ondelete="SET NULL"), nullable=True)
    # One-time flag for scheduler._handle_stale_matched_offers' day-2 nudge
    # (auto-matched offer still pending — reminds the borrower to respond
    # and tells the lender they can now manually counter-offer) — set True
    # right after that nudge fires so it doesn't repeat every day. Only
    # meaningful for template-originated offers; a manual offer never gets
    # this reminder at all.
    stale_notified = Column(Boolean, default=False, nullable=False)

    application = relationship("LoanApplication", back_populates="offers")
    lender = relationship("User", back_populates="offers_made", foreign_keys=[lender_id])
    template = relationship("LenderOfferTemplate")

    __table_args__ = (
        # Mirrors the live index created alongside the template_id FK (see
        # migration b3f6a2d9c714) — must be declared here or `alembic
        # revision --autogenerate` sees it as unmanaged and tries to DROP
        # it, which MySQL then refuses since the FK constraint depends on
        # it (same class of bug as CustomDocumentResponse above).
        Index("ix_loan_offers_template_id", "template_id"),
    )


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
    max_duration = Column(Integer, nullable=True)  # months
    # Day-based standing offer for "emergency" applications — exactly one of
    # max_duration/max_duration_days is ever set. See _template_matches.
    max_duration_days = Column(Integer, nullable=True)
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
    duration = Column(Integer, nullable=True)  # months — exactly one of duration/duration_days is set
    duration_days = Column(Integer, nullable=True)  # short-term "emergency" loan (1-29 days), single bullet repayment
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
    # Copied from the accepted LoanOffer at disbursement-accept time (see
    # respond_to_offer) — lets the lender's approve_disbursement screen show
    # exactly what was required next to what the borrower actually has on
    # file, right before releasing funds.
    required_documents = Column(Text, nullable=True)  # JSON list of labels
    # Optional free-text note the borrower can leave for the lender when
    # accepting the offer — e.g. context on a custom document requirement,
    # or anything else worth flagging before the lender approves disbursement.
    borrower_note = Column(Text, nullable=True)
    # Chat read-receipt timestamps — a loan has exactly two possible
    # readers, known in advance, so two columns here are simpler than a
    # junction table. Updated to now() whenever that side's GET
    # /loans/{id}/chat runs; unread count is messages from the OTHER
    # party newer than the caller's own column.
    borrower_chat_read_at = Column(DateTime, nullable=True)
    lender_chat_read_at = Column(DateTime, nullable=True)

    application = relationship("LoanApplication", back_populates="loan")
    borrower = relationship("User", foreign_keys=[borrower_id])
    lender_user = relationship("User", foreign_keys=[lender_id])
    repayments = relationship("Repayment", back_populates="loan", cascade="all, delete-orphan")
    chat_messages = relationship("LoanChatMessage", back_populates="loan", cascade="all, delete-orphan", order_by="LoanChatMessage.created_at")


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
    transaction_id = Column(String(100), nullable=True)  # the borrower's debit WalletTransaction
    # The lender's credit WalletTransaction for this same repayment — needed
    # so the lender's own transaction-detail view can look this repayment up
    # too (transaction_id alone only resolves from the borrower's side).
    lender_transaction_id = Column(String(100), nullable=True)

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


class WebPushSubscription(Base, TimestampMixin):
    """A browser's push subscription (from PushManager.subscribe()) — a
    user can have several (one per browser/device they've granted
    permission on), unlike the single Expo push_token column on User
    which only ever tracks one mobile device at a time."""
    __tablename__ = "web_push_subscriptions"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    user_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    endpoint = Column(Text, nullable=False)
    p256dh = Column(String(255), nullable=False)
    auth = Column(String(255), nullable=False)

    user = relationship("User")


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
    """Filed by one party (user_id) against the other side of a specific
    loan (respondent_id, auto-derived from loan_id — whichever of
    loan.borrower_id/lender_id isn't the filer). The two parties are meant
    to try to work it out directly first — via DisputeMessage and the
    propose/respond-to-proposal flow below — before either one escalates to
    admin (status="investigating"). Admin can also step in and resolve
    directly at any point. Disputes not tied to a loan (respondent_id null)
    skip straight to admin, since there's no counterparty to negotiate
    with."""
    __tablename__ = "disputes"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    user_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    respondent_id = Column(String(50), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    loan_id = Column(String(50), ForeignKey("loans.id", ondelete="SET NULL"), nullable=True)
    category = Column(String(50), nullable=False)  # payment, loan_terms, fraud, disbursement, other
    description = Column(Text, nullable=False)
    status = Column(String(20), default="open")  # open, investigating, resolved, rejected
    resolution_note = Column(Text, nullable=True)
    resolved_by = Column(String(100), nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    # Single active settlement proposal (either party can propose; the
    # other accepts or declines — accepting executes the actual wallet-to-
    # wallet transfer and resolves the dispute). A fresh proposal overwrites
    # whatever was here before, so this only ever tracks the current one,
    # not a full negotiation history (DisputeMessage carries that instead).
    proposed_by_id = Column(String(50), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    proposal_note = Column(Text, nullable=True)
    settlement_amount = Column(Float, nullable=True)
    settlement_payer_id = Column(String(50), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    proposal_status = Column(String(20), nullable=True)  # null (no active proposal), pending, declined

    user = relationship("User", foreign_keys=[user_id])
    respondent = relationship("User", foreign_keys=[respondent_id])
    proposed_by = relationship("User", foreign_keys=[proposed_by_id])
    settlement_payer = relationship("User", foreign_keys=[settlement_payer_id])
    loan = relationship("Loan")
    messages = relationship("DisputeMessage", back_populates="dispute", cascade="all, delete-orphan", order_by="DisputeMessage.created_at")


class DisputeMessage(Base, TimestampMixin):
    __tablename__ = "dispute_messages"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    dispute_id = Column(String(50), ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_id = Column(String(50), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_admin = Column(Boolean, default=False)
    message = Column(Text, nullable=False)

    dispute = relationship("Dispute", back_populates="messages")
    sender = relationship("User")


class LoanChatMessage(Base, TimestampMixin):
    """A message between a loan's borrower and lender — scoped to that one
    loan (not an open DM system), same reasoning as disputes: a real-money
    platform needs an evidence trail, not unscoped messaging that invites
    off-platform circumvention. Mirrors DisputeMessage, minus the
    admin/resolution-lock concepts that don't apply here."""
    __tablename__ = "loan_chat_messages"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    loan_id = Column(String(50), ForeignKey("loans.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_id = Column(String(50), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    message = Column(Text, nullable=True)
    file_url = Column(String(500), nullable=True)
    file_name = Column(String(255), nullable=True)

    loan = relationship("Loan", back_populates="chat_messages")
    sender = relationship("User")


class AdminChatMessage(Base, TimestampMixin):
    """A message in a user's live conversation with Mpola Support. Keyed by
    user_id, not a ticket — one persistent thread per user, any admin/super
    admin can reply (no per-admin ownership), same shared-inbox model the
    SupportTicket system already uses. Runs alongside SupportTicket rather
    than replacing it: tickets stay for formal/categorized issues, this is
    for quick live back-and-forth."""
    __tablename__ = "admin_chat_messages"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    user_id = Column(String(50), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_id = Column(String(50), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_admin = Column(Boolean, nullable=False, default=False)
    message = Column(Text, nullable=True)
    file_url = Column(String(500), nullable=True)
    file_name = Column(String(255), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    sender = relationship("User", foreign_keys=[sender_id])


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


# ═══════════════════════════════════════
#  FAQ
# ═══════════════════════════════════════

class Faq(Base, TimestampMixin):
    """Single source of truth for Help & Support FAQ content — replaces
    what used to be hardcoded, independently-drifting arrays duplicated in
    both the website and the app. `role` scopes a question to borrower,
    lender, or both ("all")."""
    __tablename__ = "faqs"

    id = Column(String(50), primary_key=True, default=generateUniqueId)
    category = Column(String(50), default="general")  # general, wallet, loan, kyc, bug, other
    role = Column(String(20), default="all")  # all, borrower, lender
    question = Column(String(500), nullable=False)
    answer = Column(Text, nullable=False)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
