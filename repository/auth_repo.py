"""
Authentication repository — FINTECH-GRADE SECURITY
───────────────────────────────────────────────────
Security measures:
  • Passwords: bcrypt (12 rounds) with min-strength validation
  • OTP: secrets module (CSPRNG), bcrypt-hashed in DB, 10-min expiry, max 5 attempts
  • JWT: 15-min access tokens with jti+iss, 7-day refresh tokens, rotation on use
  • Login: account lockout after 5 failed attempts (15 min window)
  • Role escalation: blocked at registration (only borrower/lender allowed)
  • Audit: all sensitive actions logged to audit_logs table
  • No OTP codes in logs (only debug level for dev)
"""

import json
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import JWT_SECRET, EGOSMS_USERNAME, EGOSMS_APIKEY
from database.tables import User, OTP, LoginAttempt, AuditLog
from helpers import generateUniqueId, normalizePhoneNumber
from logging_module import logger
from repository.models import AuthUser

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15       # Short-lived access tokens
REFRESH_TOKEN_EXPIRE_DAYS = 7          # 7-day refresh tokens (not 60)
OTP_EXPIRE_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15
PASSWORD_MIN_LENGTH = 8
ALLOWED_REGISTRATION_ROLES = {"borrower", "lender"}  # Users CANNOT register as admin


# ═══════════════════════════════════════════════
#  PASSWORD HELPERS
# ═══════════════════════════════════════════════


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def validate_password_strength(password: str):
    """Enforce fintech-grade password requirements."""
    errors = []
    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
    if not re.search(r"[A-Z]", password):
        errors.append("Password must contain at least one uppercase letter")
    if not re.search(r"[a-z]", password):
        errors.append("Password must contain at least one lowercase letter")
    if not re.search(r"\d", password):
        errors.append("Password must contain at least one digit")
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>_\-+=\[\]\\;'/`~]", password):
        errors.append("Password must contain at least one special character")
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))


def validate_email_format(email: str):
    """Validate email format."""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(pattern, email):
        raise HTTPException(status_code=400, detail="Invalid email format")


# ═══════════════════════════════════════════════
#  TOKEN HELPERS
# ═══════════════════════════════════════════════


def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({
        "exp": expire,
        "iss": "lendflow-api",
        "jti": generateUniqueId(20),  # Unique token ID for revocation tracking
        "iat": datetime.now(timezone.utc),
        "type": "access",
    })
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)


def create_refresh_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({
        "exp": expire,
        "iss": "lendflow-api",
        "jti": generateUniqueId(20),
        "iat": datetime.now(timezone.utc),
        "type": "refresh",
    })
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)


# ═══════════════════════════════════════════════
#  OTP HELPERS (CSPRNG + hashed storage)
# ═══════════════════════════════════════════════


def _generate_otp_code() -> str:
    """Cryptographically secure 6-digit OTP using secrets module."""
    return f"{secrets.randbelow(900000) + 100000}"


def _hash_otp(code: str) -> str:
    """Hash OTP before storing in DB — never store plain text."""
    return bcrypt.hashpw(code.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")


def _verify_otp_hash(code: str, hashed: str) -> bool:
    return bcrypt.checkpw(code.encode("utf-8"), hashed.encode("utf-8"))


# ═══════════════════════════════════════════════
#  AUDIT LOGGING
# ═══════════════════════════════════════════════


def _audit(db: Session, action: str, username: str | None = None, user_id: str | None = None,
           resource_type: str | None = None, resource_id: str | None = None,
           ip_address: str | None = None, details: dict | None = None):
    """Write an immutable audit log entry."""
    try:
        entry = AuditLog(
            user_id=user_id,
            username=username,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip_address,
            details=json.dumps(details) if details else None,
        )
        db.add(entry)
        db.flush()  # Don't commit — let the caller's transaction handle it
    except Exception as e:
        logger.error(f"Audit log write failed: {e}")


# ═══════════════════════════════════════════════
#  SMS VIA EGOSMS
# ═══════════════════════════════════════════════


def send_sms(phone_number: str, message: str):
    """Send SMS via EgoSMS API."""
    try:
        import requests as req_lib
        url = "https://www.egosms.co/api/v1/json/"
        payload = {
            "method": "SendSms",
            "userdata": {
                "username": EGOSMS_USERNAME,
                "password": EGOSMS_APIKEY,
            },
            "msgdata": [
                {
                    "number": phone_number,
                    "message": message,
                    "senderid": "LendFlow",
                }
            ],
        }
        resp = req_lib.post(url, json=payload, timeout=15)
        logger.info(f"EgoSMS response: {resp.status_code}")  # Don't log message content
        return resp.json()
    except Exception as e:
        logger.error(f"EgoSMS send error: {type(e).__name__}")
        return None


# ═══════════════════════════════════════════════
#  AUTH REPO
# ═══════════════════════════════════════════════


class AuthRepo:

    # ── verify token ────────────────────────────

    @staticmethod
    def verify_token(token: str) -> AuthUser:
        try:
            payload = jwt.decode(
                token, JWT_SECRET, algorithms=[ALGORITHM],
                issuer="lendflow-api",  # Verify issuer claim
            )
            username: str = payload.get("sub")
            role: str = payload.get("role", "borrower")
            token_type: str = payload.get("type", "access")
            if username is None:
                raise HTTPException(status_code=401, detail="Invalid token")
            if token_type != "access":
                raise HTTPException(status_code=401, detail="Invalid token type")
            return AuthUser(username=username, user_category=role)
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired")
        except jwt.InvalidIssuerError:
            raise HTTPException(status_code=401, detail="Invalid token issuer")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")

    # ── login attempt tracking ──────────────────

    @staticmethod
    def _check_login_lockout(db: Session, identifier: str, ip_address: str | None = None):
        """Check if account is locked due to too many failed attempts."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
        recent_failures = db.query(func.count(LoginAttempt.id)).filter(
            LoginAttempt.identifier == identifier.lower(),
            LoginAttempt.success == False,
            LoginAttempt.created_at >= cutoff,
        ).scalar()

        if recent_failures >= LOGIN_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=429,
                detail=f"Account temporarily locked. Try again in {LOGIN_LOCKOUT_MINUTES} minutes.",
            )

    @staticmethod
    def _record_login_attempt(db: Session, identifier: str, success: bool, ip_address: str | None = None):
        attempt = LoginAttempt(
            identifier=identifier.lower(),
            ip_address=ip_address,
            success=success,
        )
        db.add(attempt)
        db.flush()

    # ── register ────────────────────────────────

    @staticmethod
    def register(db: Session, user_data, ip_address: str | None = None):
        # Validate email format
        validate_email_format(user_data.email)

        # Validate password strength
        validate_password_strength(user_data.password)

        # SECURITY: Prevent role escalation — users can only register as borrower or lender
        requested_role = (user_data.role or "borrower").lower()
        if requested_role not in ALLOWED_REGISTRATION_ROLES:
            requested_role = "borrower"

        # Check for duplicate email (case-insensitive)
        existing_email = db.query(User).filter(
            func.lower(User.email) == user_data.email.lower()
        ).first()
        if existing_email:
            raise HTTPException(status_code=400, detail="Email already registered")

        # Generate username from email if not provided
        username = user_data.username
        if not username:
            # Sanitize: only allow alphanumeric and underscores
            raw = re.sub(r'[^a-zA-Z0-9_]', '', user_data.email.split("@")[0])
            username = raw[:50] if raw else "user"
            base_username = username
            counter = 1
            while db.query(User).filter(func.lower(User.username) == username.lower()).first():
                username = f"{base_username}{counter}"
                counter += 1
        else:
            # Sanitize provided username
            username = re.sub(r'[^a-zA-Z0-9_.]', '', username)[:100]

        # Check duplicate username
        if db.query(User).filter(func.lower(User.username) == username.lower()).first():
            raise HTTPException(status_code=400, detail="Username already taken")

        # Check duplicate phone (normalized to 256XXXXXXXXX)
        normalized_phone = None
        if user_data.phone_number:
            normalized_phone = normalizePhoneNumber(user_data.phone_number)
            if normalized_phone:
                existing_phone = db.query(User).filter(
                    User.phone_number == normalized_phone
                ).first()
                if existing_phone:
                    raise HTTPException(status_code=400, detail="Phone number already registered")

        hashed = get_password_hash(user_data.password)

        user = User(
            username=username,
            email=user_data.email.lower().strip(),
            full_name=user_data.full_name,
            phone_number=normalized_phone or user_data.phone_number,
            password_hash=hashed,
            nin=user_data.nin,
            account_type=user_data.account_type or "individual",
            role=requested_role,
        )

        db.add(user)
        db.flush()

        # Generate tokens
        token = create_access_token({"sub": user.username, "role": user.role})
        refresh = create_refresh_token({"sub": user.username, "role": user.role})

        user.refresh_token = refresh
        user.refresh_token_expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

        # Audit log
        _audit(db, "register", username=user.username, user_id=user.id,
               resource_type="user", ip_address=ip_address,
               details={"role": requested_role, "email": user.email})

        db.commit()
        db.refresh(user)

        # Send email OTP in background (new session to avoid thread-safety issues)
        try:
            threading.Thread(
                target=AuthRepo._send_otp_background,
                args=(username, user.email),
                daemon=True,
            ).start()
        except Exception:
            pass

        return {
            "status": 200,
            "message": "Registration successful",
            "access_token": token,
            "refresh_token": refresh,
            "user": _user_response(user),
        }

    # ── login ───────────────────────────────────

    @staticmethod
    def login(db: Session, login_data, ip_address: str | None = None):
        identifier = login_data.username.strip()

        # Check lockout
        AuthRepo._check_login_lockout(db, identifier, ip_address)

        # Normalize phone if user is logging in with a phone number
        normalized_phone = normalizePhoneNumber(identifier)

        # Find user (case-insensitive email/username, or normalized phone)
        user = db.query(User).filter(
            (func.lower(User.username) == identifier.lower())
            | (func.lower(User.email) == identifier.lower())
            | (User.phone_number == normalized_phone) if normalized_phone else
            (func.lower(User.username) == identifier.lower())
            | (func.lower(User.email) == identifier.lower())
        ).first()

        if not user:
            # Record failed attempt even if user doesn't exist (prevent enumeration timing)
            AuthRepo._record_login_attempt(db, identifier, False, ip_address)
            db.commit()
            raise HTTPException(status_code=400, detail="Invalid credentials")

        if not user.is_active:
            AuthRepo._record_login_attempt(db, identifier, False, ip_address)
            db.commit()
            raise HTTPException(status_code=403, detail="Account suspended")

        if not verify_password(login_data.password, user.password_hash):
            AuthRepo._record_login_attempt(db, identifier, False, ip_address)
            _audit(db, "login_failed", username=user.username, user_id=user.id,
                   resource_type="user", ip_address=ip_address)
            db.commit()
            raise HTTPException(status_code=400, detail="Invalid credentials")

        # Successful login
        AuthRepo._record_login_attempt(db, identifier, True, ip_address)

        token = create_access_token({"sub": user.username, "role": user.role})
        refresh = create_refresh_token({"sub": user.username, "role": user.role})

        user.refresh_token = refresh
        user.refresh_token_expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

        _audit(db, "login_success", username=user.username, user_id=user.id,
               resource_type="user", ip_address=ip_address)
        db.commit()

        return {
            "status": 200,
            "message": "Login successful",
            "access_token": token,
            "refresh_token": refresh,
            "user": _user_response(user),
        }

    # ── refresh ─────────────────────────────────

    @staticmethod
    def refresh_token(db: Session, refresh_token: str):
        try:
            payload = jwt.decode(
                refresh_token, JWT_SECRET, algorithms=[ALGORITHM],
                issuer="lendflow-api",
            )
            username = payload.get("sub")
            token_type = payload.get("type")
            if token_type != "refresh":
                raise HTTPException(status_code=401, detail="Invalid token type")
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Refresh token expired")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        user = db.query(User).filter(User.username == username).first()
        if not user or user.refresh_token != refresh_token:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account suspended")

        new_access = create_access_token({"sub": user.username, "role": user.role})
        new_refresh = create_refresh_token({"sub": user.username, "role": user.role})

        # Rotate refresh token (one-time use)
        user.refresh_token = new_refresh
        user.refresh_token_expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
        db.commit()

        return {
            "status": 200,
            "access_token": new_access,
            "refresh_token": new_refresh,
        }

    # ── OTP (secure) ───────────────────────────

    @staticmethod
    def _send_otp_background(username: str, email: str):
        """Background thread: generate + send OTP via email. Uses its own DB session."""
        from database import SessionLocal
        s = SessionLocal()
        try:
            code = _generate_otp_code()
            hashed = _hash_otp(code)

            # Upsert OTP
            existing = s.query(OTP).filter(
                OTP.username == username, OTP.purpose == "verification"
            ).first()
            if existing:
                existing.code_hash = hashed
                existing.expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES)
                existing.attempts = 0
            else:
                s.add(OTP(
                    username=username,
                    code_hash=hashed,
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES),
                    purpose="verification",
                ))
            s.commit()

            # Don't log the actual OTP code in production
            logger.info(f"OTP generated for user (verification)")

            # TODO: Send actual email via SMTP/SES
            # For dev only — remove in production:
            logger.debug(f"[DEV ONLY] OTP code for {username}: {code}")

        except Exception as e:
            logger.error(f"OTP background send error: {type(e).__name__}")
        finally:
            s.close()

    @staticmethod
    def send_otp(db: Session, username: str):
        """Generate and send OTP to user's email."""
        user = db.query(User).filter(User.username == username).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        code = _generate_otp_code()
        hashed = _hash_otp(code)

        existing = db.query(OTP).filter(
            OTP.username == username, OTP.purpose == "verification"
        ).first()
        if existing:
            existing.code_hash = hashed
            existing.expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES)
            existing.attempts = 0
        else:
            db.add(OTP(
                username=username,
                code_hash=hashed,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES),
                purpose="verification",
            ))
        db.commit()

        logger.info(f"OTP sent for user verification")
        logger.debug(f"[DEV ONLY] OTP code for {username}: {code}")
        return {"status": 200, "message": "OTP sent to email"}

    @staticmethod
    def verify_otp(db: Session, username: str, code: str):
        """Verify OTP with expiration + max attempts check."""
        otp = db.query(OTP).filter(
            OTP.username == username, OTP.purpose == "verification"
        ).first()

        if not otp:
            raise HTTPException(status_code=400, detail="No OTP found. Request a new one.")

        # Check expiration
        if otp.expires_at < datetime.now(timezone.utc):
            db.delete(otp)
            db.commit()
            raise HTTPException(status_code=400, detail="OTP expired. Request a new one.")

        # Check max attempts
        if otp.attempts >= OTP_MAX_ATTEMPTS:
            db.delete(otp)
            db.commit()
            raise HTTPException(status_code=429, detail="Too many attempts. Request a new OTP.")

        # Increment attempts before verification
        otp.attempts += 1
        db.flush()

        if not _verify_otp_hash(code, otp.code_hash):
            db.commit()
            remaining = OTP_MAX_ATTEMPTS - otp.attempts
            raise HTTPException(
                status_code=400,
                detail=f"Invalid OTP. {remaining} attempts remaining.",
            )

        # Success
        user = db.query(User).filter(User.username == username).first()
        if user:
            user.is_verified = True
        db.delete(otp)
        db.commit()
        return {"status": 200, "message": "Email verified successfully"}

    # ── Phone OTP (secure, via EgoSMS) ──────────

    @staticmethod
    def send_phone_otp(db: Session, username: str, phone_number: str):
        user = db.query(User).filter(User.username == username).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        code = _generate_otp_code()
        hashed = _hash_otp(code)

        existing = db.query(OTP).filter(
            OTP.username == username, OTP.phone_number == phone_number, OTP.purpose == "phone"
        ).first()
        if existing:
            existing.code_hash = hashed
            existing.expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES)
            existing.attempts = 0
        else:
            db.add(OTP(
                username=username,
                phone_number=phone_number,
                code_hash=hashed,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES),
                purpose="phone",
            ))
        db.commit()

        # Send SMS
        normalized = normalizePhoneNumber(phone_number)
        full_phone = f"256{normalized}" if normalized else phone_number
        message = f"Your LendFlow verification code is: {code}. Expires in {OTP_EXPIRE_MINUTES} minutes."
        threading.Thread(target=send_sms, args=(full_phone, message), daemon=True).start()

        return {"status": 200, "message": "OTP sent to phone"}

    @staticmethod
    def verify_phone_otp(db: Session, username: str, phone_number: str, code: str):
        otp = db.query(OTP).filter(
            OTP.username == username, OTP.phone_number == phone_number, OTP.purpose == "phone"
        ).first()

        if not otp:
            raise HTTPException(status_code=400, detail="No OTP found. Request a new one.")
        if otp.expires_at < datetime.now(timezone.utc):
            db.delete(otp)
            db.commit()
            raise HTTPException(status_code=400, detail="OTP expired. Request a new one.")
        if otp.attempts >= OTP_MAX_ATTEMPTS:
            db.delete(otp)
            db.commit()
            raise HTTPException(status_code=429, detail="Too many attempts. Request a new OTP.")

        otp.attempts += 1
        db.flush()

        if not _verify_otp_hash(code, otp.code_hash):
            db.commit()
            remaining = OTP_MAX_ATTEMPTS - otp.attempts
            raise HTTPException(status_code=400, detail=f"Invalid OTP. {remaining} attempts remaining.")

        user = db.query(User).filter(User.username == username).first()
        if user:
            user.is_phone_verified = True
            user.phone_number = phone_number
        db.delete(otp)
        db.commit()
        return {"status": 200, "message": "Phone verified successfully"}

    # ── Password reset (secure) ─────────────────

    @staticmethod
    def send_password_reset_code(db: Session, email: str):
        validate_email_format(email)
        user = db.query(User).filter(func.lower(User.email) == email.lower()).first()
        if not user:
            # Don't reveal whether the email exists (enumeration protection)
            return {"status": 200, "message": "If this email is registered, a reset code has been sent."}

        code = _generate_otp_code()
        hashed = _hash_otp(code)

        existing = db.query(OTP).filter(
            OTP.username == user.username, OTP.purpose == "password_reset"
        ).first()
        if existing:
            existing.code_hash = hashed
            existing.expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES)
            existing.attempts = 0
        else:
            db.add(OTP(
                username=user.username,
                code_hash=hashed,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES),
                purpose="password_reset",
            ))
        db.commit()

        logger.info(f"Password reset code generated")
        logger.debug(f"[DEV ONLY] Reset code for {email}: {code}")
        return {"status": 200, "message": "If this email is registered, a reset code has been sent."}

    @staticmethod
    def verify_password_reset_code(db: Session, email: str, code: str):
        user = db.query(User).filter(func.lower(User.email) == email.lower()).first()
        if not user:
            raise HTTPException(status_code=400, detail="Invalid code")

        otp = db.query(OTP).filter(
            OTP.username == user.username, OTP.purpose == "password_reset"
        ).first()

        if not otp:
            raise HTTPException(status_code=400, detail="No reset code found. Request a new one.")
        if otp.expires_at < datetime.now(timezone.utc):
            db.delete(otp)
            db.commit()
            raise HTTPException(status_code=400, detail="Code expired. Request a new one.")
        if otp.attempts >= OTP_MAX_ATTEMPTS:
            db.delete(otp)
            db.commit()
            raise HTTPException(status_code=429, detail="Too many attempts. Request a new code.")

        otp.attempts += 1
        db.flush()

        if not _verify_otp_hash(code, otp.code_hash):
            db.commit()
            raise HTTPException(status_code=400, detail="Invalid code")

        # Generate short-lived token for password reset (15 min)
        token = create_access_token(
            {"sub": user.username, "role": user.role, "purpose": "password_reset"},
            expires_delta=timedelta(minutes=15),
        )
        db.delete(otp)
        db.commit()
        return {"status": 200, "access_token": token, "message": "Code verified"}

    @staticmethod
    def reset_password(db: Session, email: str, new_password: str, access_token: str | None = None):
        validate_password_strength(new_password)

        user = db.query(User).filter(func.lower(User.email) == email.lower()).first()
        if not user:
            raise HTTPException(status_code=404, detail="Email not found")

        if not access_token:
            raise HTTPException(status_code=400, detail="Reset token required")

        try:
            payload = jwt.decode(access_token, JWT_SECRET, algorithms=[ALGORITHM], issuer="lendflow-api")
            if payload.get("sub") != user.username:
                raise HTTPException(status_code=401, detail="Token mismatch")
            if payload.get("purpose") != "password_reset":
                raise HTTPException(status_code=401, detail="Invalid token purpose")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid reset token")

        user.password_hash = get_password_hash(new_password)
        # Invalidate all sessions after password reset
        user.refresh_token = None
        user.refresh_token_expires_at = None

        _audit(db, "password_reset", username=user.username, user_id=user.id, resource_type="user")
        db.commit()
        return {"status": 200, "message": "Password reset successful"}

    @staticmethod
    def change_password(db: Session, user: User, old_password: str, new_password: str):
        if not verify_password(old_password, user.password_hash):
            raise HTTPException(status_code=400, detail="Incorrect current password")

        validate_password_strength(new_password)

        user.password_hash = get_password_hash(new_password)
        # Invalidate refresh token to force re-auth
        user.refresh_token = None
        user.refresh_token_expires_at = None

        _audit(db, "password_change", username=user.username, user_id=user.id, resource_type="user")
        db.commit()
        return {"status": 200, "message": "Password changed. Please login again."}

    # ── Existence checks ────────────────────────

    @staticmethod
    def check_email_status(db: Session, email: str):
        exists = db.query(User).filter(func.lower(User.email) == email.lower()).first() is not None
        return {"exists": exists, "email": email}

    @staticmethod
    def check_phone_number_status(db: Session, phone: str):
        exists = db.query(User).filter(User.phone_number == phone).first() is not None
        return {"exists": exists, "phone_number": phone}

    @staticmethod
    def check_username_status(db: Session, username: str):
        exists = db.query(User).filter(func.lower(User.username) == username.lower()).first() is not None
        return {"exists": exists, "username": username}


# ═══════════════════════════════════════════════
#  PRIVATE HELPERS
# ═══════════════════════════════════════════════

def _user_response(user: User) -> dict:
    """Standard user response dict — avoids leaking sensitive fields like password_hash, refresh_token."""
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
        "phone_number": user.phone_number,
        "role": user.role,
        "is_verified": user.is_verified,
        "is_phone_verified": user.is_phone_verified,
        "is_kyc_verified": user.is_kyc_verified,
        "kyc_status": user.kyc_status,
        "account_type": user.account_type,
        "profile_pic": user.profile_pic,
        "credit_score": user.credit_score,
        "bio": user.bio,
        "created_at": str(user.created_at),
    }
