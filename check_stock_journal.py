"""
check_stock_journal.py

Quick correctness check for the Stock Journal write path.
Run this after the isolated --types "Stock Journal" --write test.

Usage:
    python check_stock_journal.py
"""

from database.db import SessionLocal
from database.models import StockMovement
from sqlalchemy import func


def main() -> None:
    with SessionLocal() as session:
        print("Movement type breakdown (stock_movements):")
        rows = (
            session.query(StockMovement.movement_type, func.count())
            .group_by(StockMovement.movement_type)
            .all()
        )
        if not rows:
            print("  (no rows found in stock_movements)")
        for movement_type, count in rows:
            print(f"  {movement_type or '(none)':<20} {count}")

        distinct_guids = (
            session.query(func.count(func.distinct(StockMovement.guid)))
            .scalar()
        )
        print()
        print(f"Distinct voucher GUIDs in stock_movements: {distinct_guids}")
        print("(expect 161, or slightly under if some vouchers legitimately "
              "have zero stock-item lines)")

        by_voucher_type = (
            session.query(StockMovement.voucher_type, func.count())
            .group_by(StockMovement.voucher_type)
            .all()
        )
        print()
        print("voucher_type breakdown on those rows (sanity check -- "
              "should all say 'Stock Journal'):")
        for vtype, count in by_voucher_type:
            print(f"  {vtype or '(none)':<20} {count}")


if __name__ == "__main__":
    main()
