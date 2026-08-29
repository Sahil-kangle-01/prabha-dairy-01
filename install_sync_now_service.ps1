$ErrorActionPreference = 'Stop'
$target = Join-Path (Get-Location) 'sync_now_service.py'
@'
"""
Sync Now service for the Prabha Dairy Tally integration.

This version intentionally uses the project's PostgreSQL SessionLocal from
database.db. It does NOT create its own SQLAlchemy engine and does NOT have
an SQLite fallback.

Current stage:
- Discovers vouchers through Tally's Day Book export.
- Uses GUID + AlterID to decide new / changed / unchanged Sales vouchers.
- Fetches complete Sales voucher XML using the verified MasterID fetcher.
- Reuses the verified Sales inventory parser.
- Reuses the verified Sales accounting parser.
- Persists SalesVoucher, SalesInventory and AccountingEntry into PostgreSQL.

Usage:
    python sync_now_service.py 20260819 20260819
    python sync_now_service.py 20260819 20260819 --dry-run
    python sync_now_service.py 20260819 20260819 --write

The no-argument "Sync Now" date-window behavior will be wired after this
service is verified against the live database.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.db import SessionLocal
from database.models import AccountingEntry, SalesInventory, SalesVoucher

import daybook_discovery
import sales_accounting_extractor
import sales_batch_extractor


TALLY_URL = os.getenv("TALLY_URL", "http://localhost:9000")
TALLY_COMPANY = os.getenv(
    "TALLY_COMPANY",
    "SHRI JAIN BANDHU GRAMODYOG - (from 1-Apr-2026)",
)
TALLY_TIMEOUT = int(os.getenv("TALLY_TIMEOUT", "30"))
TALLY_RETRIES = int(os.getenv("TALLY_RETRIES", "3"))

SUPPORTED_VOUCHER_TYPE = "Sales"


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _decimal(value: Any) -> Decimal | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except Exception:
        return None


def _date_obj(value: Any) -> datetime | None:
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _voucher_ref(row: dict[str, Any]) -> sales_batch_extractor.VoucherRef:
    return sales_batch_extractor.VoucherRef(
        date=_clean(row.get("date")),
        voucher_number=_clean(row.get("voucher_number")),
        voucher_type=_clean(row.get("voucher_type")),
        party_ledger=_clean(row.get("party_ledger")),
        guid=_clean(row.get("guid")),
        master_id=_clean(row.get("master_id")),
        alter_id=_clean(row.get("alter_id")),
    )


def _accounting_rows(
    ref: sales_batch_extractor.VoucherRef,
    xml_text: str,
) -> list[dict[str, Any]]:
    """
    Reuse the verified accounting parser.

    The parser accepts a file path, so the live voucher XML is written to a
    temporary file and removed immediately after parsing.
    """
    temp_dir = Path("storage") / "sync_now_accounting"
    temp_dir.mkdir(parents=True, exist_ok=True)

    safe_master = ref.master_id or "unknown"
    temp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".xml",
            prefix=f"sales_{safe_master}_",
            dir=temp_dir,
            delete=False,
        ) as handle:
            handle.write(xml_text)
            temp_path = Path(handle.name)

        return sales_accounting_extractor.parse_file(temp_path)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass


def build_plan(
    session: Session,
    discovered: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    new: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []

    for row in discovered:
        voucher_type = _clean(row.get("voucher_type"))

        if voucher_type != SUPPORTED_VOUCHER_TYPE:
            unsupported.append(row)
            continue

        guid = _clean(row.get("guid"))
        if not guid:
            continue

        existing = session.scalar(
            select(SalesVoucher).where(SalesVoucher.guid == guid)
        )

        if existing is None:
            new.append(row)
        elif _clean(existing.alter_id) != _clean(row.get("alter_id")):
            changed.append(row)
        else:
            unchanged.append(row)

    return {
        "new": new,
        "changed": changed,
        "unchanged": unchanged,
        "unsupported": unsupported,
    }


def _persist_sales(
    session: Session,
    row: dict[str, Any],
    movements: list[Any],
    accounting_rows: list[dict[str, Any]],
) -> None:
    guid = _clean(row.get("guid"))

    voucher = session.scalar(
        select(SalesVoucher).where(SalesVoucher.guid == guid)
    )

    if voucher is None:
        voucher = SalesVoucher(guid=guid)
        session.add(voucher)
        session.flush()
    else:
        # Changed vouchers are replaced atomically.
        session.query(SalesInventory).filter(
            SalesInventory.sales_voucher_id == voucher.id
        ).delete(synchronize_session=False)

        session.query(AccountingEntry).filter(
            AccountingEntry.sales_voucher_id == voucher.id
        ).delete(synchronize_session=False)

    voucher.master_id = _clean(row.get("master_id"))
    voucher.alter_id = _clean(row.get("alter_id"))
    voucher.voucher_date = _date_obj(row.get("date"))
    voucher.voucher_number = _clean(row.get("voucher_number"))
    voucher.voucher_type = _clean(row.get("voucher_type"))
    voucher.party_ledger = _clean(row.get("party_ledger"))

    for movement in movements:
        data = vars(movement) if hasattr(movement, "__dict__") else dict(movement)

        session.add(
            SalesInventory(
                sales_voucher_id=voucher.id,
                stock_item=_clean(data.get("stock_item")),
                quantity=_decimal(data.get("quantity")),
                unit=_clean(data.get("unit")),
                billed_quantity=_clean(data.get("billed_quantity")),
                rate=_decimal(data.get("rate")),
                amount=_decimal(data.get("amount")),
                source_godown=_clean(data.get("source_godown")),
                destination_godown=_clean(data.get("destination_godown")),
                batch_name=_clean(data.get("batch_name")),
                is_deemed_positive=_clean(data.get("is_deemed_positive")),
                movement_type=_clean(data.get("movement_type")),
            )
        )

    for data in accounting_rows:
        session.add(
            AccountingEntry(
                sales_voucher_id=voucher.id,
                guid=_clean(data.get("guid")) or guid,
                master_id=_clean(data.get("master_id")) or _clean(row.get("master_id")),
                alter_id=_clean(data.get("alter_id")) or _clean(row.get("alter_id")),
                voucher_date=_date_obj(data.get("date")) or _date_obj(row.get("date")),
                voucher_number=_clean(data.get("voucher_number")) or _clean(row.get("voucher_number")),
                voucher_type=_clean(data.get("voucher_type")) or _clean(row.get("voucher_type")),
                party_ledger=_clean(data.get("party_ledger")) or _clean(row.get("party_ledger")),
                reference=_clean(data.get("reference")),
                is_invoice=_clean(data.get("is_invoice")),
                ledger_name=_clean(data.get("ledger_name")),
                amount=_decimal(data.get("amount")),
                is_deemed_positive=_clean(data.get("is_deemed_positive")),
                is_party_ledger=_clean(data.get("is_party_ledger")),
                ledger_from_item=_clean(data.get("ledger_from_item")),
                bill_reference=_clean(data.get("bill_reference")),
                bill_date=_clean(data.get("bill_date")),
                bill_type=_clean(data.get("bill_type")),
                cost_centre=_clean(data.get("cost_centre")),
            )
        )


def _extract_sales(
    row: dict[str, Any],
    from_date: str,
    to_date: str,
) -> tuple[list[Any], list[dict[str, Any]]]:
    ref = _voucher_ref(row)

    xml_text = sales_batch_extractor.fetch_voucher_xml(
        TALLY_URL,
        ref,
        from_date,
        to_date,
        TALLY_COMPANY,
        TALLY_TIMEOUT,
        TALLY_RETRIES,
    )

    movements = sales_batch_extractor.parse_movements_from_voucher_xml(
        xml_text,
        ref,
    )

    accounting = _accounting_rows(ref, xml_text)

    return movements, accounting


def sync_now(
    from_date: str,
    to_date: str,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    discovered = daybook_discovery.discover_daybook_vouchers(
        TALLY_URL,
        from_date,
        to_date,
        TALLY_COMPANY,
        timeout=TALLY_TIMEOUT,
    )

    with SessionLocal() as session:
        plan = build_plan(session, discovered)

        result: dict[str, Any] = {
            "from_date": from_date,
            "to_date": to_date,
            "discovered": len(discovered),
            "new": len(plan["new"]),
            "changed": len(plan["changed"]),
            "unchanged": len(plan["unchanged"]),
            "unsupported": len(plan["unsupported"]),
            "processed": 0,
            "failed": 0,
            "dry_run": dry_run,
            "failures": [],
        }

        if dry_run:
            return result

        work = plan["new"] + plan["changed"]

        for row in work:
            try:
                movements, accounting = _extract_sales(
                    row,
                    from_date,
                    to_date,
                )

                _persist_sales(
                    session,
                    row,
                    movements,
                    accounting,
                )

                session.commit()
                result["processed"] += 1

            except Exception as exc:
                session.rollback()
                result["failed"] += 1
                result["failures"].append(
                    {
                        "guid": _clean(row.get("guid")),
                        "master_id": _clean(row.get("master_id")),
                        "voucher_number": _clean(row.get("voucher_number")),
                        "error": str(exc),
                    }
                )

        return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the current Tally Sync Now Sales service."
    )

    parser.add_argument("from_date", help="Start date YYYYMMDD")
    parser.add_argument("to_date", help="End date YYYYMMDD")

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and classify vouchers without writing to PostgreSQL.",
    )

    parser.add_argument(
        "--write",
        action="store_true",
        help="Fetch and write new/changed Sales vouchers to PostgreSQL.",
    )

    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()

    if args.dry_run and args.write:
        raise SystemExit("Use either --dry-run or --write, not both.")

    # Safe default: dry run unless --write is explicitly supplied.
    dry_run = not args.write

    result = sync_now(
        args.from_date,
        args.to_date,
        dry_run=dry_run,
    )

    print()
    print("SYNC NOW")
    print("=" * 60)
    print(f"Date range       : {result['from_date']} -> {result['to_date']}")
    print(f"Discovered       : {result['discovered']}")
    print(f"New              : {result['new']}")
    print(f"Changed          : {result['changed']}")
    print(f"Unchanged        : {result['unchanged']}")
    print(f"Unsupported      : {result['unsupported']}")
    print(f"Processed        : {result['processed']}")
    print(f"Failed           : {result['failed']}")
    print(f"Mode             : {'DRY RUN' if result['dry_run'] else 'WRITE'}")

    if result["failures"]:
        print()
        print("FAILURES")
        for failure in result["failures"]:
            print(f"  {failure}")

    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

'@ | Set-Content -LiteralPath $target -Encoding UTF8
Write-Host "Replaced: $target"
Write-Host "Size:" (Get-Item $target).Length "bytes"
