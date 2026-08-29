"""
analytics_service.py

Client-facing analytics over Purchase Milk data. Covers:

  #1 Period-wise weighted average degree/FAT/SNF, recalculated per
     date range.
  #2 Milk-type breakdown (Cow/Buffalo/Mishr/Other) within any total
     or period.
  #3 Supplier/godown-wise analysis with averages.

WHY "WEIGHTED" MATTERS HERE:
A plain average of degree/FAT/SNF across vouchers is wrong the moment
voucher sizes differ -- a 2-litre delivery and a 200-litre delivery
would count equally. These are weighted by litres (sum(litres * value)
/ sum(litres)), which is the correct way to answer "what was our
overall FAT for this period", not "what was the average FAT per
voucher regardless of size".

Each of degree/fat/snf is weighted independently, filtered on its own
non-null rows -- a voucher missing `fat` still contributes to the
`degree` and `snf` weighted averages, it's just excluded from the
`fat` calculation specifically. This avoids one missing field zeroing
out otherwise-good data on the other two.

Reads only -- makes no writes, safe to run alongside a sync in
progress.

Usage:
    python analytics_service.py 20260401 20260819
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from database.db import SessionLocal
from database.models import PurchaseMilk

# Sentinel distinguishing "no filter requested" from an explicit
# filter value of None (which means "rows where this column IS
# NULL", not "don't filter this column at all"). Using plain None
# for both would silently skip filtering on real NULL groups -- see
# period_summary()'s docstring.
_UNSET = object()


def _parse_date(value: str):
    return datetime.strptime(value, "%Y%m%d").date()


def _weighted_fields(session: Session, base_query):
    """
    Shared weighted-average expression builder for degree/fat/snf,
    each independently null-safe, plus total litres/amount/count.
    Returns a single-row dict (or all-None/zero fields if no rows
    matched).
    """
    row = base_query.with_entities(
        func.count(PurchaseMilk.id).label("record_count"),
        func.coalesce(func.sum(PurchaseMilk.litres), 0).label("total_litres"),
        func.coalesce(func.sum(PurchaseMilk.actual_amount), 0).label("total_amount"),
        (
            func.sum(PurchaseMilk.litres * PurchaseMilk.degree)
            .filter(PurchaseMilk.degree.isnot(None), PurchaseMilk.litres.isnot(None))
            / func.nullif(
                func.sum(PurchaseMilk.litres)
                .filter(PurchaseMilk.degree.isnot(None), PurchaseMilk.litres.isnot(None)),
                0,
            )
        ).label("weighted_degree"),
        (
            func.sum(PurchaseMilk.litres * PurchaseMilk.fat)
            .filter(PurchaseMilk.fat.isnot(None), PurchaseMilk.litres.isnot(None))
            / func.nullif(
                func.sum(PurchaseMilk.litres)
                .filter(PurchaseMilk.fat.isnot(None), PurchaseMilk.litres.isnot(None)),
                0,
            )
        ).label("weighted_fat"),
        (
            func.sum(PurchaseMilk.litres * PurchaseMilk.snf)
            .filter(PurchaseMilk.snf.isnot(None), PurchaseMilk.litres.isnot(None))
            / func.nullif(
                func.sum(PurchaseMilk.litres)
                .filter(PurchaseMilk.snf.isnot(None), PurchaseMilk.litres.isnot(None)),
                0,
            )
        ).label("weighted_snf"),
    ).one()

    return {
        "record_count": row.record_count,
        "total_litres": float(row.total_litres or 0),
        "total_amount": float(row.total_amount or 0),
        "weighted_degree": float(row.weighted_degree) if row.weighted_degree is not None else None,
        "weighted_fat": float(row.weighted_fat) if row.weighted_fat is not None else None,
        "weighted_snf": float(row.weighted_snf) if row.weighted_snf is not None else None,
    }


def period_summary(
    session: Session,
    from_date,
    to_date,
    milk_type: str | None = _UNSET,
    godown: str | None = _UNSET,
) -> dict[str, Any]:
    """
    Requirement #1: overall weighted degree/FAT/SNF for a period.
    Optionally narrowed to one milk_type and/or one godown -- this is
    also the building block #3 (supplier/godown analysis) uses per
    godown.

    IMPORTANT: milk_type/godown default to the _UNSET sentinel, not
    None. Passing None explicitly means "filter for rows where this
    column IS NULL" (a real, distinct group in the data) -- omitting
    the argument entirely means "don't filter on this column at all".
    Conflating those two (by defaulting to None and checking
    `if milk_type:`) was a real bug: it silently skipped filtering
    whenever a caller passed an actual NULL group, making that group's
    "summary" equal to the whole unfiltered dataset instead of just
    its own (usually tiny) slice.
    """
    query = session.query(PurchaseMilk).filter(
        PurchaseMilk.date >= from_date,
        PurchaseMilk.date <= to_date,
    )
    if milk_type is not _UNSET:
        query = query.filter(
            PurchaseMilk.milk_type.is_(None) if milk_type is None
            else PurchaseMilk.milk_type == milk_type
        )
    if godown is not _UNSET:
        query = query.filter(
            PurchaseMilk.godown.is_(None) if godown is None
            else PurchaseMilk.godown == godown
        )

    result = _weighted_fields(session, query)
    result["from_date"] = str(from_date)
    result["to_date"] = str(to_date)
    result["milk_type"] = None if milk_type is _UNSET else milk_type
    result["godown"] = None if godown is _UNSET else godown
    return result


def milk_type_breakdown(session: Session, from_date, to_date) -> list[dict[str, Any]]:
    """Requirement #2: weighted stats broken out per milk_type."""
    types = [
        row[0] for row in
        session.query(PurchaseMilk.milk_type)
        .filter(PurchaseMilk.date >= from_date, PurchaseMilk.date <= to_date)
        .distinct()
        .all()
    ]

    # Pass milk_type=t explicitly for every t, including None -- this
    # correctly becomes an IS NULL filter (a real, usually small,
    # group) rather than silently matching everything. See
    # period_summary()'s docstring.
    return [
        period_summary(session, from_date, to_date, milk_type=t)
        for t in types
    ]


def godown_breakdown(
    session: Session,
    from_date,
    to_date,
    milk_type: str | None = None,
) -> list[dict[str, Any]]:
    """
    Requirement #3: weighted stats broken out per godown/location.
    `milk_type` here is a plain optional narrow-by-type filter (None
    = don't narrow) -- this is a different layer from the per-godown
    `g` value below, which comes from a distinct-values scan and can
    legitimately be None (a real NULL-godown group), so it's passed
    through explicitly to period_summary rather than relying on a
    default.
    """
    query = session.query(PurchaseMilk.godown).filter(
        PurchaseMilk.date >= from_date, PurchaseMilk.date <= to_date,
    )
    if milk_type is not None:
        query = query.filter(PurchaseMilk.milk_type == milk_type)
    godowns = [row[0] for row in query.distinct().all()]

    results = []
    for g in godowns:
        kwargs: dict[str, Any] = {"godown": g}
        if milk_type is not None:
            kwargs["milk_type"] = milk_type
        results.append(period_summary(session, from_date, to_date, **kwargs))
    return results


def supplier_breakdown(
    session: Session,
    from_date,
    to_date,
    top_n: int = 20,
) -> list[dict[str, Any]]:
    """
    Requirement #3 (supplier side): top suppliers by litres in the
    period, with their own weighted degree/FAT/SNF.
    """
    parties = (
        session.query(PurchaseMilk.party_ledger)
        .filter(PurchaseMilk.date >= from_date, PurchaseMilk.date <= to_date)
        .group_by(PurchaseMilk.party_ledger)
        .order_by(func.sum(PurchaseMilk.litres).desc())
        .limit(top_n)
        .all()
    )

    results = []
    for (party,) in parties:
        query = session.query(PurchaseMilk).filter(
            PurchaseMilk.date >= from_date,
            PurchaseMilk.date <= to_date,
            PurchaseMilk.party_ledger == party,
        )
        row = _weighted_fields(session, query)
        row["party_ledger"] = party
        results.append(row)
    return results


def _print_row(label: str, row: dict[str, Any]) -> None:
    degree = f"{row['weighted_degree']:.2f}" if row["weighted_degree"] is not None else "n/a"
    fat = f"{row['weighted_fat']:.2f}" if row["weighted_fat"] is not None else "n/a"
    snf = f"{row['weighted_snf']:.2f}" if row["weighted_snf"] is not None else "n/a"
    print(
        f"  {label:<35} litres={row['total_litres']:>10.2f}  "
        f"degree={degree:>6}  fat={fat:>6}  snf={snf:>6}  "
        f"records={row['record_count']}"
    )


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python analytics_service.py YYYYMMDD YYYYMMDD")
        return 2

    from_date = _parse_date(sys.argv[1])
    to_date = _parse_date(sys.argv[2])

    with SessionLocal() as session:
        print("PRABHA DAIRY - PURCHASE MILK ANALYTICS")
        print("=" * 70)
        print(f"Period: {from_date} -> {to_date}")
        print()

        overall = period_summary(session, from_date, to_date)
        print("Overall:")
        _print_row("ALL", overall)

        print()
        print("By milk type (#2):")
        for row in sorted(
            milk_type_breakdown(session, from_date, to_date),
            key=lambda r: r["total_litres"], reverse=True,
        ):
            _print_row(row["milk_type"] or "(unspecified)", row)

        print()
        print("By godown (#3, top by litres):")
        for row in sorted(
            godown_breakdown(session, from_date, to_date),
            key=lambda r: r["total_litres"], reverse=True,
        )[:15]:
            _print_row(row["godown"] or "(unspecified)", row)

        print()
        print("Top suppliers by litres (#3):")
        for row in supplier_breakdown(session, from_date, to_date, top_n=15):
            _print_row(row["party_ledger"] or "(unspecified)", row)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
