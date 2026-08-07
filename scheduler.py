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
from database.tables import User, Loan, LoanApplication, LenderOfferTemplate, Wallet, PlatformFeeTransaction, PlatformSetting, AuditLog
from logging_module import logger
from repository.auth_repo import _audit, _notify, _send_email, _setting_enabled


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
        late_fee = round((loan.monthly_payment or 0) * late_fee_rate, 2)
        loan.status = "overdue"
        if late_fee > 0:
            loan.total_repayable = (loan.total_repayable or 0) + late_fee
            # Tracked separately from total_repayable so make_repayment can tell
            # how much of a future payment is "late fee" vs principal/interest —
            # only the late-fee portion gets the platform's extra cut.
            loan.late_fee_amount = (loan.late_fee_amount or 0) + late_fee

        _audit(db, "loan_marked_overdue", resource_type="loan", resource_id=loan.id,
               details={"late_fee": late_fee, "due_date": str(loan.next_payment_date)})
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
               resource_id=template.id, details={"valid_until": str(template.valid_until)})
        _notify(
            db, template.lender_id,
            title="Standing offer expired",
            message=f"Your standing offer (UGX {template.min_amount:,.0f}–{template.max_amount:,.0f} "
                    f"at {template.interest_rate}%/month) has expired and will no longer be matched to "
                    f"borrowers. Extend its expiry date to bring it back.",
            type="offer_template_expired",
            data={"template_id": template.id},
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
    """
    from routers.wallet import _finalize_card_deposit, _finalize_bank_withdrawal
    from utils.upg_client import UPGClient
    from database.tables import Wallet, WalletTransaction

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expiry_hours = _setting(db, "payment_pending_expiry_hours", 48)
        cutoff = now - timedelta(hours=expiry_hours)

        pending = db.query(WalletTransaction).filter(
            WalletTransaction.status == "pending",
            WalletTransaction.type.in_(["deposit", "withdrawal"]),
            WalletTransaction.created_at >= cutoff,
        ).all()

        for tx in pending:
            wallet = db.query(Wallet).filter(Wallet.id == tx.wallet_id).first()
            user = db.query(User).filter(User.id == wallet.user_id).first() if wallet else None
            if not wallet or not user:
                continue
            try:
                # Only /deposit/card and /withdraw/bank ever leave a tx "pending" —
                # mobile money deposit/withdraw resolve synchronously — so type
                # alone tells us which UPG endpoint to re-check.
                if tx.type == "deposit":
                    resp = UPGClient().get_transaction(tx.reference)
                    upg_status = (resp.get("status") or "").upper()
                    if upg_status == "SUCCESS":
                        _finalize_card_deposit(db, tx, wallet, user)
                    elif upg_status in ("FAILED", "REVERSED"):
                        tx.status = "failed"
                else:
                    resp = UPGClient().get_payout_status(tx.reference)
                    upg_status = (resp.get("status") or "").lower()
                    if upg_status == "success":
                        _finalize_bank_withdrawal(db, tx, wallet, user)
                    elif upg_status == "failed":
                        tx.status = "failed"
            except Exception as e:
                logger.warning(f"Reconciliation check failed for tx {tx.id} ({tx.reference}): {e}")
                continue  # leave pending, retry next run

        # Transactions stuck pending past the expiry window are almost certainly
        # abandoned (e.g. user closed the checkout tab) — stop re-checking them
        # forever and let the user know rather than leaving it silently stuck.
        expired = db.query(WalletTransaction).filter(
            WalletTransaction.status == "pending",
            WalletTransaction.type.in_(["deposit", "withdrawal"]),
            WalletTransaction.created_at < cutoff,
        ).all()
        for tx in expired:
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
    # Frequent (not daily) — closes the gap where a card deposit/bank withdrawal
    # only ever finalized when the client itself polled for status.
    _scheduler.add_job(
        _reconcile_pending_payments, "interval", minutes=2, id="payment_reconciliation_job",
        next_run_time=datetime.now(timezone.utc),
    )
    _scheduler.start()
    logger.info("Collections scheduler started (runs every 24h, digest weekly, payment reconciliation every 2min)")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
