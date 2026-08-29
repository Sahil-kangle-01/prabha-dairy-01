"""
check_sales_milks.py

Correctness check for the SALES MILKS write path, same idea as
check_stock_journal.py. Run this once the isolated
--types "SALES MILKS" --write test finishes.

Usage:
    python check_sales_milks.py
"""

from database.db import SessionLocal
from database.models import AccountingEntry, SalesInventory, SalesVoucher
from sqlalchemy import func


def main() -> None:
    with SessionLocal() as session:
        voucher_count = (
            session.query(func.count(SalesVoucher.id))
            .filter(SalesVoucher.voucher_type == "SALES MILKS")
            .scalar()
        )
        print(f"SalesVoucher rows with voucher_type = 'SALES MILKS': {voucher_count}")
        print("(expect 2265, or slightly under if some vouchers "
              "legitimately failed/were skipped -- check for an "
              "Errors: section in the --write output if so)")

        milks_voucher_ids = [
            row[0] for row in
            session.query(SalesVoucher.id)
            .filter(SalesVoucher.voucher_type == "SALES MILKS")
            .all()
        ]

        if not milks_voucher_ids:
            print("\nNo SALES MILKS vouchers found -- nothing further to check.")
            return

        print()
        print("SalesInventory movement_type breakdown (SALES MILKS only):")
        inv_rows = (
            session.query(SalesInventory.movement_type, func.count())
            .filter(SalesInventory.sales_voucher_id.in_(milks_voucher_ids))
            .group_by(SalesInventory.movement_type)
            .all()
        )
        if not inv_rows:
            print("  (no inventory rows found -- check if SALES MILKS "
                  "vouchers are expected to carry inventory lines at all)")
        for movement_type, count in inv_rows:
            print(f"  {movement_type or '(none)':<20} {count}")

        distinct_inv_vouchers = (
            session.query(func.count(func.distinct(SalesInventory.sales_voucher_id)))
            .filter(SalesInventory.sales_voucher_id.in_(milks_voucher_ids))
            .scalar()
        )
        print(f"Distinct SALES MILKS vouchers with inventory rows: {distinct_inv_vouchers}")

        print()
        acc_count = (
            session.query(func.count(AccountingEntry.id))
            .filter(AccountingEntry.sales_voucher_id.in_(milks_voucher_ids))
            .scalar()
        )
        distinct_acc_vouchers = (
            session.query(func.count(func.distinct(AccountingEntry.sales_voucher_id)))
            .filter(AccountingEntry.sales_voucher_id.in_(milks_voucher_ids))
            .scalar()
        )
        print(f"AccountingEntry rows for SALES MILKS vouchers: {acc_count}")
        print(f"Distinct SALES MILKS vouchers with accounting rows: {distinct_acc_vouchers}")
        print("(a healthy voucher should have at least one accounting row -- "
              "the party ledger side at minimum; a voucher with inventory "
              "but zero accounting rows would be worth investigating)")

        print()
        print("Party ledger breakdown (top 10, SALES MILKS):")
        party_rows = (
            session.query(SalesVoucher.party_ledger, func.count())
            .filter(SalesVoucher.voucher_type == "SALES MILKS")
            .group_by(SalesVoucher.party_ledger)
            .order_by(func.count().desc())
            .limit(10)
            .all()
        )
        for party, count in party_rows:
            print(f"  {party or '(none)':<40} {count}")


if __name__ == "__main__":
    main()
