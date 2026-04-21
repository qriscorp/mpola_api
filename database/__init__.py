from sqlalchemy import create_engine
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

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=ENGINE)
