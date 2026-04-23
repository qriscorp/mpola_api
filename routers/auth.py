"""
Auth router — all authentication endpoints.
IP address is captured for login attempt tracking + audit logs.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from repository.auth_repo import AuthRepo
from repository.dependencies import get_db, current_active_user
from repository.models import (
    Login, UserCreate, SendOTPModel, VerifyOTPModel,
    SendPhoneOTPModel, VerifyPhoneOTPModel,
    SendPasswordResetCodeModel, VerifyPasswordResetCodeModel,
    ResetPasswordModel, ChangePasswordModel,
    SendLoginPhoneOTPModel, VerifyLoginPhoneOTPModel,
    RegisterStart, SignupDraftRequest, SignupDraftPhoneRequest,
    SignupDraftVerifyRequest, SignupDraftVerifyPhoneRequest,
)
from database.tables import User

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _get_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/register")
async def register(data: UserCreate, request: Request, db: Session = Depends(get_db)):
    return AuthRepo.register(db, data, ip_address=_get_ip(request))


@router.post("/register_start")
async def register_start(data: RegisterStart, request: Request, db: Session = Depends(get_db)):
    return AuthRepo.register_start(db, data, ip_address=_get_ip(request))


@router.post("/send_signup_email_otp")
async def send_signup_email_otp(data: SignupDraftRequest, db: Session = Depends(get_db)):
    return AuthRepo.send_signup_email_otp(db, data.draft_id)


@router.post("/verify_signup_email_otp")
async def verify_signup_email_otp(data: SignupDraftVerifyRequest, db: Session = Depends(get_db)):
    return AuthRepo.verify_signup_email_otp(db, data.draft_id, data.code)


@router.post("/send_signup_phone_otp")
async def send_signup_phone_otp(data: SignupDraftPhoneRequest, db: Session = Depends(get_db)):
    return AuthRepo.send_signup_phone_otp(db, data.draft_id, data.phone_number)


@router.post("/verify_signup_phone_otp")
async def verify_signup_phone_otp(data: SignupDraftVerifyPhoneRequest, request: Request, db: Session = Depends(get_db)):
    return AuthRepo.verify_signup_phone_otp(
        db,
        data.draft_id,
        data.phone_number,
        data.code,
        ip_address=_get_ip(request),
    )


@router.post("/login")
async def login(data: Login, request: Request, db: Session = Depends(get_db)):
    return AuthRepo.login(db, data, ip_address=_get_ip(request))


@router.post("/refresh")
async def refresh(refresh_token: str, db: Session = Depends(get_db)):
    return AuthRepo.refresh_token(db, refresh_token)


@router.post("/send_otp")
async def send_otp(data: SendOTPModel, db: Session = Depends(get_db)):
    return AuthRepo.send_otp(db, data.username)


@router.post("/verify_otp")
async def verify_otp(data: VerifyOTPModel, db: Session = Depends(get_db)):
    return AuthRepo.verify_otp(db, data.username, data.code)


@router.post("/send_phone_otp")
async def send_phone_otp(data: SendPhoneOTPModel, db: Session = Depends(get_db)):
    return AuthRepo.send_phone_otp(db, data.username, data.phone_number)


@router.post("/verify_phone_otp")
async def verify_phone_otp(data: VerifyPhoneOTPModel, db: Session = Depends(get_db)):
    return AuthRepo.verify_phone_otp(db, data.username, data.phone_number, data.code)


@router.post("/send_password_reset_code")
async def send_reset_code(data: SendPasswordResetCodeModel, db: Session = Depends(get_db)):
    return AuthRepo.send_password_reset_code(db, data.identifier)


@router.post("/verify_password_reset_code")
async def verify_reset_code(data: VerifyPasswordResetCodeModel, db: Session = Depends(get_db)):
    return AuthRepo.verify_password_reset_code(db, data.identifier, data.code)


@router.post("/reset_password")
async def reset_password(data: ResetPasswordModel, db: Session = Depends(get_db)):
    return AuthRepo.reset_password(db, data.new_password, data.access_token)


@router.post("/send_login_phone_otp")
async def send_login_phone_otp(data: SendLoginPhoneOTPModel, db: Session = Depends(get_db)):
    return AuthRepo.send_login_phone_otp(db, data.phone_number)


@router.post("/verify_login_phone_otp")
async def verify_login_phone_otp(data: VerifyLoginPhoneOTPModel, request: Request, db: Session = Depends(get_db)):
    return AuthRepo.verify_login_phone_otp(
        db,
        data.phone_number,
        data.code,
        ip_address=_get_ip(request),
        portal=data.portal,
    )


@router.post("/change_password")
async def change_password(
    data: ChangePasswordModel,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    return AuthRepo.change_password(db, user, data.old_password, data.new_password)


@router.get("/me")
async def get_me(db: Session = Depends(get_db), user: User = Depends(current_active_user)):
    from repository.user_repo import UserRepo
    return UserRepo.get_user_by_username(db, user.username)


@router.get("/check-email-status/{email}")
async def check_email(email: str, db: Session = Depends(get_db)):
    return AuthRepo.check_email_status(db, email)


@router.get("/check-phone-number-status/{phone}")
async def check_phone(phone: str, db: Session = Depends(get_db)):
    return AuthRepo.check_phone_number_status(db, phone)


@router.get("/check-username-status/{username}")
async def check_username(username: str, db: Session = Depends(get_db)):
    return AuthRepo.check_username_status(db, username)
