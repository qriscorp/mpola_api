"""
Wallet router — balance, transactions, deposit, withdraw.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database.tables import User, Wallet, WalletTransaction, PlatformFeeTransaction
from repository.auth_repo import get_password_hash, verify_password, _audit, _notify
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
from utils.fee import calc_mobile_money_withdrawal_charges, calc_bank_withdrawal_charges

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
    # Locked for the rest of this request — a concurrent balance-mutating
    # request on this same wallet now blocks until this one commits, instead
    # of both computing their new balance from the same stale read and one
    # silently clobbering the other's credit/debit (lost update).
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).with_for_update().first()
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
    _notify(
        db, user.id,
        title="Deposit successful",
        message=f"UGX {data.amount:,.0f} was added to your wallet.",
        type="payment",
    )
    db.commit()

    return {"status": 200, "message": "Deposit successful", "balance": wallet.balance}


@router.post("/withdraw")
async def withdraw(
    data: WalletWithdrawModel,
    db: Session = Depends(get_db),
    user: User = Depends(current_active_user),
):
    """Withdraw funds from wallet to mobile money. A platform fee (0.5%) plus
    the Interswitch MTN/Airtel surcharge is charged on top, debited from the
    wallet — the recipient still receives the full amount requested.
    """
    # Locked for the rest of this request, held across the UPG payout call —
    # real money leaves via UPG below, so a concurrent withdrawal on this
    # wallet must wait rather than risk both passing the balance check and
    # sending out more than the wallet actually has.
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).with_for_update().first()
    if not wallet or not wallet.is_wallet_setup:
        raise HTTPException(status_code=400, detail="Please set up your wallet first")

    carrier = (data.carrier or _detect_carrier(data.phone_number)).upper()
    charges = calc_mobile_money_withdrawal_charges(data.amount, carrier)
    total_debit = data.amount + charges["total_fee"]

    if wallet.balance < total_debit:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient funds — you need UGX {total_debit:,.0f} (UGX {charges['total_fee']:,.0f} in fees included)",
        )

    try:
        resp = UPGClient().disburse(amount=data.amount, phone=data.phone_number, carrier=carrier)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Payment gateway error: {e}")

    if not UPGClient.is_success(resp):
        raise HTTPException(status_code=400, detail=resp.get("message", "Mobile money disbursement failed"))

    wallet.balance -= total_debit
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
    db.flush()
    db.add(PlatformFeeTransaction(
        user_id=user.id,
        wallet_transaction_id=tx.id,
        category="mobile_money_withdrawal",
        platform_fee=charges["platform_fee"],
        provider_fee=charges["provider_fee"],
        total_fee=charges["total_fee"],
    ))
    _audit(db, "wallet_withdrawal", username=user.username, user_id=user.id,
           resource_type="wallet", details={"amount": data.amount, "to": data.phone_number, "carrier": carrier, "fee": charges["total_fee"]})
    _notify(
        db, user.id,
        title="Withdrawal successful",
        message=f"UGX {data.amount:,.0f} was sent to {data.phone_number} (UGX {charges['total_fee']:,.0f} fee charged).",
        type="payment",
    )
    db.commit()

    return {
        "status": 200,
        "message": "Withdrawal successful",
        "balance": wallet.balance,
        "fee": charges["total_fee"],
        "total_debited": total_debit,
    }


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


def _finalize_card_deposit(db: Session, tx: WalletTransaction, wallet: Wallet, user: User) -> None:
    """UPG confirmed this card deposit succeeded — credit the wallet, audit,
    and notify. Shared by the client-poll endpoint below and the scheduler's
    reconciliation job, so a deposit finalizes the same way whether the
    client is still around to poll or not. Caller must db.commit()."""
    wallet.balance += tx.amount
    tx.status = "completed"
    _audit(db, "wallet_card_deposit_completed", username=user.username, user_id=user.id,
           resource_type="wallet", details={"amount": tx.amount, "reference": tx.reference})
    _notify(
        db, user.id,
        title="Deposit successful",
        message=f"UGX {tx.amount:,.0f} was added to your wallet via card.",
        type="payment",
    )


def _finalize_bank_withdrawal(db: Session, tx: WalletTransaction, wallet: Wallet, user: User) -> None:
    """UPG confirmed this bank payout succeeded — debit (or fail it if funds
    moved since initiate), audit, and notify. Shared by the client-poll
    endpoint below and the scheduler's reconciliation job. Caller must db.commit()."""
    charges = calc_bank_withdrawal_charges(tx.amount)
    total_debit = tx.amount + charges["total_fee"]
    if wallet.balance < total_debit:
        # Funds moved elsewhere since initiate — flag rather than push the balance negative.
        tx.status = "failed"
        tx.description = (tx.description or "") + " (insufficient balance at settlement)"
        return

    wallet.balance -= total_debit
    tx.status = "completed"
    db.add(PlatformFeeTransaction(
        user_id=user.id,
        wallet_transaction_id=tx.id,
        category="bank_withdrawal",
        platform_fee=charges["platform_fee"],
        provider_fee=charges["provider_fee"],
        total_fee=charges["total_fee"],
    ))
    _audit(db, "wallet_bank_withdraw_completed", username=user.username, user_id=user.id,
           resource_type="wallet", details={"amount": tx.amount, "reference": tx.reference, "fee": charges["total_fee"]})
    _notify(
        db, user.id,
        title="Withdrawal successful",
        message=f"UGX {tx.amount:,.0f} was sent to your bank account (UGX {charges['total_fee']:,.0f} fee charged).",
        type="payment",
    )


def _recheck_card_deposit(db: Session, tx_id: str) -> WalletTransaction | None:
    """Single entry point for 'is this card deposit done yet' — used by both
    the client-poll endpoint below AND the scheduler's reconciliation job.
    Both can reach the same still-pending transaction at nearly the same
    moment; locking the transaction row here means whichever caller loses
    the race simply blocks until the winner commits, then re-reads a
    status that's no longer "pending" and safely no-ops, instead of both
    calling UPG and both crediting the wallet for one deposit.
    """
    tx = db.query(WalletTransaction).filter(WalletTransaction.id == tx_id).with_for_update().first()
    if not tx or tx.status != "pending":
        return tx

    wallet = db.query(Wallet).filter(Wallet.id == tx.wallet_id).with_for_update().first()
    if not wallet:
        return tx
    user = db.query(User).filter(User.id == wallet.user_id).first()

    try:
        resp = UPGClient().get_transaction(tx.reference)
    except Exception:
        return tx  # transient gateway error — leave pending, next check retries

    # /transactions/{id} status is UPPERCASE, unlike /collect, /disburse, /checkout, /payout.
    upg_status = (resp.get("status") or "").upper()
    if upg_status == "SUCCESS":
        _finalize_card_deposit(db, tx, wallet, user)
    elif upg_status in ("FAILED", "REVERSED"):
        tx.status = "failed"
    return tx


def _recheck_bank_withdrawal(db: Session, tx_id: str) -> WalletTransaction | None:
    """Same purpose as _recheck_card_deposit, for bank payouts — used by both
    the client-poll endpoint below and the scheduler's reconciliation job."""
    tx = db.query(WalletTransaction).filter(WalletTransaction.id == tx_id).with_for_update().first()
    if not tx or tx.status != "pending":
        return tx

    wallet = db.query(Wallet).filter(Wallet.id == tx.wallet_id).with_for_update().first()
    if not wallet:
        return tx
    user = db.query(User).filter(User.id == wallet.user_id).first()

    try:
        resp = UPGClient().get_payout_status(tx.reference)
    except Exception:
        return tx  # transient gateway error — leave pending, next check retries

    # /payout/{reference} status is lowercase, matching /collect, /disburse, /checkout.
    upg_status = (resp.get("status") or "").lower()
    if upg_status == "success":
        _finalize_bank_withdrawal(db, tx, wallet, user)
    elif upg_status == "failed":
        tx.status = "failed"
    return tx


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

    if tx.status == "pending":
        # Swallows transient gateway errors and leaves the tx pending rather
        # than raising — same behavior the scheduler's reconciliation job
        # relies on, so both paths retry later instead of surfacing a 502
        # for what's usually just a slow/flaky upstream check.
        tx = _recheck_card_deposit(db, tx.id)
        db.commit()
        db.refresh(wallet)

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
    # Locked for the rest of this request — makes the "one pending withdrawal
    # at a time" check below actually reliable instead of a TOCTOU race where
    # two concurrent initiate calls both see "none pending" and both proceed.
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).with_for_update().first()
    if not wallet or not wallet.is_wallet_setup:
        raise HTTPException(status_code=400, detail="Please set up your wallet first")

    charges = calc_bank_withdrawal_charges(data.amount)
    total_debit = data.amount + charges["total_fee"]
    if wallet.balance < total_debit:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient funds — you need UGX {total_debit:,.0f} (UGX {charges['total_fee']:,.0f} in fees included)",
        )

    # Balance isn't reserved until settlement, so block a second withdrawal from
    # starting while one is still in flight — otherwise both could pass the balance
    # check above and each trigger a real payout before either is confirmed.
    has_pending_withdrawal = db.query(WalletTransaction).filter(
        WalletTransaction.wallet_id == wallet.id,
        WalletTransaction.type == "withdrawal",
        WalletTransaction.status == "pending",
    ).first()
    if has_pending_withdrawal:
        raise HTTPException(
            status_code=400,
            detail="You already have a withdrawal in progress — wait for it to complete first.",
        )

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
           resource_type="wallet", details={"amount": data.amount, "reference": reference, "fee": charges["total_fee"]})
    db.commit()

    return {"reference": reference, "status": "pending", "fee": charges["total_fee"], "total_debited": total_debit}


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

    if tx.status == "pending":
        # Swallows transient gateway errors and leaves the tx pending rather
        # than raising — same behavior the scheduler's reconciliation job
        # relies on, so both paths retry later instead of surfacing a 502
        # for what's usually just a slow/flaky upstream check.
        tx = _recheck_bank_withdrawal(db, tx.id)
        db.commit()
        db.refresh(wallet)

    fee_tx = db.query(PlatformFeeTransaction).filter(
        PlatformFeeTransaction.wallet_transaction_id == tx.id
    ).first()
    return {
        "status": tx.status,
        "balance": wallet.balance,
        "fee": fee_tx.total_fee if fee_tx else None,
    }
