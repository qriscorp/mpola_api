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
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


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
