"""
Platform + payment-provider fee schedule — mirrors kumpi's model exactly,
since Mpola shares the same UPG (Unified Payment Gateway) and the same
underlying Interswitch (MTN/Airtel) and Flutterwave rails.

The 0.5% platform fee (Mpola's actual revenue — see calc_platform_fee) is
charged on FOUR kinds of transaction: mobile money withdrawal, bank
withdrawal, loan disbursement (lender pays it on top, borrower receives the
full amount), and loan repayment (borrower pays it on top, wallet-to-wallet).
No fee on deposits.

The provider surcharge below (Interswitch for mobile money, Flutterwave for
bank payouts) only applies to withdrawals, since those are the only two
categories that actually leave the platform via an external rail — loan
disbursement/repayment are wallet-to-wallet and never touch a provider.
That surcharge is collected from the withdrawing user and passed straight
through to the provider; it is NOT Mpola revenue.
"""

import math

# ── Platform fee ─────────────────────────────────────────────────────────
# 0.5% — Mpola's own cut, charged on every withdrawal, loan disbursement,
# and loan repayment, on top of the amount, absorbed by whoever is sending
# the money. This is the only line item that counts as platform revenue —
# see routers/admin.py's /admin/revenue endpoint.
TX_FEE_RATE = 0.005  # 0.5%


def calc_platform_fee(amount: float) -> int:
    """Platform's own cut — rounds up to the nearest UGX."""
    return math.ceil(amount * TX_FEE_RATE)


# ── Provider (Interswitch) fee — MTN/Airtel mobile money withdrawals ────
# Same tiered flat-fee schedule Interswitch charges kumpi.

def calc_mtn_fee(amount: float) -> int:
    if amount < 60_000:
        return 300
    if amount < 500_000:
        return 600
    if amount < 1_000_000:
        return 1_000
    return 1_200


def calc_airtel_fee(amount: float) -> int:
    if amount < 60_000:
        return 300
    if amount < 500_000:
        return 600
    return 1_000


def calc_mobile_money_provider_fee(amount: float, carrier: str) -> int:
    c = (carrier or "").upper()
    if c == "MTN":
        return calc_mtn_fee(amount)
    if c == "AIRTEL":
        return calc_airtel_fee(amount)
    return 0


# ── Provider (Flutterwave) fee — bank payouts ───────────────────────────
# Flat pass-through estimate for Flutterwave's own transfer surcharge —
# not queried live per-transfer, same approach kumpi takes.
FLUTTERWAVE_FEE_RATE = 0.03  # 3%


def calc_flutterwave_fee(amount: float) -> int:
    return math.ceil(amount * FLUTTERWAVE_FEE_RATE)


def calc_mobile_money_withdrawal_charges(amount: float, carrier: str) -> dict:
    """Platform fee + Interswitch MTN/Airtel surcharge for a mobile money withdrawal."""
    platform_fee = calc_platform_fee(amount)
    provider_fee = calc_mobile_money_provider_fee(amount, carrier)
    return {
        "platform_fee": platform_fee,
        "provider_fee": provider_fee,
        "total_fee": platform_fee + provider_fee,
    }


def calc_bank_withdrawal_charges(amount: float) -> dict:
    """Platform fee + Flutterwave surcharge for a bank payout."""
    platform_fee = calc_platform_fee(amount)
    provider_fee = calc_flutterwave_fee(amount)
    return {
        "platform_fee": platform_fee,
        "provider_fee": provider_fee,
        "total_fee": platform_fee + provider_fee,
    }
