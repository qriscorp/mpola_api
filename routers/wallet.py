"""
Wallet router — balance, transactions, deposit, withdraw.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database.tables import User, Wallet, WalletTransaction
from repository.auth_repo import get_password_hash, verify_password, _audit
from repository.dependencies import get_db, current_active_user
from repository.models import WalletSetupModel, WalletDepositModel, WalletWithdrawModel
from helpers import generateUniqueId

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
    """Deposit funds to wallet."""
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if not wallet or not wallet.is_wallet_setup:
        raise HTTPException(status_code=400, detail="Please set up your wallet first")

    wallet.balance += data.amount
    tx = WalletTransaction(
        wallet_id=wallet.id,
        amount=data.amount,
        type="deposit",
        status="completed",
        description=f"Deposit of {data.amount} UGX",
        reference=generateUniqueId(15),
    )
    db.add(tx)
    _audit(db, "wallet_deposit", username=user.username, user_id=user.id,
           resource_type="wallet", details={"amount": data.amount})
    db.commit()

    return {"status": 200, "message": "Deposit successful", "balance": wallet.balance}


@router.post("/withdraw")
async def withdraw(
    data: WalletWithdrawModel,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Withdraw funds from wallet."""
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if not wallet or not wallet.is_wallet_setup:
        raise HTTPException(status_code=400, detail="Please set up your wallet first")
    if wallet.balance < data.amount:
        raise HTTPException(status_code=400, detail="Insufficient funds")

    wallet.balance -= data.amount
    tx = WalletTransaction(
        wallet_id=wallet.id,
        amount=data.amount,
        type="withdrawal",
        status="completed",
        description=f"Withdrawal of {data.amount} UGX to {data.phone_number}",
        reference=generateUniqueId(15),
        counterparty=data.phone_number,
    )
    db.add(tx)
    _audit(db, "wallet_withdrawal", username=user.username, user_id=user.id,
           resource_type="wallet", details={"amount": data.amount, "to": data.phone_number})
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
