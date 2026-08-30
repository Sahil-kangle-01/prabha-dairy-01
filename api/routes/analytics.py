"""
api/routes/analytics.py

Thin HTTP wrapper around analytics_service.py -- requirements #1, #2, #3.
No query logic lives here; it all stays in analytics_service.py so the
CLI (`python analytics_service.py ...`) and the API never drift apart.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

import analytics_service as svc
from api.deps import get_db

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _parse_date(value: str, field_name: str) -> date:
    try:
        return svc._parse_date(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"'{field_name}' must be YYYYMMDD, got {value!r}",
        )


@router.get("/period-summary")
def period_summary(
    from_date: str = Query(..., description="YYYYMMDD"),
    to_date: str = Query(..., description="YYYYMMDD"),
    milk_type: str | None = None,
    godown: str | None = None,
    db: Session = Depends(get_db),
):
    """Requirement #1: overall weighted degree/FAT/SNF for a period,
    optionally narrowed to one milk_type and/or one godown.

    IMPORTANT: only pass milk_type/godown through when the query param
    was actually supplied. svc.period_summary() distinguishes "argument
    omitted" (don't filter) from "argument explicitly None" (filter for
    IS NULL) via its _UNSET sentinel -- forwarding this route's default
    of None unconditionally would silently narrow every unfiltered
    request down to just the tiny "(Unspecified)" group instead of the
    full period. See the _UNSET docstring in analytics_service.py.
    """
    fd = _parse_date(from_date, "from_date")
    td = _parse_date(to_date, "to_date")
    kwargs = {}
    if milk_type is not None:
        kwargs["milk_type"] = milk_type
    if godown is not None:
        kwargs["godown"] = godown
    return svc.period_summary(db, fd, td, **kwargs)


@router.get("/milk-type-breakdown")
def milk_type_breakdown(
    from_date: str = Query(..., description="YYYYMMDD"),
    to_date: str = Query(..., description="YYYYMMDD"),
    db: Session = Depends(get_db),
):
    """Requirement #2: weighted stats per milk_type (Cow/Buffalo/Mishr/Other)."""
    fd = _parse_date(from_date, "from_date")
    td = _parse_date(to_date, "to_date")
    return svc.milk_type_breakdown(db, fd, td)


@router.get("/godown-breakdown")
def godown_breakdown(
    from_date: str = Query(..., description="YYYYMMDD"),
    to_date: str = Query(..., description="YYYYMMDD"),
    milk_type: str | None = None,
    db: Session = Depends(get_db),
):
    """Requirement #3 (location side): weighted stats per godown."""
    fd = _parse_date(from_date, "from_date")
    td = _parse_date(to_date, "to_date")
    return svc.godown_breakdown(db, fd, td, milk_type=milk_type)


@router.get("/daily-breakdown")
def daily_breakdown(
    from_date: str = Query(..., description="YYYYMMDD"),
    to_date: str = Query(..., description="YYYYMMDD"),
    milk_type: str | None = None,
    godown: str | None = None,
    db: Session = Depends(get_db),
):
    """One row per calendar day in the period, same litres-weighted
    degree/FAT/SNF as period-summary, for the new Daily Breakdown table."""
    fd = _parse_date(from_date, "from_date")
    td = _parse_date(to_date, "to_date")
    return svc.daily_breakdown(db, fd, td, milk_type=milk_type, godown=godown)


@router.get("/supplier-breakdown")
def supplier_breakdown(
    from_date: str = Query(..., description="YYYYMMDD"),
    to_date: str = Query(..., description="YYYYMMDD"),
    top_n: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Requirement #3 (supplier side): top suppliers by litres, with
    their own weighted degree/FAT/SNF."""
    fd = _parse_date(from_date, "from_date")
    td = _parse_date(to_date, "to_date")
    return svc.supplier_breakdown(db, fd, td, top_n=top_n)
