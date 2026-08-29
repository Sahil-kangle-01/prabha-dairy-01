"""
master_sync_service.py

Syncs Tally master data (Stock Items, Ledgers, Godowns, Units) into
PostgreSQL. Built directly on the already-proven extraction functions
in master_data_extractor.py -- this file adds a persistence layer on
top, it does not re-implement Tally requests.

WHY THIS IS A SIMPLE UPSERT, NOT GUID/AlterID CLASSIFICATION LIKE
sync_now_service.py:
Vouchers are immutable historical facts (a Sale on a given date either
happened or didn't) tracked via GUID/AlterID insert-vs-update
classification. Masters are different: a Stock Item or Ledger is a
current-state record with no independent "did this change" signal
-- it's simplest and correct to always upsert the current name/parent
against Tally's current export, overwriting on every run. Full-refresh
semantics are appropriate here because Tally always returns the
complete masters list.

WHY THIS DOES NOT STORE STOCK BALANCES:
Requirement is that live stock quantity/rate is ALWAYS pulled fresh
from Tally, never cached (see live_stock_lookup.py). Storing
opening/closing balances here would create a second, potentially
stale source of truth for exactly the numbers that must never be
stale. So this table is identity/reference data only (for
autocomplete, dropdowns, joins) -- not a stock cache.

Deletion detection is intentionally NOT implemented, matching the
same policy already established for Purchase Milk (see README.md) --
a master renamed or removed in Tally will leave its old DB row in
place rather than being silently deleted.

Usage:
    python master_sync_service.py
"""

from __future__ import annotations

import re
from typing import Any, Callable

from sqlalchemy.orm import Session

from database.db import SessionLocal
from database.models import Godown, Ledger, StockItem, Unit

from master_data_extractor import (
    build_units,
    extract_godowns,
    extract_ledgers,
    extract_stock_items,
)


def _to_number(value: Any) -> float | None:
    """
    Coerce a raw Tally balance string to a float, or None if blank/
    unparseable. Tally balance strings can carry trailing unit/suffix
    text (e.g. "1234.56 Cr", "0.000 KG") and use a bare leading "-"
    for negative amounts -- this pulls out the leading numeric token
    and ignores everything after it. Never raises; an unparseable
    value becomes None rather than corrupting the sync.
    """
    if value in (None, ""):
        return None
    match = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(value))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _upsert_by_name(
    session: Session,
    model: type,
    rows: list[dict[str, Any]],
    field_map: dict[str, str],
    coerce: dict[str, Callable[[Any], Any]] | None = None,
) -> dict[str, int]:
    """
    Upsert `rows` into `model`, keyed on the `name` column.
    `field_map` maps {model_column: source_dict_key} for every column
    besides `name` that should be written. `coerce`, if given, maps
    {model_column: function} to transform the raw source value (e.g.
    string -> float) before comparing/storing it.

    Returns counts: {"inserted": n, "updated": n, "unchanged": n, "skipped": n}.
    """
    coerce = coerce or {}
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}

    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            counts["skipped"] += 1
            continue

        existing = session.query(model).filter(model.name == name).one_or_none()

        if existing is None:
            kwargs = {"name": name}
            for column, source_key in field_map.items():
                value = row.get(source_key)
                value = value if value not in (None, "") else None
                if value is not None and column in coerce:
                    value = coerce[column](value)
                kwargs[column] = value
            session.add(model(**kwargs))
            counts["inserted"] += 1
            continue

        changed = False
        for column, source_key in field_map.items():
            new_value = row.get(source_key)
            new_value = new_value if new_value not in (None, "") else None
            if new_value is not None and column in coerce:
                new_value = coerce[column](new_value)
            if getattr(existing, column) != new_value:
                setattr(existing, column, new_value)
                changed = True

        if changed:
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1

    return counts


def sync_masters() -> dict[str, Any]:
    """
    Pulls the current Stock Item, Ledger, and Godown masters from Tally
    (via the already-proven extractor functions) and upserts them into
    Postgres. Units are derived from the stock items, same as the
    extractor already does.
    """
    stock_items = extract_stock_items()
    ledgers = extract_ledgers()
    godowns = extract_godowns()
    units = build_units(stock_items)

    summary: dict[str, Any] = {}

    with SessionLocal() as session:
        # StockItem table currently only has `name` -- see module
        # docstring for why parent/balances aren't persisted here.
        summary["stock_items"] = _upsert_by_name(
            session, StockItem, stock_items, field_map={},
        )

        summary["ledgers"] = _upsert_by_name(
            session, Ledger, ledgers, field_map={
                "parent": "parent",
                "guid": "guid",
                "opening_balance": "opening_balance",
                "closing_balance": "closing_balance",
            },
            coerce={
                "opening_balance": _to_number,
                "closing_balance": _to_number,
            },
        )

        # Godown table currently only has `name` -- see module
        # docstring; `parent` is fetched but not yet a column.
        summary["godowns"] = _upsert_by_name(
            session, Godown, godowns, field_map={},
        )

        summary["units"] = _upsert_by_name(
            session, Unit, units, field_map={},
        )

        session.commit()

    return summary


def main() -> int:
    print("PRABHA DAIRY - MASTER SYNC")
    print("=" * 60)

    try:
        summary = sync_masters()
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    print()
    print("Results:")
    for table, counts in summary.items():
        print(
            f"  {table:<12} "
            f"inserted={counts['inserted']:<4} "
            f"updated={counts['updated']:<4} "
            f"unchanged={counts['unchanged']:<4} "
            f"skipped={counts['skipped']}"
        )

    print()
    print("MASTER SYNC COMPLETE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
