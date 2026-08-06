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
"""

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from database import SessionLocal
from database.tables import Loan, PlatformSetting
from logging_module import logger
from repository.auth_repo import _audit, _notify


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
        )


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
        )


_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(run_collections_job, "interval", hours=24, id="collections_job", next_run_time=datetime.now(timezone.utc))
    _scheduler.start()
    logger.info("Collections scheduler started (runs every 24h)")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
