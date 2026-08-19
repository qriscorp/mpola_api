"""
Collections engine — the daily job that actually moves loans through
active -> overdue -> defaulted, applies late fees, and sends payment
reminders. Before this file existed, those statuses were defined in the
schema and filtered/counted everywhere but nothing ever set them.

Admin-configurable via PlatformSetting (see routers/admin.py settings
endpoints) — all have sane defaults so the job works with zero configuration:
  - reminder_days_before_due   (default 3)   — notify borrower N days before next_payment_date
  - grace_period_days          (default 3)   — days past due before a loan flips to "overdue"
  - default_after_days         (default 60)  — days overdue before a loan flips to "defaulted"
  - late_fee_rate              (default 0.02) — one-time late fee, as a fraction of monthly_payment,
                                                 added to total_repayable when a loan first goes overdue

Also runs a weekly admin digest email (gated by the notif_weekly_digest
toggle on the admin Settings page) — see run_weekly_digest_job.
"""

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func

from database import SessionLocal
from database.tables import User, Loan, Repayment, LoanApplication, LoanOffer, LenderOfferTemplate, Guarantor, Wallet, WalletTransaction, PlatformFeeTransaction, PlatformSetting, AuditLog, DeactivatedAccount
from helpers import safe_isoformat
from logging_module import logger
from repository.auth_repo import _audit, _notify, _notify_admins, _send_email, _setting_enabled


def _setting(db, key: str, default: float) -> float:
    row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
    if not row:
        return default
    try:
        return float(row.value)
    except (TypeError, ValueError):
        return default


def run_collections_job() -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        reminder_days = _setting(db, "reminder_days_before_due", 3)
        grace_days = _setting(db, "grace_period_days", 3)
        default_days = _setting(db, "default_after_days", 60)
        late_fee_rate = _setting(db, "late_fee_rate", 0.02)

        _send_reminders(db, now, reminder_days)
        _flag_overdue(db, now, grace_days, late_fee_rate)
        _flag_defaulted(db, now, grace_days, default_days)
        _flag_expired_offers(db, now)
        _notify_low_balance_lenders(db, now)
        _recompute_borrower_credit_scores(db)
        _remind_pending_guarantors(db, now)
        _expire_stale_applications(db, now)
        _handle_stale_matched_offers(db, now)
        _purge_deactivated_accounts(db, now)

        db.commit()
    except Exception as e:
        logger.error(f"Collections job failed: {e}")
        db.rollback()
    finally:
        db.close()


def _send_reminders(db, now, reminder_days: float) -> None:
    """Nudge borrowers whose next instalment is due in `reminder_days` days."""
    window_start = now + timedelta(days=reminder_days)
    window_end = window_start + timedelta(days=1)
    loans = db.query(Loan).filter(
        Loan.status == "active",
        Loan.next_payment_date >= window_start,
        Loan.next_payment_date < window_end,
    ).all()
    for loan in loans:
        _notify(
            db, loan.borrower_id,
            title="Payment reminder",
            message=f"Your instalment of UGX {loan.next_payment_amount:,.0f} is due on "
                    f"{loan.next_payment_date.strftime('%d %b %Y')}.",
            type="payment_reminder",
            data={"loan_id": loan.id},
            pref_key="notif_payment_reminder",
        )


def _flag_overdue(db, now, grace_days: float, late_fee_rate: float) -> None:
    """Active loans past their due date (plus grace) become overdue, once, with a late fee."""
    cutoff = now - timedelta(days=grace_days)
    loans = db.query(Loan).filter(
        Loan.status == "active",
        Loan.next_payment_date.isnot(None),
        Loan.next_payment_date < cutoff,
    ).all()
    for loan in loans:
        # Charged on what's actually still owed for the overdue instalment
        # — if a partial payment already chipped away at it (see
        # make_repayment), next_payment_amount reflects the real shortfall,
        # not the original full instalment.
        outstanding = loan.next_payment_amount if loan.next_payment_amount is not None else loan.monthly_payment
        late_fee = round((outstanding or 0) * late_fee_rate, 2)
        loan.status = "overdue"
        if late_fee > 0:
            loan.total_repayable = (loan.total_repayable or 0) + late_fee
            # Tracked separately from total_repayable so make_repayment can tell
            # how much of a future payment is "late fee" vs principal/interest —
            # only the late-fee portion gets the platform's extra cut.
            loan.late_fee_amount = (loan.late_fee_amount or 0) + late_fee

        _audit(db, "loan_marked_overdue", resource_type="loan", resource_id=loan.id,
               details={"late_fee": late_fee, "due_date": safe_isoformat(loan.next_payment_date)})
        _notify(
            db, loan.borrower_id,
            title="Payment overdue",
            message=f"Your instalment was due on {loan.next_payment_date.strftime('%d %b %Y')} "
                    f"and is now overdue" + (f" — a UGX {late_fee:,.0f} late fee has been added." if late_fee else "."),
            type="loan_overdue",
            data={"loan_id": loan.id},
        )
        _notify(
            db, loan.lender_id,
            title="Borrower payment overdue",
            message=f"A repayment on your UGX {loan.amount:,.0f} loan is now overdue.",
            type="loan_overdue",
            data={"loan_id": loan.id},
            pref_key="notif_loan_overdue",
        )
        # The lender who actually owns this loan is notified above — that's
        # the right person to act on it. Admins don't get a per-loan ping
        # (on a busy platform that's dozens of alerts a day); they see the
        # weekly digest total instead (see run_weekly_digest_job).


def _flag_defaulted(db, now, grace_days: float, default_days: float) -> None:
    """Loans overdue for longer than `default_days` (counted from the missed due
    date) are marked defaulted — the terminal collections state.
    """
    cutoff = now - timedelta(days=default_days)
    loans = db.query(Loan).filter(
        Loan.status == "overdue",
        Loan.next_payment_date.isnot(None),
        Loan.next_payment_date < cutoff,
    ).all()
    for loan in loans:
        loan.status = "defaulted"
        _audit(db, "loan_marked_defaulted", resource_type="loan", resource_id=loan.id,
               details={"days_overdue": default_days})
        _notify(
            db, loan.borrower_id,
            title="Loan defaulted",
            message=f"Your UGX {loan.amount:,.0f} loan has been marked as defaulted due to non-payment.",
            type="loan_defaulted",
            data={"loan_id": loan.id},
        )
        _notify(
            db, loan.lender_id,
            title="Loan defaulted",
            message=f"A borrower has defaulted on your UGX {loan.amount:,.0f} loan.",
            type="loan_defaulted",
            data={"loan_id": loan.id},
            pref_key="notif_loan_overdue",
        )
        # Same reasoning as overdue above — the lender is notified directly;
        # admins get the weekly digest total instead of a per-loan ping.


def _flag_expired_offers(db, now) -> None:
    """Approved standing offers whose valid_until has passed silently stop
    matching (see _template_matches in routers/loans.py) — nothing else
    would ever tell the lender that happened. Ping them once per expiry so
    they know to extend it (PUT /loans/offer-templates/{id}/expiry) if they
    want it live again. expiry_notified resets to False whenever that
    endpoint is called, so a later re-expiry can notify again."""
    templates = db.query(LenderOfferTemplate).filter(
        LenderOfferTemplate.status == "approved",
        LenderOfferTemplate.valid_until.isnot(None),
        LenderOfferTemplate.valid_until < now,
        LenderOfferTemplate.expiry_notified.is_(False),
    ).all()
    for template in templates:
        template.expiry_notified = True
        _audit(db, "offer_template_expired", resource_type="lender_offer_template",
               resource_id=template.id, details={"valid_until": safe_isoformat(template.valid_until)})
        _notify(
            db, template.lender_id,
            title="Standing offer expired",
            message=f"Your standing offer (UGX {template.min_amount:,.0f}–{template.max_amount:,.0f} "
                    f"at {template.interest_rate}%/month) has expired and will no longer be matched to "
                    f"borrowers. Extend its expiry date to bring it back.",
            type="offer_template_expired",
            data={"template_id": template.id},
        )


def _handle_stale_matched_offers(db, now) -> None:
    """Auto-matched (template-originated) offers that just sit "pending"
    forever aren't great for either side — the borrower may not realize
    it's waiting, and the lender has no way to know they can step in. Two
    thresholds, both scoped to template_id-is-not-null offers only (a
    lender's own manual offer never gets either treatment):

      - AUTO_MATCH_MANUAL_OFFER_COOLDOWN (2 days, see routers/loans.py) —
        the same moment the lender becomes allowed to make a competing
        manual offer, nudge the borrower to respond and tell the lender
        they can now act. One-time per offer (LoanOffer.stale_notified).
      - matched_offer_expiry_days (admin-configurable, default 14) — give
        up on it: auto-expire so it doesn't sit "pending" indefinitely,
        and tell both sides.
    """
    from routers.loans import AUTO_MATCH_MANUAL_OFFER_COOLDOWN

    reminder_cutoff = now - AUTO_MATCH_MANUAL_OFFER_COOLDOWN
    due_for_reminder = db.query(LoanOffer).filter(
        LoanOffer.status == "pending",
        LoanOffer.template_id.isnot(None),
        LoanOffer.created_at <= reminder_cutoff,
        LoanOffer.stale_notified.is_(False),
    ).all()
    for offer in due_for_reminder:
        offer.stale_notified = True
        app = offer.application
        if not app:
            continue
        cooldown_days = AUTO_MATCH_MANUAL_OFFER_COOLDOWN.days
        _notify(
            db, app.borrower_id,
            title="You have a pending offer",
            message=f"A lender auto-matched your UGX {offer.amount:,.0f} loan request "
                    f"{cooldown_days} days ago and is still waiting on your response — "
                    f"review it before they consider offering someone else.",
            type="offer_awaiting_response",
            data={"application_id": app.id, "offer_id": offer.id},
        )
        _notify(
            db, offer.lender_id,
            title="Still awaiting borrower response",
            message=f"Your standing offer to {app.borrower.full_name if app.borrower else 'a borrower'} "
                    f"(UGX {offer.amount:,.0f}) is still pending after {cooldown_days} days — "
                    f"you can now make a manual offer on this request if you'd like.",
            type="auto_match_cooldown_lifted",
            data={"application_id": app.id, "offer_id": offer.id},
        )

    expiry_days = _setting(db, "matched_offer_expiry_days", 14)
    expiry_cutoff = now - timedelta(days=expiry_days)
    expired = db.query(LoanOffer).filter(
        LoanOffer.status == "pending",
        LoanOffer.template_id.isnot(None),
        LoanOffer.created_at <= expiry_cutoff,
    ).all()
    for offer in expired:
        offer.status = "expired"
        app = offer.application
        _audit(db, "offer_auto_expired", resource_type="loan_offer", resource_id=offer.id,
               details={"application_id": offer.application_id, "days_pending": expiry_days})
        if app:
            _notify(
                db, app.borrower_id,
                title="Offer expired",
                message=f"A lender's UGX {offer.amount:,.0f} auto-matched offer on your loan request "
                        f"expired after {expiry_days} days without a response. You can still be "
                        f"matched to other lenders.",
                type="offer_expired",
                data={"application_id": app.id, "offer_id": offer.id},
            )
        _notify(
            db, offer.lender_id,
            title="Your auto-matched offer expired",
            message=f"Your standing offer to {app.borrower.full_name if app and app.borrower else 'a borrower'} "
                    f"(UGX {offer.amount:,.0f}) expired unaccepted after {expiry_days} days.",
            type="offer_expired",
            data={"application_id": offer.application_id, "offer_id": offer.id},
        )


def _reconcile_pending_payments() -> None:
    """Runs every ~2 min. Card deposits and bank withdrawals are async UPG
    flows that only ever finalized when the CLIENT polled /status/{reference}
    — if the tab/app closed mid-flow, the transaction sat 'pending' forever
    with nothing else to finalize it. UPG has no webhook wired up to us, so
    this is a poll-based reconciliation loop instead (standard practice when
    webhook infra isn't in place): re-check each still-pending tx the same
    way the client-side polling endpoints do, and finalize via the same
    shared functions those endpoints use — so a resolution here still fires
    _notify() -> WebSocket -> frontend invalidation, independent of whether
    the client is still around.

    Mobile money deposit/withdraw normally resolve synchronously inline —
    but a request whose response never made it back (worker restart,
    network blip, a slow-Interswitch timeout) leaves its transaction row
    "pending" with no client left to poll it, same class of gap as the
    card/bank flows above. Those rows are identified by having no
    `reference` yet (card/bank always get one immediately at initiate,
    before going pending) and are re-checked the same way, safely, since
    the recheck re-POSTs with the transaction's own id as the Idempotency-
    Key rather than triggering a second real-money transfer.
    """
    from routers.wallet import (
        _recheck_card_deposit, _recheck_bank_withdrawal,
        _recheck_mobile_deposit, _recheck_mobile_withdrawal,
    )
    from database.tables import Wallet, WalletTransaction

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expiry_hours = _setting(db, "payment_pending_expiry_hours", 48)
        cutoff = now - timedelta(hours=expiry_hours)

        pending_rows = db.query(WalletTransaction.id, WalletTransaction.type, WalletTransaction.reference).filter(
            WalletTransaction.status == "pending",
            WalletTransaction.type.in_(["deposit", "withdrawal"]),
            WalletTransaction.created_at >= cutoff,
        ).all()

        for tx_id, tx_type, reference in pending_rows:
            try:
                # Both helpers for a given branch lock the transaction row
                # before touching it, so if the owner happens to be polling
                # /status/{reference} (card/bank) for this exact transaction
                # at the same moment, one of us blocks and no-ops instead of
                # both finalizing (double-crediting) it.
                if tx_type == "deposit":
                    if reference:
                        _recheck_card_deposit(db, tx_id)
                    else:
                        _recheck_mobile_deposit(db, tx_id)
                else:
                    if reference:
                        _recheck_bank_withdrawal(db, tx_id)
                    else:
                        _recheck_mobile_withdrawal(db, tx_id)
                db.commit()
            except Exception as e:
                db.rollback()
                logger.warning(f"Reconciliation check failed for tx {tx_id}: {e}")
                continue  # leave pending, retry next run

        # Transactions stuck pending past the expiry window are almost certainly
        # abandoned (e.g. user closed the checkout tab) — stop re-checking them
        # forever and let the user know rather than leaving it silently stuck.
        expired = db.query(WalletTransaction).filter(
            WalletTransaction.status == "pending",
            WalletTransaction.type.in_(["deposit", "withdrawal"]),
            WalletTransaction.created_at < cutoff,
        ).with_for_update().all()
        for tx in expired:
            if tx.status != "pending":
                continue  # resolved by the loop above (or a concurrent poll) between the two queries
            wallet = db.query(Wallet).filter(Wallet.id == tx.wallet_id).first()
            if not wallet:
                continue
            tx.status = "failed"
            tx.description = (tx.description or "") + f" (expired after {expiry_hours}h unconfirmed)"
            _audit(db, "wallet_tx_expired", user_id=wallet.user_id,
                   resource_type="wallet", details={"reference": tx.reference, "type": tx.type})
            _notify(
                db, wallet.user_id,
                title="Transaction expired",
                message=f"Your {tx.type} of UGX {tx.amount:,.0f} could not be confirmed within "
                        f"{expiry_hours}h and has been marked as failed. If you were charged, contact support.",
                type="payment",
            )

        db.commit()
    except Exception as e:
        logger.error(f"Payment reconciliation job failed: {e}")
        db.rollback()
    finally:
        db.close()


WALLET_DRIFT_FULL_SWEEP_INTERVAL = timedelta(hours=24)


def _get_setting_str(db, key: str) -> str | None:
    row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
    return row.value if row else None


def _set_setting_str(db, key: str, value: str) -> None:
    row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(PlatformSetting(key=key, value=value))


def _check_wallet_drift() -> None:
    """Runs every ~2 min. A wallet's stored balance should always exactly
    equal the running sum of its own completed WalletTransaction rows (see
    compute_wallet_drift in routers/admin.py, the same check GET
    /admin/reconciliation displays on demand) — any mismatch means a bug or
    an out-of-band DB edit happened, which is serious enough on a platform
    holding real money that it isn't left for an admin to notice next time
    they happen to open that page. Any wallet found drifted is frozen
    immediately (blocking every money-moving action on it — see
    _ensure_wallet_not_frozen in routers/wallet.py) until an admin reviews
    and manually unfreezes it via PUT /admin/wallets/{username}/freeze —
    that endpoint is the ONLY way to lift a freeze, auto or manual, so this
    can never resolve itself silently. Both the wallet owner and every admin
    are notified so it can't go unnoticed either.

    Stays cheap as the platform grows: most runs only fully recompute
    wallets with WalletTransaction activity (new rows, or a status change
    like pending -> completed) since the last run — tracked via the
    last_wallet_drift_check_at PlatformSetting — instead of every wallet's
    entire history every 2 minutes. Every legitimate balance change writes
    or updates a WalletTransaction in the same commit (see
    _ensure_wallet_not_frozen / the wallets_guard DB trigger), so a wallet
    with no new activity can't have newly drifted through the app. As a
    safety net against anything that slips through that assumption (e.g.
    a direct DB edit crafted to also satisfy the trigger), a full
    all-wallets sweep still runs at least once every
    WALLET_DRIFT_FULL_SWEEP_INTERVAL regardless of activity.
    """
    from routers.admin import compute_wallet_drift

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        last_check_str = _get_setting_str(db, "last_wallet_drift_check_at")
        last_full_sweep_str = _get_setting_str(db, "last_wallet_drift_full_sweep_at")
        last_full_sweep = datetime.fromisoformat(last_full_sweep_str) if last_full_sweep_str else None
        do_full_sweep = last_full_sweep is None or (now - last_full_sweep) >= WALLET_DRIFT_FULL_SWEEP_INTERVAL

        if do_full_sweep or not last_check_str:
            drifted = compute_wallet_drift(db)
        else:
            last_check = datetime.fromisoformat(last_check_str)
            active_wallet_ids = [
                row[0] for row in db.query(WalletTransaction.wallet_id)
                .filter(WalletTransaction.updated_at > last_check)
                .distinct()
                .all()
            ]
            drifted = compute_wallet_drift(db, wallet_ids=active_wallet_ids) if active_wallet_ids else []

        for d in drifted:
            wallet = db.query(Wallet).filter(Wallet.id == d["wallet_id"]).with_for_update().first()
            if not wallet or wallet.is_frozen:
                continue  # already frozen (this job earlier, or an admin) — don't re-freeze/re-notify every run
            user = db.query(User).filter(User.id == wallet.user_id).first()
            if not user:
                continue

            reason = (
                f"Automatic freeze — ledger drift detected. Stored balance "
                f"UGX {d['stored_balance']:,.0f} vs transaction ledger UGX "
                f"{d['ledger_balance']:,.0f} (delta UGX {d['delta']:,.0f})."
            )
            wallet.is_frozen = True
            wallet.frozen_reason = reason
            wallet.frozen_at = datetime.now(timezone.utc)
            wallet.frozen_by = "system"

            _audit(db, "wallet_auto_frozen", username="system", user_id=user.id,
                   resource_type="wallet", resource_id=wallet.id,
                   details={"stored_balance": d["stored_balance"], "ledger_balance": d["ledger_balance"], "delta": d["delta"]})
            _notify(
                db, user.id,
                title="Wallet frozen",
                message=(
                    f"Your wallet was automatically frozen — {reason} "
                    "Our team is reviewing; contact support if you have questions."
                ),
                type="wallet_frozen",
            )
            _notify_admins(
                db,
                title="Wallet auto-frozen — drift detected",
                message=f"{user.username}'s wallet was automatically frozen: {reason} Review via Reconciliation and unfreeze once resolved.",
                type="wallet_drift_alert",
            )
            db.commit()

        # Only advance the cursor after a fully successful run — if
        # anything above raised, next run should re-check the same window
        # rather than silently skip it.
        _set_setting_str(db, "last_wallet_drift_check_at", now.isoformat())
        if do_full_sweep:
            _set_setting_str(db, "last_wallet_drift_full_sweep_at", now.isoformat())
        db.commit()
    except Exception as e:
        logger.error(f"Wallet drift check failed: {e}")
        db.rollback()
    finally:
        db.close()


def _notify_low_balance_lenders(db, now) -> None:
    """Nudge actively-lending lenders whose balance can't cover another
    disbursement at their own recent pace. Re-arms (re-notifies) only after
    a cooldown once still-low, and clears once balance recovers — same
    notify-once shape as _flag_expired_offers, but re-armable since this is
    a recurring condition, not a one-time event."""
    lookback_days = _setting(db, "low_balance_lookback_days", 30)
    cooldown_days = _setting(db, "low_balance_notify_cooldown_days", 5)
    cutoff = now - timedelta(days=lookback_days)

    template_lenders = db.query(LenderOfferTemplate.lender_id).filter(
        LenderOfferTemplate.status == "approved",
        LenderOfferTemplate.is_frozen.is_(False),
        (LenderOfferTemplate.valid_until.is_(None)) | (LenderOfferTemplate.valid_until > now),
    ).distinct()
    disbursed_lenders = db.query(Loan.lender_id).filter(Loan.disbursed_at >= cutoff).distinct()
    active_lender_ids = {row[0] for row in template_lenders.all()} | {row[0] for row in disbursed_lenders.all()}

    for lender_id in active_lender_ids:
        wallet = db.query(Wallet).filter(Wallet.user_id == lender_id).first()
        if not wallet:
            continue

        count = db.query(func.count(Loan.id)).filter(
            Loan.lender_id == lender_id, Loan.disbursed_at >= cutoff
        ).scalar() or 0
        if count > 0:
            volume = db.query(func.sum(Loan.amount)).filter(
                Loan.lender_id == lender_id, Loan.disbursed_at >= cutoff
            ).scalar() or 0.0
            threshold = volume / count
        else:
            min_amt = db.query(func.min(LenderOfferTemplate.min_amount)).filter(
                LenderOfferTemplate.lender_id == lender_id,
                LenderOfferTemplate.status == "approved",
                LenderOfferTemplate.is_frozen.is_(False),
                (LenderOfferTemplate.valid_until.is_(None)) | (LenderOfferTemplate.valid_until > now),
            ).scalar()
            if min_amt is None:
                continue  # active only via a past disbursement outside the window with no live template
            threshold = min_amt

        if wallet.balance >= threshold:
            if wallet.low_balance_notified_at is not None:
                wallet.low_balance_notified_at = None  # recovered — allow immediate re-notify on next dip
            continue

        # MySQL DATETIME has no tz — a value round-tripped through the DB
        # comes back naive even though it was written from an aware `now`,
        # so compare naive-to-naive rather than mixing aware/naive (which
        # raises TypeError on subtraction).
        last_notified = wallet.low_balance_notified_at
        if last_notified is not None and \
           now.replace(tzinfo=None) - last_notified < timedelta(days=cooldown_days):
            continue  # still low, but within cooldown since last nudge

        wallet.low_balance_notified_at = now.replace(tzinfo=None)
        _audit(db, "lender_low_balance_notified", user_id=lender_id, resource_type="wallet",
               details={"balance": wallet.balance, "threshold": threshold})
        _notify(
            db, lender_id,
            title="Low wallet balance",
            message=f"Your wallet balance (UGX {wallet.balance:,.0f}) is below what you'd need to fund "
                    f"another loan at your recent pace (~UGX {threshold:,.0f}). Top up to keep lending "
                    f"without interruption.",
            type="low_wallet_balance",
            data={"balance": wallet.balance, "threshold": threshold},
        )


def _recompute_borrower_credit_scores(db) -> None:
    """Real credit_score computation — this field existed and was already
    displayed to lenders everywhere (marketplace, applicant detail, admin)
    but nothing ever computed it, so it always showed 0. Runs daily (not
    event-triggered) — credit scores don't need per-event freshness, and a
    single daily pass both keeps everyone current and self-initializes every
    existing borrower the first time it runs."""
    borrower_ids = {row[0] for row in db.query(Loan.borrower_id).distinct().all()}
    now = datetime.now(timezone.utc)

    for borrower_id in borrower_ids:
        completed = db.query(func.count(Loan.id)).filter(
            Loan.borrower_id == borrower_id, Loan.status == "completed"
        ).scalar() or 0
        defaulted = db.query(func.count(Loan.id)).filter(
            Loan.borrower_id == borrower_id, Loan.status == "defaulted"
        ).scalar() or 0
        # late_fee_amount is set exactly once, the moment a loan first goes
        # overdue, and never clears — a ready-made "was this loan ever late" flag.
        ever_overdue = db.query(func.count(Loan.id)).filter(
            Loan.borrower_id == borrower_id,
            Loan.status.in_(["completed", "defaulted"]),
            Loan.late_fee_amount > 0,
        ).scalar() or 0
        currently_overdue = db.query(func.count(Loan.id)).filter(
            Loan.borrower_id == borrower_id, Loan.status == "overdue"
        ).scalar() or 0

        resolved = completed + defaulted
        if resolved == 0:
            score = 50.0  # neutral — no resolved track record yet, not "worst possible"
        else:
            ever_overdue_not_defaulted = max(0, ever_overdue - defaulted)  # a default already passed through overdue
            score = (
                30
                + 70 * (completed / resolved)
                - 60 * (defaulted / resolved)
                - 15 * (ever_overdue_not_defaulted / resolved)
            )
        if currently_overdue > 0:
            score -= 20
        score = max(0, min(100, round(score)))

        user = db.query(User).filter(User.id == borrower_id).first()
        if user and user.credit_score != score:
            user.credit_score = score


def _remind_pending_guarantors(db, now) -> None:
    """A guarantor who hasn't responded blocks the application from ever
    matching (see auto_match_offers_for_application) — the borrower can
    manually nudge them (POST /guarantors/{id}/remind), but many won't
    think to. This runs daily and does it for them: once a pending request
    has sat unanswered past `guarantor_reminder_after_hours`, nudge the
    guarantor to act AND tell the borrower it's still pending (so they know
    to follow up directly or replace them) — gated by the same
    last_reminded_at cooldown the manual endpoint uses, so the two paths
    can't be combined to spam anyone faster than the configured rate.
    """
    reminder_after_hours = _setting(db, "guarantor_reminder_after_hours", 24)
    reminder_cooldown_hours = _setting(db, "guarantor_reminder_cooldown_hours", 24)
    stale_cutoff = now - timedelta(hours=reminder_after_hours)

    pending = db.query(Guarantor).filter(
        Guarantor.status == "pending",
        Guarantor.created_at < stale_cutoff,
    ).all()

    for g in pending:
        if g.last_reminded_at:
            last = g.last_reminded_at.replace(tzinfo=timezone.utc)
            if now - last < timedelta(hours=reminder_cooldown_hours):
                continue

        app = db.query(LoanApplication).filter(LoanApplication.id == g.application_id).first()
        if not app or app.status != "awaiting_guarantors":
            continue

        g.last_reminded_at = now
        borrower = db.query(User).filter(User.id == app.borrower_id).first()
        guarantor_user = db.query(User).filter(User.id == g.guarantor_user_id).first()
        borrower_name = borrower.full_name or borrower.username if borrower else "Someone"
        guarantor_name = guarantor_user.full_name or guarantor_user.username if guarantor_user else "Your guarantor"

        _notify(
            db, g.guarantor_user_id,
            title="Reminder: guarantor request pending",
            message=f"{borrower_name} is still waiting for you to approve or decline "
                    f"their UGX {app.amount:,.0f} loan request.",
            type="guarantor_invite_received",
            data={"application_id": app.id},
        )
        _notify(
            db, app.borrower_id,
            title="Guarantor hasn't responded yet",
            message=f"{guarantor_name} hasn't responded to your guarantor request yet. "
                    f"You can send another reminder or replace them.",
            type="guarantor_still_pending",
            data={"application_id": app.id, "guarantor_id": g.id},
        )


def _expire_stale_applications(db, now) -> None:
    """A borrower can optionally cap how long their request stays live
    (LoanApplication.valid_until — most people who set this need funds
    urgently and don't want to be matched to a lender weeks after they've
    already moved on). Once that date passes, retire the request instead of
    letting it sit there: mark it expired, tell the borrower, and let any
    guarantor who still hasn't responded know they no longer need to —
    nothing further (matching, reminders) touches it after this. Requests
    with no valid_until set are untouched — they stay open indefinitely,
    exactly as before this feature existed."""
    stale = db.query(LoanApplication).filter(
        LoanApplication.status.in_(["awaiting_guarantors", "pending"]),
        LoanApplication.valid_until.isnot(None),
        LoanApplication.valid_until < now.replace(tzinfo=None),
    ).all()

    for app in stale:
        app.status = "expired"
        _audit(db, "application_expired", resource_type="loan_application", resource_id=app.id,
               details={"valid_until": safe_isoformat(app.valid_until)})
        _notify(
            db, app.borrower_id,
            title="Loan request expired",
            message=f"Your UGX {app.amount:,.0f} loan request has expired without being funded. "
                    f"You can submit a new request whenever you're ready.",
            type="application_expired",
            data={"application_id": app.id},
            pref_key="notif_application_status",
        )

        still_pending_guarantors = db.query(Guarantor).filter(
            Guarantor.application_id == app.id, Guarantor.status == "pending",
        ).all()
        for g in still_pending_guarantors:
            _notify(
                db, g.guarantor_user_id,
                title="Guarantor request no longer needed",
                message="The loan request you were asked to guarantee has expired — no need to respond.",
                type="guarantor_request_expired",
                data={"application_id": app.id},
            )


def _purge_deactivated_accounts(db, now) -> None:
    """The 30-day grace window promised to a deactivating user (both the
    self-service /users/me/deactivate flow and an admin's manual
    deactivation) — the User row itself is deleted immediately on
    deactivation, but this DeactivatedAccount stub (and the option to
    restore via POST /admin/users/{username}/restore) sticks around until
    its scheduled_deletion_date passes. This is the job that actually makes
    that date mean something instead of just being a number shown in the
    admin UI."""
    stale = db.query(DeactivatedAccount).filter(
        DeactivatedAccount.scheduled_deletion_date.isnot(None),
        DeactivatedAccount.scheduled_deletion_date < now.replace(tzinfo=None),
    ).all()

    for record in stale:
        _audit(
            db, "deactivated_account_purged",
            resource_type="deactivated_account", resource_id=record.id,
            details={"original_username": record.original_username, "original_email": record.original_email},
        )
        db.delete(record)


def run_weekly_digest_job() -> None:
    """Emails every admin a one-week performance summary. Gated by the
    "Weekly performance digest" toggle on the admin Settings page."""
    db = SessionLocal()
    try:
        if not _setting_enabled(db, "notif_weekly_digest"):
            return

        since = datetime.now(timezone.utc) - timedelta(days=7)

        new_users = db.query(func.count(User.id)).filter(User.created_at >= since).scalar() or 0
        new_applications = db.query(func.count(LoanApplication.id)).filter(
            LoanApplication.created_at >= since
        ).scalar() or 0
        disbursed_volume = db.query(func.sum(Loan.amount)).filter(Loan.disbursed_at >= since).scalar() or 0.0
        revenue = db.query(func.sum(PlatformFeeTransaction.platform_fee)).filter(
            PlatformFeeTransaction.created_at >= since
        ).scalar() or 0.0
        # Individual overdue/default events aren't pinged to admins in
        # real time (the lender on each loan already gets notified directly
        # — see _flag_overdue/_flag_defaulted) — this weekly total is how
        # admins stay aware without a per-loan flood.
        new_overdue = db.query(func.count(AuditLog.id)).filter(
            AuditLog.action == "loan_marked_overdue", AuditLog.created_at >= since
        ).scalar() or 0
        new_defaulted = db.query(func.count(AuditLog.id)).filter(
            AuditLog.action == "loan_marked_defaulted", AuditLog.created_at >= since
        ).scalar() or 0

        admins = db.query(User).filter(
            (User.is_admin == True) | (User.role.in_(["admin", "super_admin"]))
        ).all()

        subject = "Mpola — Weekly Performance Digest"
        html_body = f"""
        <h2>Mpola weekly digest</h2>
        <p>Last 7 days:</p>
        <ul>
          <li>New users: {new_users}</li>
          <li>New loan applications: {new_applications}</li>
          <li>Loans disbursed: UGX {disbursed_volume:,.0f}</li>
          <li>Platform revenue: UGX {revenue:,.0f}</li>
          <li>Loans newly overdue: {new_overdue}</li>
          <li>Loans newly defaulted: {new_defaulted}</li>
        </ul>
        """
        for admin in admins:
            if admin.email:
                _send_email(admin.email, subject, html_body)
    except Exception as e:
        logger.error(f"Weekly digest job failed: {e}")
    finally:
        db.close()


def run_lender_portfolio_digest_job() -> None:
    """Sends each lender who has the "Portfolio digest" toggle on (User.
    notif_portfolio_digest) a weekly summary of their own book — repayments
    received, new loans funded, and how many active/overdue loans they're
    carrying. Unlike run_weekly_digest_job (admin-wide, one email listing
    platform totals), this is per-lender and personal to their own
    portfolio, delivered as an in-app notification via the same _notify
    pref_key mechanism every other notif_* toggle already uses."""
    db = SessionLocal()
    try:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        lenders = db.query(User).filter(
            User.role == "lender", User.notif_portfolio_digest == True  # noqa: E712
        ).all()
        for lender in lenders:
            active_loans = db.query(func.count(Loan.id)).filter(
                Loan.lender_id == lender.id, Loan.status.in_(["active", "overdue"])
            ).scalar() or 0
            repayments_received = db.query(func.sum(Repayment.amount)).join(
                Loan, Repayment.loan_id == Loan.id
            ).filter(
                Loan.lender_id == lender.id, Repayment.created_at >= since, Repayment.status == "completed"
            ).scalar() or 0.0
            new_loans_funded = db.query(func.count(Loan.id)).filter(
                Loan.lender_id == lender.id, Loan.disbursed_at >= since
            ).scalar() or 0

            if not (repayments_received or new_loans_funded or active_loans):
                continue  # nothing worth reporting this week

            _notify(
                db, lender.id,
                title="Your weekly portfolio digest",
                message=(
                    f"UGX {repayments_received:,.0f} received in repayments this week"
                    + (f", {new_loans_funded} new loan{'s' if new_loans_funded != 1 else ''} funded" if new_loans_funded else "")
                    + f" — {active_loans} active loan{'s' if active_loans != 1 else ''} in your portfolio."
                ),
                type="portfolio_digest",
                pref_key="notif_portfolio_digest",
            )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Lender portfolio digest job failed: {e}")
    finally:
        db.close()


_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(run_collections_job, "interval", hours=24, id="collections_job", next_run_time=datetime.now(timezone.utc))
    # First run is a week out (not immediate like the collections job) so a
    # server restart never spams admins with an extra digest email.
    _scheduler.add_job(
        run_weekly_digest_job, "interval", weeks=1, id="weekly_digest_job",
        next_run_time=datetime.now(timezone.utc) + timedelta(weeks=1),
    )
    _scheduler.add_job(
        run_lender_portfolio_digest_job, "interval", weeks=1, id="lender_portfolio_digest_job",
        next_run_time=datetime.now(timezone.utc) + timedelta(weeks=1),
    )
    # Frequent (not daily) — closes the gap where a card deposit/bank withdrawal
    # only ever finalized when the client itself polled for status.
    _scheduler.add_job(
        _reconcile_pending_payments, "interval", minutes=2, id="payment_reconciliation_job",
        next_run_time=datetime.now(timezone.utc),
    )
    # Financial integrity — catches a wrong wallet balance fast and freezes
    # it before more money can move through it, rather than waiting for an
    # admin to happen to open the Reconciliation page. 2min is safe to run
    # this often because _check_wallet_drift only fully rescans wallets
    # with recent activity, not the whole platform's history every time.
    _scheduler.add_job(
        _check_wallet_drift, "interval", minutes=2, id="wallet_drift_check_job",
        next_run_time=datetime.now(timezone.utc),
    )
    _scheduler.start()
    logger.info("Collections scheduler started (runs every 24h, digest weekly, payment reconciliation every 2min, wallet drift check every 2min)")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
