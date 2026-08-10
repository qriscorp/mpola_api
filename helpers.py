"""
Shared utility helpers — follows kumpi_api pattern.
"""

import base64
import uuid
import re
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc)


def safe_isoformat(dt):
    """Serialize a datetime for the frontend as an unambiguous UTC ISO
    string (with an explicit offset). Every model's DateTime columns are
    naive-but-actually-UTC (populated via func.now()/datetime.now(timezone.utc)),
    and plain str(dt)/dt.isoformat() on a naive datetime silently drops the
    UTC marker. JS's `new Date(...)` then parses that as local time instead
    of UTC, shifting every timestamp by the viewer's UTC offset (e.g. "just
    now" rendering as "3 hours ago" for a UTC+3 user) — this makes the
    UTC-ness explicit so the frontend parses it correctly.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def generateUniqueId(length=10):
    """Generate a URL-safe base64-encoded UUID, truncated to `length` chars."""
    raw = uuid.uuid4().bytes
    encoded = base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    return encoded[:length]


def generateReferenceNumber(prefix="LF"):
    """Generate a reference number like LF-2025-04-8821."""
    now = utc_now()
    uid = str(uuid.uuid4().int)[:4]
    return f"{prefix}-{now.year}-{now.month:02d}-{uid}"


def generateReferralCode(length=7):
    """Short, shareable, human-typeable referral code (uppercase, no ambiguous chars)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1
    raw = uuid.uuid4().bytes
    return "".join(alphabet[b % len(alphabet)] for b in raw[:length])


def normalizePhoneNumber(phone: str) -> str | None:
    """
    Normalize Ugandan phone to 256XXXXXXXXX (12 digits, no +).
    Handles: +256704690012, 256704690012, 0704690012, 704690012
    """
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("256") and len(digits) == 12:
        return digits  # already 256XXXXXXXXX
    if digits.startswith("0") and len(digits) == 10:
        return "256" + digits[1:]  # 0704... → 256704...
    if len(digits) == 9:
        return "256" + digits  # 704... → 256704...
    # Fallback: try to extract last 9 and prepend 256
    if len(digits) > 12 and digits.startswith("256"):
        return digits[:12]
    if len(digits) >= 9:
        return "256" + digits[-9:]
    return None


# Canonical resolution for a LenderOfferTemplate's required_documents labels
# (free-text, picked from a fixed checklist on the post-offer form — see
# DOCUMENTS/DOCUMENT_OPTIONS on both frontends). "National ID" reuses the
# account-wide KYC upload (routers/users.py); everything else is a
# BorrowerDocument — also account-wide/reusable, since a document a lender
# asked for once is worth keeping for the next offer that asks for the same
# thing, exactly like KYC already works. Single source of truth, shared by
# routers/loans.py (resolving a specific offer's requirements at accept
# time) and routers/users.py (validating uploads against the borrower_doc
# half of this map). Whoever adds a new label to the frontend checklist
# must add its resolution here too, or it'll never be satisfiable.
DOCUMENT_LABEL_MAP: dict[str, tuple[str, str]] = {
    "National ID": ("kyc", "national_id"),
    "Bank Statement (3mo)": ("borrower_doc", "bank_statement"),
    "Payslip / Business Proof": ("borrower_doc", "business_proof"),
    "Land Title": ("borrower_doc", "land_title"),
    "URA TIN": ("borrower_doc", "ura_tin"),
}
