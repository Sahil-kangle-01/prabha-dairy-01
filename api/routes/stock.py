"""
api/routes/stock.py

Requirement #4: live godown stock lookup, search-as-you-type.

Two-step design:
  - /stock/search  -- autocomplete against our already-synced StockItem
    master table (fast, no Tally round-trip per keystroke).
  - /stock/live     -- once the user picks an exact item name, THIS is
    the only call that hits Tally, for the current godown-wise balance
    and rate. Never cached, never backed by the DB -- see api/live_stock.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_db
from api.live_stock import get_live_godown_stock
from database.models import Godown, StockItem

router = APIRouter(prefix="/stock", tags=["stock"])


@router.get("/search")
def search_stock_items(
    q: str = Query(..., min_length=1, description="Partial stock item name"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Autocomplete against the synced StockItem master -- no Tally call."""
    rows = db.scalars(
        select(StockItem.name)
        .where(StockItem.name.ilike(f"%{q}%"))
        .order_by(StockItem.name)
        .limit(limit)
    ).all()
    return {"query": q, "results": rows}


@router.get("/godowns")
def list_godowns(db: Session = Depends(get_db)):
    """The synced Godown master -- for a location filter dropdown."""
    rows = db.scalars(select(Godown.name).order_by(Godown.name)).all()
    return {"results": rows}


@router.get("/live")
def live_stock(
    item: str = Query(..., min_length=1, description="Exact stock item name"),
):
    """
    Requirement #4/#6: current godown-wise balance and rate for one item,
    queried directly from Tally on every call -- never cached, never read
    from Postgres. See api/live_stock.py's module docstring for the
    verification caveat on this specific query.
    """
    try:
        rows = get_live_godown_stock(item)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No live stock data returned for item {item!r} "
                   "(check the exact name via /stock/search first)",
        )

    return {
        "item": item,
        "godowns": [
            {"godown": r.godown, "closing_balance": r.closing_balance, "closing_rate": r.closing_rate}
            for r in rows
        ],
    }
