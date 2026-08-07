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
from database.tables import User, Loan, LoanApplication, PlatformFeeTransaction, PlatformSetting, AuditLog
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
    _scheduler.start()
    logger.info("Collections scheduler started (runs every 24h, digest weekly)")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
