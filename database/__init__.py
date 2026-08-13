from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL

ENGINE = create_engine(
    DATABASE_URL,
    pool_size=20,
    max_overflow=50,
    pool_timeout=10,
    pool_recycle=1800,
    pool_pre_ping=True,
)


@event.listens_for(ENGINE, "connect")
def _mark_connection_authorized(dbapi_connection, connection_record):
    """Every physical DB connection this app ever opens — an HTTP request
    via get_db(), the background scheduler's own SessionLocal() calls,
    anything else running as this process — is implicitly trusted. Marks it
    with @app_authorized so the wallets/wallet_transactions/
    platform_fee_transactions guard triggers (migration
    c9f4a2e7b6d1_wallets_balance_guard_trigger.py) let its writes to
    protected financial fields through. A raw connection opened OUTSIDE
    this app (a separate script, phpMyAdmin, a leaked credential used
    directly) never fires this listener and stays unauthorized — exactly
    the distinction those triggers exist to enforce.

    Set once per physical connection (this fires on connect, not on every
    pooled checkout) — a MySQL user-defined variable persists for the
    connection's lifetime, so it stays set across every request/job that
    later reuses this same pooled connection until it's recycled.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("SET @app_authorized = 1")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=ENGINE)
