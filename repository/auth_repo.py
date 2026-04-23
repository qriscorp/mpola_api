import json
import re
import secrets
import smtplib
import ssl
import threading
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import bcrypt
import jwt
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from comms_sdk import CommsSDK

from config import JWT_SECRET, EGOSMS_USERNAME, EGOSMS_APIKEY, SMTP_USERNAME, SMTP_PASSWORD, SMTP_SERVER, SMTP_PORT
from database.tables import User, OTP, LoginAttempt, AuditLog, SignupDraft
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
ALLOWED_LOGIN_PORTALS = {"borrower", "lender"}
SIGNUP_DRAFT_EXPIRE_HOURS = 24


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
        # Don't flush here: flushing can trigger unrelated pending writes and
        # poison the active transaction if one of them fails.
    except Exception as e:
        logger.error(f"Audit log write failed: {e}")


# ═══════════════════════════════════════════════
#  EMAIL VIA SMTP
# ═══════════════════════════════════════════════


def _send_email(to_email: str, subject: str, html_body: str):
    """Send HTML email via SMTP (SSL, port 465)."""
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        logger.warning("SMTP credentials not configured — email not sent")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Welend <{SMTP_USERNAME}>"
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html"))
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_USERNAME, to_email, msg.as_string())
        logger.info(f"Email sent successfully")
    except Exception as e:
        logger.error(f"Email send error: {type(e).__name__}: {e}")


def _build_otp_email_html(username: str, code: str, purpose: str = "verification") -> str:
    action_text = "verify your account" if purpose == "verification" else "reset your password"
    year = datetime.now().year
    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>Welend OTP</title></head>
    <body style="font-family:Arial,sans-serif;margin:0;padding:0;background:#f4f8f7;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f4f8f7;padding:30px 0;">
        <tr><td align="center">
          <table width="560" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff;border-radius:8px;overflow:hidden;">
            <tr><td style="background:#2BB5A0;padding:24px 32px;">
              <h1 style="margin:0;color:#ffffff;font-size:22px;letter-spacing:-0.5px;">Welend</h1>
              <p style="margin:4px 0 0;color:#d0f5ef;font-size:13px;">Peer-to-Peer Lending Platform</p>
            </td></tr>
            <tr><td style="padding:32px;">
              <p style="color:#1B2B3A;font-size:15px;">Hello <strong>{username}</strong>,</p>
              <p style="color:#444;font-size:14px;">Use the code below to {action_text}. It expires in 10 minutes.</p>
              <div style="margin:24px 0;text-align:center;">
                <span style="display:inline-block;background:#2BB5A0;color:#ffffff;font-size:32px;font-weight:bold;letter-spacing:8px;padding:14px 32px;border-radius:6px;">{code}</span>
              </div>
              <p style="color:#888;font-size:12px;">If you did not request this code, please ignore this email. Do not share this code with anyone.</p>
            </td></tr>
            <tr><td style="background:#f9f9f9;padding:16px 32px;border-top:1px solid #eee;">
              <p style="margin:0;color:#aaa;font-size:11px;text-align:center;">&copy; {year} Welend Uganda Ltd. &middot; All rights reserved.</p>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """


# ═══════════════════════════════════════════════
#  SMS VIA EGOSMS (CommsSDK)
# ═══════════════════════════════════════════════

try:
    if EGOSMS_USERNAME and EGOSMS_APIKEY:
        _sms_sdk = CommsSDK.authenticate(EGOSMS_USERNAME, EGOSMS_APIKEY)
        logger.info("CommsSDK (EgoSMS) initialized successfully")
    else:
        logger.warning("SMS credentials not found — SMS disabled")
        _sms_sdk = None
except Exception as _e:
    logger.error(f"Failed to initialize CommsSDK: {_e}")
    _sms_sdk = None


def send_sms(phone_number: str, message: str):
    """Send SMS via EgoSMS CommsSDK."""
    try:
        if _sms_sdk is None:
            logger.error("SMS SDK not initialized")
            return None
        # CommsSDK requires E.164 format (+256XXXXXXXXX)
        if not phone_number.startswith('+'):
            phone_number = '+' + phone_number
        _sms_sdk.send_sms(phone_number, message)
        logger.info(f"SMS sent via CommsSDK to {phone_number[:8]}...")
    except Exception as e:
        logger.error(f"EgoSMS send error: {type(e).__name__}: {e}")
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

    @staticmethod
    def _enforce_portal_role(user: User, portal: str | None):
        if not portal:
            return

        requested_portal = portal.strip().lower()
        if requested_portal not in ALLOWED_LOGIN_PORTALS:
            raise HTTPException(status_code=400, detail="Invalid login portal")

        role = (user.role or "borrower").lower()
        if role in {"admin", "super_admin"}:
            return

        if requested_portal == "borrower" and role == "lender":
            raise HTTPException(
                status_code=403,
                detail="This account is registered as a lender. Please sign in from the lender portal.",
            )

        if requested_portal == "lender" and role == "borrower":
            raise HTTPException(
                status_code=403,
                detail="This account is registered as a borrower. Please sign in from the borrower portal.",
            )

    @staticmethod
    def _generate_unique_username(db: Session, email: str) -> str:
        raw = re.sub(r'[^a-zA-Z0-9_]', '', email.split("@")[0])
        username = raw[:50] if raw else "user"
        base_username = username
        counter = 1
        while db.query(User).filter(func.lower(User.username) == username.lower()).first():
            username = f"{base_username}{counter}"
            counter += 1
        return username

    @staticmethod
    def _get_active_signup_draft(db: Session, draft_id: str) -> SignupDraft:
        draft = db.query(SignupDraft).filter(SignupDraft.id == draft_id).first()
        if not draft:
            raise HTTPException(status_code=404, detail="Signup session not found")

        if draft.is_completed:
            raise HTTPException(status_code=400, detail="Signup already completed. Please sign in.")

        if draft.expires_at < datetime.utcnow():
            db.query(OTP).filter(OTP.username == draft.id).delete(synchronize_session=False)
            db.delete(draft)
            db.commit()
            raise HTTPException(status_code=400, detail="Signup session expired. Please register again.")

        return draft

    @staticmethod
    def _upsert_signup_otp(db: Session, draft_id: str, purpose: str, code: str, phone_number: str | None = None):
        hashed = _hash_otp(code)
        existing = db.query(OTP).filter(
            OTP.username == draft_id,
            OTP.purpose == purpose,
        ).first()
        if existing:
            existing.code_hash = hashed
            existing.phone_number = phone_number
            existing.expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES)
            existing.attempts = 0
        else:
            db.add(
                OTP(
                    username=draft_id,
                    phone_number=phone_number,
                    code_hash=hashed,
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES),
                    purpose=purpose,
                )
            )

    @staticmethod
    def register_start(db: Session, data, ip_address: str | None = None):
        validate_email_format(data.email)
        validate_password_strength(data.password)

        requested_role = (data.role or "borrower").lower()
        if requested_role not in ALLOWED_REGISTRATION_ROLES:
            requested_role = "borrower"

        normalized_phone = None
        if data.phone_number:
            normalized_phone = normalizePhoneNumber(data.phone_number)
            if not normalized_phone:
                raise HTTPException(status_code=400, detail="Invalid phone number")

        existing_user = db.query(User).filter(func.lower(User.email) == data.email.lower()).first()
        if existing_user:
            raise HTTPException(status_code=400, detail="Email already registered")

        if normalized_phone:
            existing_phone = db.query(User).filter(User.phone_number == normalized_phone).first()
            if existing_phone:
                raise HTTPException(status_code=400, detail="Phone number already registered")

        draft = db.query(SignupDraft).filter(
            func.lower(SignupDraft.email) == data.email.lower(),
            SignupDraft.is_completed == False,
        ).first()

        if draft and draft.expires_at < datetime.utcnow():
            db.query(OTP).filter(OTP.username == draft.id).delete(synchronize_session=False)
            db.delete(draft)
            draft = None

        if not draft:
            draft = SignupDraft(
                id=generateUniqueId(),
                username=AuthRepo._generate_unique_username(db, data.email),
                email=data.email.lower().strip(),
                phone_number=normalized_phone,
                password_hash=get_password_hash(data.password),
                full_name=data.full_name,
                nin=data.nin,
                account_type=data.account_type or "individual",
                role=requested_role,
                email_verified=False,
                phone_verified=False,
                expires_at=datetime.utcnow() + timedelta(hours=SIGNUP_DRAFT_EXPIRE_HOURS),
            )
            db.add(draft)
        else:
            draft.username = AuthRepo._generate_unique_username(db, data.email)
            draft.phone_number = normalized_phone
            draft.password_hash = get_password_hash(data.password)
            draft.full_name = data.full_name
            draft.nin = data.nin
            draft.account_type = data.account_type or "individual"
            draft.role = requested_role
            draft.email_verified = False
            draft.phone_verified = False
            draft.expires_at = datetime.utcnow() + timedelta(hours=SIGNUP_DRAFT_EXPIRE_HOURS)
            db.query(OTP).filter(OTP.username == draft.id).delete(synchronize_session=False)

        email_code = _generate_otp_code()
        AuthRepo._upsert_signup_otp(db, draft.id, "signup_email", email_code)

        _audit(
            db,
            "signup_started",
            username=draft.username,
            resource_type="signup_draft",
            resource_id=draft.id,
            ip_address=ip_address,
            details={"role": requested_role, "email": draft.email},
        )
        db.commit()

        logger.debug(f"[DEV ONLY] Signup email OTP for draft {draft.id}: {email_code}")
        threading.Thread(
            target=_send_email,
            args=(
                draft.email,
                "Welend — Verify Your Email Address",
                _build_otp_email_html(draft.username, email_code, purpose="verification"),
            ),
            daemon=True,
        ).start()

        return {
            "status": 200,
            "message": "Registration started. Verify your email to continue.",
            "draft_id": draft.id,
            "email": draft.email,
            "phone_number": draft.phone_number,
            "role": draft.role,
        }

    @staticmethod
    def send_signup_email_otp(db: Session, draft_id: str):
        draft = AuthRepo._get_active_signup_draft(db, draft_id)
        email_code = _generate_otp_code()
        AuthRepo._upsert_signup_otp(db, draft.id, "signup_email", email_code)
        db.commit()

        logger.debug(f"[DEV ONLY] Signup email OTP resend for draft {draft.id}: {email_code}")
        threading.Thread(
            target=_send_email,
            args=(
                draft.email,
                "Welend — Verify Your Email Address",
                _build_otp_email_html(draft.username, email_code, purpose="verification"),
            ),
            daemon=True,
        ).start()

        return {"status": 200, "message": "OTP sent to email"}

    @staticmethod
    def verify_signup_email_otp(db: Session, draft_id: str, code: str):
        draft = AuthRepo._get_active_signup_draft(db, draft_id)
        otp = db.query(OTP).filter(
            OTP.username == draft.id,
            OTP.purpose == "signup_email",
        ).first()

        if not otp:
            raise HTTPException(status_code=400, detail="No OTP found. Request a new one.")
        if otp.expires_at < datetime.utcnow():
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

        draft.email_verified = True
        db.delete(otp)
        db.commit()
        return {"status": 200, "message": "Email verified successfully"}

    @staticmethod
    def send_signup_phone_otp(db: Session, draft_id: str, phone_number: str):
        draft = AuthRepo._get_active_signup_draft(db, draft_id)
        normalized = normalizePhoneNumber(phone_number)
        if not normalized:
            raise HTTPException(status_code=400, detail="Invalid phone number")

        existing_phone = db.query(User).filter(User.phone_number == normalized).first()
        if existing_phone:
            raise HTTPException(status_code=400, detail="Phone number already registered")

        draft.phone_number = normalized
        phone_code = _generate_otp_code()
        AuthRepo._upsert_signup_otp(db, draft.id, "signup_phone", phone_code, phone_number=normalized)
        db.commit()

        logger.debug(f"[DEV ONLY] Signup phone OTP for draft {draft.id}: {phone_code}")
        message = f"Your Welend verification code is: {phone_code}. Expires in {OTP_EXPIRE_MINUTES} minutes."
        threading.Thread(target=send_sms, args=(normalized, message), daemon=True).start()
        return {"status": 200, "message": "OTP sent to phone"}

    @staticmethod
    def _finalize_signup_draft(db: Session, draft: SignupDraft, ip_address: str | None = None):
        existing_email = db.query(User).filter(func.lower(User.email) == draft.email.lower()).first()
        if existing_email:
            raise HTTPException(status_code=400, detail="Email already registered")

        if draft.phone_number:
            existing_phone = db.query(User).filter(User.phone_number == draft.phone_number).first()
            if existing_phone:
                raise HTTPException(status_code=400, detail="Phone number already registered")

        username = draft.username
        base_username = username
        while db.query(User).filter(func.lower(User.username) == username.lower()).first():
            username = f"{base_username}{secrets.randbelow(9) + 1}"

        user = User(
            username=username,
            email=draft.email,
            full_name=draft.full_name,
            phone_number=draft.phone_number,
            password_hash=draft.password_hash,
            nin=draft.nin,
            account_type=draft.account_type or "individual",
            role=draft.role or "borrower",
            is_verified=True,
            is_phone_verified=True,
        )
        db.add(user)
        db.flush()

        draft.is_completed = True
        draft.created_user_id = user.id

        _audit(
            db,
            "register",
            username=user.username,
            user_id=user.id,
            resource_type="user",
            ip_address=ip_address,
            details={"role": user.role, "email": user.email, "source": "signup_draft"},
        )
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def verify_signup_phone_otp(db: Session, draft_id: str, phone_number: str, code: str, ip_address: str | None = None):
        draft = AuthRepo._get_active_signup_draft(db, draft_id)
        normalized = normalizePhoneNumber(phone_number)
        if not normalized:
            raise HTTPException(status_code=400, detail="Invalid phone number")

        otp = db.query(OTP).filter(
            OTP.username == draft.id,
            OTP.purpose == "signup_phone",
        ).first()

        if not otp:
            raise HTTPException(status_code=400, detail="No OTP found. Request a new one.")
        if otp.expires_at < datetime.utcnow():
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

        draft.phone_verified = True
        draft.phone_number = normalized
        db.delete(otp)

        if not draft.email_verified:
            db.commit()
            return {"status": 200, "message": "Phone verified. Please verify email to finish signup."}

        AuthRepo._finalize_signup_draft(db, draft, ip_address=ip_address)
        return {"status": 200, "message": "Account created successfully. Please sign in."}

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

        # Find user: case-insensitive email/username, or normalized phone (256XXXXXXXXX),
        # or raw phone as stored (backward compat with pre-normalization registrations)
        lookup = (
            (func.lower(User.username) == identifier.lower())
            | (func.lower(User.email) == identifier.lower())
        )
        if normalized_phone:
            lookup = lookup | (User.phone_number == normalized_phone)
        # Also match raw identifier as phone (handles users stored before normalization)
        if identifier != normalized_phone:
            lookup = lookup | (User.phone_number == identifier)
        user = db.query(User).filter(lookup).first()

        if not user:
            # Explicit message requested by product team
            AuthRepo._record_login_attempt(db, identifier, False, ip_address)
            db.commit()
            raise HTTPException(
                status_code=404,
                detail="Account does not exist. Please sign up first.",
            )

        if not user.is_active:
            AuthRepo._record_login_attempt(db, identifier, False, ip_address)
            db.commit()
            raise HTTPException(status_code=403, detail="Account suspended")

        if not verify_password(login_data.password, user.password_hash):
            AuthRepo._record_login_attempt(db, identifier, False, ip_address)
            _audit(db, "login_failed", username=user.username, user_id=user.id,
                   resource_type="user", ip_address=ip_address)
            db.commit()
            raise HTTPException(
                status_code=401,
                detail="Invalid credentials. Please check your password and try again.",
            )

        # Enforce portal-specific sign-in for borrower/lender accounts.
        AuthRepo._enforce_portal_role(user, getattr(login_data, "portal", None))

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
            logger.info(f"OTP generated for user (verification)")
            logger.debug(f"[DEV ONLY] OTP code for {username}: {code}")

            # Send email
            _send_email(
                to_email=email,
                subject="Welend — Verify Your Email Address",
                html_body=_build_otp_email_html(username, code, purpose="verification"),
            )

        except Exception as e:
            logger.error(f"OTP background send error: {type(e).__name__}: {e}")
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

        logger.info(f"OTP generated for resend")
        logger.debug(f"[DEV ONLY] OTP code for {username}: {code}")

        # Send email in background thread (non-blocking)
        threading.Thread(
            target=_send_email,
            args=(user.email, "Welend \u2014 Verify Your Email Address",
                  _build_otp_email_html(username, code, purpose="verification")),
            daemon=True,
        ).start()

        return {"status": 200, "message": "OTP sent to email"}

    @staticmethod
    def verify_otp(db: Session, username: str, code: str):
        """Verify OTP with expiration + max attempts check."""
        otp = db.query(OTP).filter(
            OTP.username == username, OTP.purpose == "verification"
        ).first()

        if not otp:
            raise HTTPException(status_code=400, detail="No OTP found. Request a new one.")

        # MySQL stores DateTime as naive UTC — compare with naive utcnow()
        if otp.expires_at < datetime.utcnow():
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

        # Normalize phone upfront — store and send to 256XXXXXXXXX format
        normalized = normalizePhoneNumber(phone_number)
        if not normalized:
            raise HTTPException(status_code=400, detail="Invalid phone number")

        code = _generate_otp_code()
        hashed = _hash_otp(code)

        # Upsert OTP keyed by normalized phone
        existing = db.query(OTP).filter(
            OTP.username == username, OTP.purpose == "phone"
        ).first()
        if existing:
            existing.code_hash = hashed
            existing.phone_number = normalized
            existing.expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES)
            existing.attempts = 0
        else:
            db.add(OTP(
                username=username,
                phone_number=normalized,
                code_hash=hashed,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES),
                purpose="phone",
            ))
        db.commit()

        logger.debug(f"[DEV ONLY] Phone OTP for {username}: {code}")

        # Send SMS via EgoSMS (normalized already has 256 prefix)
        sms_number = normalized  # 256XXXXXXXXX
        message = f"Your Welend verification code is: {code}. Expires in {OTP_EXPIRE_MINUTES} minutes."
        threading.Thread(target=send_sms, args=(sms_number, message), daemon=True).start()

        return {"status": 200, "message": "OTP sent to phone"}

    @staticmethod
    def verify_phone_otp(db: Session, username: str, phone_number: str, code: str):
        # Normalize so it matches what was stored in send_phone_otp
        normalized = normalizePhoneNumber(phone_number) or phone_number

        otp = db.query(OTP).filter(
            OTP.username == username, OTP.purpose == "phone"
        ).first()

        if not otp:
            raise HTTPException(status_code=400, detail="No OTP found. Request a new one.")
        # MySQL stores DateTime as naive UTC — compare with naive utcnow()
        if otp.expires_at < datetime.utcnow():
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
            user.phone_number = normalized
        db.delete(otp)
        db.commit()
        return {"status": 200, "message": "Phone verified successfully"}

    # ── Password reset (secure) ─────────────────

    @staticmethod
    def _find_user_by_identifier(db: Session, identifier: str):
        """Look up a user by email or phone number (auto-detected)."""
        if "@" in identifier:
            return db.query(User).filter(func.lower(User.email) == identifier.lower()).first()
        # Treat as phone — normalize and try both raw and normalized
        normalized = normalizePhoneNumber(identifier) or identifier
        return (
            db.query(User).filter(User.phone_number == normalized).first()
            or db.query(User).filter(User.phone_number == identifier).first()
        )

    @staticmethod
    def send_password_reset_code(db: Session, identifier: str):
        user = AuthRepo._find_user_by_identifier(db, identifier)
        _generic_ok = {"status": 200, "message": "If this account exists, a reset code has been sent."}
        if not user:
            return _generic_ok

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

        logger.debug(f"[DEV ONLY] Reset code for {user.username}: {code}")

        # Send via the channel the user chose
        if "@" in identifier:
            html = _build_otp_email_html(user.username, code, purpose="password_reset")
            threading.Thread(
                target=_send_email,
                args=(user.email, "Welend — Password Reset Code", html),
                daemon=True,
            ).start()
        else:
            message = f"Your Welend password reset code is: {code}. Expires in {OTP_EXPIRE_MINUTES} minutes."
            threading.Thread(target=send_sms, args=(user.phone_number, message), daemon=True).start()

        return _generic_ok

    @staticmethod
    def verify_password_reset_code(db: Session, identifier: str, code: str):
        user = AuthRepo._find_user_by_identifier(db, identifier)
        if not user:
            raise HTTPException(status_code=400, detail="Invalid code")

        otp = db.query(OTP).filter(
            OTP.username == user.username, OTP.purpose == "password_reset"
        ).first()

        if not otp:
            raise HTTPException(status_code=400, detail="No reset code found. Request a new one.")
        if otp.expires_at < datetime.utcnow():
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
    def reset_password(db: Session, new_password: str, access_token: str):
        validate_password_strength(new_password)

        try:
            payload = jwt.decode(access_token, JWT_SECRET, algorithms=[ALGORITHM], issuer="lendflow-api")
            if payload.get("purpose") != "password_reset":
                raise HTTPException(status_code=401, detail="Invalid token purpose")
            username = payload.get("sub")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid reset token")

        user = db.query(User).filter(User.username == username).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.password_hash = get_password_hash(new_password)
        user.refresh_token = None
        user.refresh_token_expires_at = None

        _audit(db, "password_reset", username=user.username, user_id=user.id, resource_type="user")
        db.commit()
        return {"status": 200, "message": "Password reset successful"}

    # ── Login via phone OTP ──────────────────────

    @staticmethod
    def send_login_phone_otp(db: Session, phone_number: str):
        normalized = normalizePhoneNumber(phone_number)
        if not normalized:
            raise HTTPException(status_code=400, detail="Invalid phone number")

        user = (
            db.query(User).filter(User.phone_number == normalized).first()
            or db.query(User).filter(User.phone_number == phone_number).first()
        )
        if not user:
            # Enumerate-safe: always return 200
            return {"status": 200, "message": "If this number is registered, a code has been sent."}

        code = _generate_otp_code()
        hashed = _hash_otp(code)

        existing = db.query(OTP).filter(
            OTP.username == user.username, OTP.purpose == "login_otp"
        ).first()
        if existing:
            existing.code_hash = hashed
            existing.phone_number = normalized
            existing.expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES)
            existing.attempts = 0
        else:
            db.add(OTP(
                username=user.username,
                phone_number=normalized,
                code_hash=hashed,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES),
                purpose="login_otp",
            ))
        db.commit()

        logger.debug(f"[DEV ONLY] Login OTP for {user.username}: {code}")
        message = f"Your Welend sign-in code is: {code}. Expires in {OTP_EXPIRE_MINUTES} minutes."
        threading.Thread(target=send_sms, args=(normalized, message), daemon=True).start()

        return {"status": 200, "message": "If this number is registered, a code has been sent."}

    @staticmethod
    def verify_login_phone_otp(
        db: Session,
        phone_number: str,
        code: str,
        ip_address: str = "unknown",
        portal: str | None = None,
    ):
        normalized = normalizePhoneNumber(phone_number) or phone_number

        user = (
            db.query(User).filter(User.phone_number == normalized).first()
            or db.query(User).filter(User.phone_number == phone_number).first()
        )
        if not user:
            raise HTTPException(status_code=400, detail="Invalid code")

        otp = db.query(OTP).filter(
            OTP.username == user.username, OTP.purpose == "login_otp"
        ).first()
        if not otp:
            raise HTTPException(status_code=400, detail="No code found. Request a new one.")
        if otp.expires_at < datetime.utcnow():
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

        AuthRepo._enforce_portal_role(user, portal)

        db.delete(otp)

        # Issue tokens
        access_token = create_access_token({"sub": user.username, "role": user.role})
        refresh = secrets.token_urlsafe(64)
        user.refresh_token = refresh
        user.refresh_token_expires_at = datetime.utcnow() + timedelta(days=7)

        _audit(db, "login_otp", username=user.username, user_id=user.id, ip_address=ip_address)
        db.commit()

        return {
            "access_token": access_token,
            "refresh_token": refresh,
            "user": {
                "id": str(user.id),
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "phone_number": user.phone_number,
                "role": user.role,
                "is_active": user.is_active,
                "is_verified": user.is_verified,
                "is_phone_verified": user.is_phone_verified,
                "is_kyc_verified": user.is_kyc_verified,
                "kyc_status": user.kyc_status,
                "credit_score": user.credit_score,
            },
        }

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
