"""
Wallet router — balance, transactions, deposit, withdraw.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database.tables import User, Wallet, WalletTransaction
from repository.auth_repo import get_password_hash, verify_password, _audit
from repository.dependencies import get_db, current_active_user
from repository.models import (
    WalletSetupModel,
    WalletDepositModel,
    WalletWithdrawModel,
    WalletCardDepositInitiateModel,
    WalletBankWithdrawInitiateModel,
)
from helpers import generateUniqueId
from utils.upg_client import UPGClient, _detect_carrier

router = APIRouter(prefix="/wallet", tags=["Wallet"])


@router.get("/")
async def get_wallet(db: Session = Depends(get_db), user: User = Depends(current_active_user)):
    """Get current user's wallet balance."""
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if not wallet:
        return {
            "balance": 0,
            "currency": "UGX",
            "is_wallet_setup": False,
        }
    return {
        "id": wallet.id,
        "balance": wallet.balance,
        "currency": wallet.currency,
        "is_wallet_setup": wallet.is_wallet_setup,
        "created_at": str(wallet.created_at),
    }


@router.post("/setup")
async def setup_wallet(
    data: WalletSetupModel,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Setup wallet with a transaction PIN."""
    existing = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if existing and existing.is_wallet_setup:
        raise HTTPException(status_code=400, detail="Wallet already set up")

    if existing:
        existing.pin_hash = get_password_hash(data.pin)
        existing.is_wallet_setup = True
    else:
        wallet = Wallet(
            user_id=user.id,
            pin_hash=get_password_hash(data.pin),
            is_wallet_setup=True,
        )
        db.add(wallet)

    _audit(db, "wallet_setup", username=user.username, user_id=user.id, resource_type="wallet")
    db.commit()
    return {"status": 200, "message": "Wallet set up successfully"}


@router.post("/deposit")
async def deposit(
    data: WalletDepositModel,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Deposit funds from mobile money to wallet."""
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if not wallet or not wallet.is_wallet_setup:
        raise HTTPException(status_code=400, detail="Please set up your wallet first")

    phone = data.phone_number or user.phone_number
    if not phone:
        raise HTTPException(status_code=400, detail="Phone number required for deposit")

    carrier = (data.carrier or _detect_carrier(phone)).upper()

    try:
        resp = UPGClient().collect(amount=data.amount, phone=phone, carrier=carrier)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Payment gateway error: {e}")

    if not UPGClient.is_success(resp):
        raise HTTPException(status_code=400, detail=resp.get("message", "Mobile money collection failed"))

    wallet.balance += data.amount
    tx = WalletTransaction(
        wallet_id=wallet.id,
        amount=data.amount,
        type="deposit",
        status="completed",
        description=f"Mobile money deposit ({carrier}) from {phone}",
        reference=UPGClient.transaction_id(resp) or generateUniqueId(15),
        counterparty=phone,
    )
    db.add(tx)
    _audit(db, "wallet_deposit", username=user.username, user_id=user.id,
           resource_type="wallet", details={"amount": data.amount, "phone": phone, "carrier": carrier})
    db.commit()

    return {"status": 200, "message": "Deposit successful", "balance": wallet.balance}


@router.post("/withdraw")
async def withdraw(
    data: WalletWithdrawModel,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Withdraw funds from wallet to mobile money."""
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if not wallet or not wallet.is_wallet_setup:
        raise HTTPException(status_code=400, detail="Please set up your wallet first")
    if wallet.balance < data.amount:
        raise HTTPException(status_code=400, detail="Insufficient funds")

    carrier = (data.carrier or _detect_carrier(data.phone_number)).upper()

    try:
        resp = UPGClient().disburse(amount=data.amount, phone=data.phone_number, carrier=carrier)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Payment gateway error: {e}")

    if not UPGClient.is_success(resp):
        raise HTTPException(status_code=400, detail=resp.get("message", "Mobile money disbursement failed"))

    wallet.balance -= data.amount
    tx = WalletTransaction(
        wallet_id=wallet.id,
        amount=data.amount,
        type="withdrawal",
        status="completed",
        description=f"Withdrawal ({carrier}) to {data.phone_number}",
        reference=UPGClient.transaction_id(resp) or generateUniqueId(15),
        counterparty=data.phone_number,
    )
    db.add(tx)
    _audit(db, "wallet_withdrawal", username=user.username, user_id=user.id,
           resource_type="wallet", details={"amount": data.amount, "to": data.phone_number, "carrier": carrier})
    db.commit()

    return {"status": 200, "message": "Withdrawal successful", "balance": wallet.balance}


@router.get("/transactions")
async def list_transactions(
    skip: int = 0,
    limit: int = 50,
    type: str = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """List wallet transactions."""
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if not wallet:
        return {"total": 0, "transactions": []}

    query = db.query(WalletTransaction).filter(WalletTransaction.wallet_id == wallet.id)
    if type:
        query = query.filter(WalletTransaction.type == type)

    total = query.count()
    txs = query.order_by(WalletTransaction.created_at.desc()).offset(skip).limit(limit).all()

    return {
        "total": total,
        "transactions": [
            {
                "id": tx.id,
                "amount": tx.amount,
                "type": tx.type,
                "status": tx.status,
                "description": tx.description,
                "reference": tx.reference,
                "counterparty": tx.counterparty,
                "created_at": str(tx.created_at),
            }
            for tx in txs
        ],
    }


# ═══════════════════════════════════════
#  Card deposit (Flutterwave hosted checkout — async)
# ═══════════════════════════════════════

@router.post("/deposit/card/initiate")
async def initiate_card_deposit(
    data: WalletCardDepositInitiateModel,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Start a card deposit — returns a hosted Flutterwave checkout URL.
    The transaction stays 'pending' until /deposit/card/status/{reference} confirms it.
    """
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if not wallet or not wallet.is_wallet_setup:
        raise HTTPException(status_code=400, detail="Please set up your wallet first")
    if not user.email:
        raise HTTPException(status_code=400, detail="A verified email is required for card deposits")

    try:
        resp = UPGClient().checkout(
            amount=data.amount,
            customer_email=user.email,
            customer_name=user.full_name,
            customer_phone=user.phone_number,
            redirect_url=data.redirect_url,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Payment gateway error: {e}")

    reference = resp.get("transaction_id")
    checkout_url = resp.get("checkout_url")
    if not reference or not checkout_url:
        raise HTTPException(status_code=502, detail="Payment gateway did not return a checkout link")

    tx = WalletTransaction(
        wallet_id=wallet.id,
        amount=data.amount,
        type="deposit",
        status="pending",
        description="Card deposit (Flutterwave)",
        reference=reference,
    )
    db.add(tx)
    _audit(db, "wallet_card_deposit_initiated", username=user.username, user_id=user.id,
           resource_type="wallet", details={"amount": data.amount, "reference": reference})
    db.commit()

    return {"checkout_url": checkout_url, "reference": reference}


@router.get("/deposit/card/status/{reference}")
async def get_card_deposit_status(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Poll a card deposit's status. Finalizes (credits balance) the first time it observes SUCCESS."""
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    tx = db.query(WalletTransaction).filter(
        WalletTransaction.reference == reference,
        WalletTransaction.wallet_id == wallet.id,
    ).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if tx.status != "pending":
        return {"status": tx.status, "balance": wallet.balance}

    try:
        resp = UPGClient().get_transaction(reference)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Payment gateway error: {e}")

    # /transactions/{id} status is UPPERCASE, unlike /collect, /disburse, /checkout, /payout.
    upg_status = (resp.get("status") or "").upper()

    if upg_status == "SUCCESS":
        wallet.balance += tx.amount
        tx.status = "completed"
        _audit(db, "wallet_card_deposit_completed", username=user.username, user_id=user.id,
               resource_type="wallet", details={"amount": tx.amount, "reference": reference})
        db.commit()
    elif upg_status in ("FAILED", "REVERSED"):
        tx.status = "failed"
        db.commit()
    # PENDING / PROCESSING: leave as-is, caller keeps polling.

    return {"status": tx.status, "balance": wallet.balance}


# ═══════════════════════════════════════
#  Bank transfer withdrawal (Flutterwave payout — async)
# ═══════════════════════════════════════

@router.get("/banks/{country_code}")
async def list_payout_banks(
    country_code: str,
    user: User = Depends(current_active_user),
):
    """List valid bank/mobile-money payout destinations for a country (e.g. UG)."""
    try:
        resp = UPGClient().list_banks(country_code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Payment gateway error: {e}")
    return resp


@router.post("/withdraw/bank/initiate")
async def initiate_bank_withdraw(
    data: WalletBankWithdrawInitiateModel,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Start a bank-transfer withdrawal. Balance is only debited once
    /withdraw/bank/status/{reference} confirms success — never on initiate.
    """
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if not wallet or not wallet.is_wallet_setup:
        raise HTTPException(status_code=400, detail="Please set up your wallet first")
    if wallet.balance < data.amount:
        raise HTTPException(status_code=400, detail="Insufficient funds")

    try:
        resp = UPGClient().payout(
            amount=data.amount,
            account_bank=data.account_bank,
            account_number=data.account_number,
            beneficiary_name=data.beneficiary_name,
            narration=data.narration,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Payment gateway error: {e}")

    reference = resp.get("reference") or resp.get("transaction_id")
    if not reference:
        raise HTTPException(status_code=502, detail="Payment gateway did not return a transaction reference")

    tx = WalletTransaction(
        wallet_id=wallet.id,
        amount=data.amount,
        type="withdrawal",
        status="pending",
        description=f"Bank transfer to {data.beneficiary_name}",
        reference=reference,
        counterparty=data.account_number,
    )
    db.add(tx)
    _audit(db, "wallet_bank_withdraw_initiated", username=user.username, user_id=user.id,
           resource_type="wallet", details={"amount": data.amount, "reference": reference})
    db.commit()

    return {"reference": reference, "status": "pending"}


@router.get("/withdraw/bank/status/{reference}")
async def get_bank_withdraw_status(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Poll a bank withdrawal's status. Debits the balance the first time it observes success."""
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    tx = db.query(WalletTransaction).filter(
        WalletTransaction.reference == reference,
        WalletTransaction.wallet_id == wallet.id,
    ).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if tx.status != "pending":
        return {"status": tx.status, "balance": wallet.balance}

    try:
        resp = UPGClient().get_payout_status(reference)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Payment gateway error: {e}")

    # /payout/{reference} status is lowercase, matching /collect, /disburse, /checkout.
    upg_status = (resp.get("status") or "").lower()

    if upg_status == "success":
        if wallet.balance < tx.amount:
            # Funds moved elsewhere since initiate — flag rather than push the balance negative.
            tx.status = "failed"
            tx.description = (tx.description or "") + " (insufficient balance at settlement)"
        else:
            wallet.balance -= tx.amount
            tx.status = "completed"
            _audit(db, "wallet_bank_withdraw_completed", username=user.username, user_id=user.id,
                   resource_type="wallet", details={"amount": tx.amount, "reference": reference})
        db.commit()
    elif upg_status == "failed":
        tx.status = "failed"
        db.commit()
    # pending / processing: leave as-is, caller keeps polling.

    return {"status": tx.status, "balance": wallet.balance}
