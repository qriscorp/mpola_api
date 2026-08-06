"""
One-off script to create the very first admin account, or promote an
existing user to admin. There's no in-app way to do this: granting the
admin role (PATCH /admin/users/{username}/role) itself requires an existing
admin, so the first one has to be created here.

Usage:
    python scripts/create_admin.py --promote <username>
    python scripts/create_admin.py --create --username admin --email admin@mpola.co \
        --password <password> [--full-name "Mpola Admin"] [--super]
    python scripts/create_admin.py --grant-admin <username_or_email_or_phone> [--super]

--promote overwrites the account's role to "admin"/"super_admin" (legacy,
no portal identity — use for a pure admin account with no lender/borrower
dashboard). --grant-admin instead ADDS admin access on top of the account's
existing borrower/lender role, so e.g. a lender keeps using the lender
dashboard and can additionally switch into the admin dashboard.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal
from database.tables import User
from helpers import generateUniqueId
from repository.auth_repo import get_password_hash


def promote(username: str, super_admin: bool):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            print(f"No user found with username '{username}'.")
            return
        user.role = "super_admin" if super_admin else "admin"
        db.commit()
        print(f"'{username}' is now {user.role}.")
    finally:
        db.close()


def grant_admin(identifier: str, super_admin: bool):
    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(
                (User.username == identifier)
                | (User.email == identifier)
                | (User.phone_number == identifier)
            )
            .first()
        )
        if not user:
            print(f"No user found matching '{identifier}' (username/email/phone).")
            return
        user.is_admin = True
        user.is_super_admin = super_admin
        db.commit()
        level = "super_admin" if super_admin else "admin"
        print(
            f"'{user.username}' keeps role='{user.role}' and now also has {level} access. "
            "They must sign out and back in for the new access to take effect."
        )
    finally:
        db.close()


def create(username: str, email: str, password: str, full_name: str | None, super_admin: bool):
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == username).first():
            print(f"Username '{username}' is already taken.")
            return
        if db.query(User).filter(User.email == email).first():
            print(f"Email '{email}' is already registered.")
            return

        user = User(
            id=generateUniqueId(),
            username=username,
            email=email,
            password_hash=get_password_hash(password),
            full_name=full_name,
            role="super_admin" if super_admin else "admin",
            is_active=True,
            is_verified=True,
            is_kyc_verified=True,
            kyc_status="verified",
        )
        db.add(user)
        db.commit()
        print(f"Created {user.role} account '{username}'.")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--promote", metavar="USERNAME", help="Overwrite an existing user's role to admin (loses portal identity)")
    parser.add_argument("--grant-admin", metavar="IDENTIFIER", help="Grant admin access on top of an existing lender/borrower role (username, email, or phone)")
    parser.add_argument("--create", action="store_true", help="Create a brand-new admin account")
    parser.add_argument("--username")
    parser.add_argument("--email")
    parser.add_argument("--password")
    parser.add_argument("--full-name", default=None)
    parser.add_argument("--super", action="store_true", help="Grant super_admin instead of admin")
    args = parser.parse_args()

    if args.promote:
        promote(args.promote, args.super)
    elif args.grant_admin:
        grant_admin(args.grant_admin, args.super)
    elif args.create:
        if not (args.username and args.email and args.password):
            parser.error("--create requires --username, --email, and --password")
        create(args.username, args.email, args.password, args.full_name, args.super)
    else:
        parser.error("Pass --promote <username>, --grant-admin <identifier>, or --create with account details")
