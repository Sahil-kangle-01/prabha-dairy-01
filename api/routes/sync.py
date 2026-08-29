"""
api/routes/sync.py

HTTP endpoints for triggering Tally sync and checking sync status.
"""

from __future__ import annotations

import logging
import socket
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from config import settings
from database.db import SessionLocal
from sync_now_service import (
    CHECKPOINT_SYNC_TYPE,
    get_checkpoint_date,
    sync_now,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sync", tags=["sync"])


# ── Request / Response models ────────────────────────────────────

class SyncTriggerRequest(BaseModel):
    from_date: str | None = Field(
        None,
        description="Start date YYYYMMDD. Ignored when since_last=true.",
    )
    to_date: str | None = Field(
        None,
        description="End date YYYYMMDD. Defaults to today.",
    )
    write: bool = Field(
        True,
        description="False = dry-run (discover only, no DB writes).",
    )
    since_last: bool = Field(
        True,
        description="Resume from the last checkpoint (overrides from_date).",
    )


# ── Helpers ──────────────────────────────────────────────────────

def _check_tally_reachable() -> bool:
    """Quick TCP connect to Tally's HTTP port."""
    try:
        sock = socket.create_connection(
            (settings.tally_host, settings.tally_port),
            timeout=2,
        )
        sock.close()
        return True
    except (OSError, socket.timeout):
        return False


# ── Endpoints ────────────────────────────────────────────────────

@router.get("/status")
def sync_status(request: Request):
    """
    Return the last sync checkpoint date and Tally connectivity.
    """
    with SessionLocal() as session:
        checkpoint = get_checkpoint_date(session)

    return {
        "last_checkpoint": checkpoint.isoformat() if checkpoint else None,
        "tally_reachable": _check_tally_reachable(),
        "tally_host": settings.tally_host,
        "tally_port": settings.tally_port,
    }


@router.post("/trigger")
def sync_trigger(body: SyncTriggerRequest, request: Request):
    """
    Trigger a Tally → DB sync.  Returns the result summary.

    Default behaviour (since_last=true, write=true) resumes from the
    last checkpoint and persists everything — this is what the "Sync Now"
    button in the UI should call.
    """
    to_date = body.to_date or datetime.now().strftime("%Y%m%d")

    # Resolve from_date
    if body.since_last:
        with SessionLocal() as session:
            checkpoint = get_checkpoint_date(session)
        if checkpoint is None:
            # First-ever sync: default to financial year start
            from_date = "20260401"
        else:
            from_date = (checkpoint + timedelta(days=1)).strftime("%Y%m%d")
    elif body.from_date:
        from_date = body.from_date
    else:
        return JSONResponse(
            status_code=400,
            content={
                "detail": "Provide from_date, or set since_last=true to "
                          "resume from the last checkpoint.",
            },
        )

    logger.info(
        "Sync triggered via API: %s → %s, write=%s, since_last=%s",
        from_date, to_date, body.write, body.since_last,
    )

    try:
        result = sync_now(
            from_date,
            to_date,
            dry_run=not body.write,
            voucher_types=None,
        )
    except Exception as exc:
        logger.error("Sync failed: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Sync failed: {exc}"},
        )

    return {
        "from_date": from_date,
        "to_date": to_date,
        "write": body.write,
        **result,
    }
