"""
logging_config.py

Production-grade structured logging with file rotation, error tracking,
and audit trails.
"""

import logging
import logging.handlers
import sys
from pathlib import Path
from datetime import datetime

# Create logs directory
LOGS_DIR = Path(__file__).parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)


def setup_logging(service_name: str = "prabha_dairy"):
    """
    Configure structured logging with rotation for production.

    Logs to:
      - Console (INFO level, structured JSON-like format)
      - File (DEBUG level, rotated daily, 30 days retention)
      - Error file (ERROR level only, separate for monitoring)
    """

    # Create logger
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    # Remove any existing handlers
    logger.handlers.clear()

    # Console handler (INFO level)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File handler (DEBUG level, rotated daily, keep 30 days)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=LOGS_DIR / f"{service_name}.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Error-only handler (ERROR level, rotated weekly, keep 12 weeks)
    error_handler = logging.handlers.TimedRotatingFileHandler(
        filename=LOGS_DIR / f"{service_name}_errors.log",
        when="W0",  # Monday
        interval=1,
        backupCount=12,
        encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s\n%(exc_info)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    error_handler.setFormatter(error_formatter)
    logger.addHandler(error_handler)

    # Suppress noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    logger.info(f"Logging configured for {service_name}")
    logger.info(f"Log directory: {LOGS_DIR.absolute()}")

    return logger


def log_sync_event(
    sync_type: str,
    status: str,
    records_inserted: int = 0,
    records_updated: int = 0,
    records_failed: int = 0,
    duration_seconds: float = 0,
    error_message: str = None
):
    """
    Structured audit log for sync events.
    """
    logger = logging.getLogger("sync_audit")

    event = {
        "timestamp": datetime.utcnow().isoformat(),
        "sync_type": sync_type,
        "status": status,
        "records_inserted": records_inserted,
        "records_updated": records_updated,
        "records_failed": records_failed,
        "duration_seconds": round(duration_seconds, 2),
    }

    if error_message:
        event["error"] = error_message

    # Log to audit file
    audit_file = LOGS_DIR / "sync_audit.log"
    with open(audit_file, "a", encoding="utf-8") as f:
        import json
        f.write(json.dumps(event) + "\n")

    if status == "success":
        logger.info(f"Sync completed: {sync_type} | +{records_inserted} inserted, ~{records_updated} updated")
    else:
        logger.error(f"Sync failed: {sync_type} | {error_message}")


def log_api_request(
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    user_agent: str = None,
    error: str = None
):
    """
    Structured API access log for monitoring and debugging.
    """
    logger = logging.getLogger("api_access")

    log_entry = f"{method} {path} | {status_code} | {duration_ms:.0f}ms"

    if error:
        logger.warning(f"{log_entry} | ERROR: {error}")
    elif status_code >= 500:
        logger.error(log_entry)
    elif status_code >= 400:
        logger.warning(log_entry)
    else:
        logger.info(log_entry)
