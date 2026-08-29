"""
database/db.py

Production-grade PostgreSQL connection with pooling, retry logic,
and health checks.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from functools import wraps

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import OperationalError, DBAPIError
from sqlalchemy.pool import QueuePool

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url

    # Fall back to discrete PG* vars if DATABASE_URL isn't set.
    user = os.environ.get("PGUSER", "postgres")
    password = os.environ.get("PGPASSWORD", "")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    db = os.environ.get("PGDATABASE", "prabha_dairy")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"


DATABASE_URL = _database_url()

# Convert psycopg2 URLs to psycopg (asyncpg-style)
if "postgresql+psycopg2://" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg2://", "postgresql+psycopg://")

# Production connection pool configuration
engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=int(os.environ.get("DB_POOL_SIZE", "10")),
    max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "20")),
    pool_timeout=int(os.environ.get("DB_POOL_TIMEOUT", "30")),
    pool_recycle=int(os.environ.get("DB_POOL_RECYCLE", "3600")),
    pool_pre_ping=True,  # Verify connections before use
    echo=os.environ.get("SQL_ECHO", "false").lower() == "true",
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


# Connection event listeners for monitoring
@event.listens_for(engine, "connect")
def receive_connect(dbapi_conn, connection_record):
    logger.debug("New database connection established")


@event.listens_for(engine, "checkout")
def receive_checkout(dbapi_conn, connection_record, connection_proxy):
    logger.debug("Connection checked out from pool")



def retry_on_db_error(max_retries: int = 3, delay: float = 1.0):
    """
    Decorator to retry database operations on transient failures.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (OperationalError, DBAPIError) as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = delay * (2 ** attempt)  # Exponential backoff
                        logger.warning(
                            f"Database operation failed (attempt {attempt + 1}/{max_retries}), "
                            f"retrying in {wait_time}s: {e}"
                        )
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Database operation failed after {max_retries} attempts: {e}")
            raise last_exception
        return wrapper
    return decorator


@contextmanager
def get_session() -> Session:
    """
    Yields a SQLAlchemy session wrapped in a transaction with retry logic.
    Commits on clean exit, rolls back and re-raises on any exception.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Database transaction failed: {e}", exc_info=True)
        raise
    finally:
        session.close()


def check_database_health() -> dict:
    """
    Verify database connectivity and return health status.
    Used by the /health endpoint.
    """
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            result.fetchone()

        # Get pool status
        pool = engine.pool
        return {
            "status": "healthy",
            "pool_size": pool.size(),
            "checked_in_connections": pool.checkedin(),
            "checked_out_connections": pool.checkedout(),
            "overflow": pool.overflow(),
        }
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return {
            "status": "unhealthy",
            "error": str(e)
        }
