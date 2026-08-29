"""
DB-backed Sync Now service.

This is the production orchestration layer.

Flow:
    Day Book discovery
        -> classify each voucher NEW / CHANGED / UNCHANGED per domain
           (Sales: sales_vouchers.guid+alter_id,
            Purchase Milk: purchase_milk.guid+alter_id,
            Stock Journal: stock_movements.guid+alter_id)
        -> fetch full voucher XML for NEW/CHANGED (by MasterID)
        -> persist voucher (+ children) atomically, one voucher per commit
        -> on a clean, error-free WRITE run, advance the sync checkpoint

Important:
- Existing extractor/parser modules remain the source of normalization
  logic and are NOT modified: sales_batch_extractor.py,
  sales_accounting_extractor.py, purchase_milk_tally_parser.py,
  stock_journal_parser_final.py.
- Changed vouchers are replaced, not appended, so stale child rows cannot
  remain for Sales or Stock Journal (Purchase Milk is a single-row
  upsert, so replace == update-in-place).
- Unchanged vouchers are skipped without touching the DB.
- No temporary JSON state is used as the source of truth -- sync_runs and
  sync_checkpoints are the durable record of what happened.

Dry-run is the default. Pass --write to actually persist.
"""

from __future__ import annotations

import argparse
import tempfile
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.db import SessionLocal

from database.models import (
    AccountingEntry,
    PurchaseMilk,
    SalesInventory,
    SalesVoucher,
    StockMovement,
    SyncCheckpoint,
)

from daybook_discovery import discover_daybook_vouchers

# Existing, already-validated project modules. Imports are kept here so
# the service can dispatch to them without duplicating their parsing
# logic. None of these files are modified by this service.
import sales_accounting_extractor
import sales_batch_extractor
import purchase_milk_tally_parser
import stock_journal_parser as stock_journal_parser_final

CHECKPOINT_SYNC_TYPE = "daybook_sync"
TALLY_URL = "http://localhost:9000"
TALLY_COMPANY = "SHRI JAIN BANDHU GRAMODYOG - (from 1-Apr-2026)"


@dataclass
class SyncPlan:
    new: list[dict[str, Any]] = field(default_factory=list)
    changed: list[dict[str, Any]] = field(default_factory=list)
    unchanged: list[dict[str, Any]] = field(default_factory=list)
    unsupported: list[dict[str, Any]] = field(default_factory=list)


def _normalize(value: Any) -> str:
    return str(value or "").strip()


def _parse_tally_date(value: str) -> date_cls | None:
    value = _normalize(value)
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def _existing_sales_alter_id(session: Session, guid: str) -> str | None:
    row = session.scalar(select(SalesVoucher.alter_id).where(SalesVoucher.guid == guid))
    return _normalize(row) if row is not None else None


def _existing_purchase_milk_alter_id(session: Session, guid: str) -> str | None:
    row = session.scalar(select(PurchaseMilk.alter_id).where(PurchaseMilk.guid == guid))
    return _normalize(row) if row is not None else None


def _existing_stock_journal_alter_id(session: Session, guid: str) -> str | None:
    row = session.scalar(
        select(StockMovement.alter_id).where(StockMovement.guid == guid).limit(1)
    )
    return _normalize(row) if row is not None else None


_ALTER_ID_LOOKUP = {
    "Sales": _existing_sales_alter_id,
    "SALES MILKS": _existing_sales_alter_id,
    "Purchase Milk": _existing_purchase_milk_alter_id,
    "Stock Journal": _existing_stock_journal_alter_id,
}


def build_plan(session: Session, discovered: list[dict[str, Any]]) -> SyncPlan:
    plan = SyncPlan()
    supported = set(_ALTER_ID_LOOKUP)

    for row in discovered:
        voucher_type = _normalize(row.get("voucher_type"))

        if voucher_type not in supported:
            plan.unsupported.append(row)
            continue

        guid = _normalize(row.get("guid"))
        if not guid:
            continue

        existing_alter_id = _ALTER_ID_LOOKUP[voucher_type](session, guid)
        new_alter_id = _normalize(row.get("alter_id"))

        if existing_alter_id is None:
            plan.new.append(row)
        elif existing_alter_id != new_alter_id:
            plan.changed.append(row)
        else:
            plan.unchanged.append(row)

    return plan


# --------------------------------------------------------------------------
# Fetch (by MasterID -- generic across voucher types, verified against the
# client's Tally instance for Sales; the request only depends on MasterID
# and company, so it is reused as-is for Purchase Milk and Stock Journal).
# --------------------------------------------------------------------------

def _fetch_voucher_xml(ref: dict[str, Any]) -> str:
    voucher_ref = sales_batch_extractor.VoucherRef(
        date=_normalize(ref.get("date")),
        voucher_number=_normalize(ref.get("voucher_number")),
        voucher_type=_normalize(ref.get("voucher_type")),
        party_ledger=_normalize(ref.get("party_ledger")),
        guid=_normalize(ref.get("guid")),
        master_id=_normalize(ref.get("master_id")),
        alter_id=_normalize(ref.get("alter_id")),
    )
    return sales_batch_extractor.fetch_voucher_xml(
        TALLY_URL,
        voucher_ref,
        _normalize(ref.get("date")),
        _normalize(ref.get("date")),
        TALLY_COMPANY,
        30,
        3,
    )


# --------------------------------------------------------------------------
# Sales
# --------------------------------------------------------------------------

def _extract_sales_voucher(ref: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch and parse one complete Sales voucher using the already-verified
    MasterID inventory extractor and accounting parser."""
    xml_text = _fetch_voucher_xml(ref)

    voucher_ref = sales_batch_extractor.VoucherRef(
        date=_normalize(ref.get("date")),
        voucher_number=_normalize(ref.get("voucher_number")),
        voucher_type=_normalize(ref.get("voucher_type")),
        party_ledger=_normalize(ref.get("party_ledger")),
        guid=_normalize(ref.get("guid")),
        master_id=_normalize(ref.get("master_id")),
        alter_id=_normalize(ref.get("alter_id")),
    )

    movements = sales_batch_extractor.parse_movements_from_voucher_xml(xml_text, voucher_ref)

    inventory_rows = []
    for movement in movements:
        data = vars(movement) if hasattr(movement, "__dict__") else dict(movement)
        inventory_rows.append(data)

    temp_dir = Path("storage") / "sync_now_accounting"
    temp_dir.mkdir(parents=True, exist_ok=True)

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".xml",
            prefix=f"sales_{voucher_ref.master_id or 'unknown'}_",
            dir=temp_dir,
            delete=False,
        ) as handle:
            handle.write(xml_text)
            temp_path = Path(handle.name)

        accounting_rows = sales_accounting_extractor.parse_file(temp_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    voucher = {
        "date": voucher_ref.date,
        "voucher_number": voucher_ref.voucher_number,
        "voucher_type": voucher_ref.voucher_type,
        "party_ledger": voucher_ref.party_ledger,
        "guid": voucher_ref.guid,
        "master_id": voucher_ref.master_id,
        "alter_id": voucher_ref.alter_id,
    }

    return voucher, inventory_rows, accounting_rows


def _persist_sales(
    session: Session,
    voucher: dict[str, Any],
    inventory_rows: list[dict[str, Any]],
    accounting_rows: list[dict[str, Any]],
) -> SalesVoucher:
    guid = _normalize(voucher.get("guid"))

    existing = session.scalar(select(SalesVoucher).where(SalesVoucher.guid == guid))

    if existing is not None:
        session.query(SalesInventory).filter(
            SalesInventory.sales_voucher_id == existing.id
        ).delete(synchronize_session=False)
        session.query(AccountingEntry).filter(
            AccountingEntry.sales_voucher_id == existing.id
        ).delete(synchronize_session=False)
        sales_voucher = existing
    else:
        sales_voucher = SalesVoucher(guid=guid)
        session.add(sales_voucher)

    sales_voucher.master_id = _normalize(voucher.get("master_id"))
    sales_voucher.alter_id = _normalize(voucher.get("alter_id"))
    sales_voucher.voucher_date = _parse_tally_date(voucher.get("date"))
    sales_voucher.voucher_number = _normalize(voucher.get("voucher_number"))
    sales_voucher.voucher_type = _normalize(voucher.get("voucher_type"))
    sales_voucher.party_ledger = _normalize(voucher.get("party_ledger"))

    session.flush()

    def _numeric(value):
        """Convert a raw parser value to float or None.
        Handles None, empty strings, and strings with unit suffixes
        like '74.00/ltr'.  PostgreSQL rejects '' for Numeric columns,
        so every numeric field must go through this."""
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        # Strip unit suffixes like "/ltr" that Tally rate fields can carry
        text = text.split("/", 1)[0].strip()
        try:
            return float(text)
        except (ValueError, TypeError):
            return None

    for row in inventory_rows:
        session.add(
            SalesInventory(
                sales_voucher_id=sales_voucher.id,
                stock_item=_normalize(row.get("stock_item")),
                quantity=_numeric(row.get("quantity")),
                unit=_normalize(row.get("unit")),
                billed_quantity=_normalize(row.get("billed_quantity")),
                rate=_numeric(row.get("rate")),
                amount=_numeric(row.get("amount")),
                source_godown=_normalize(row.get("source_godown")),
                destination_godown=_normalize(row.get("destination_godown")),
                batch_name=_normalize(row.get("batch_name")),
                is_deemed_positive=_normalize(row.get("is_deemed_positive")),
                movement_type=_normalize(row.get("movement_type")),
            )
        )

    for row in accounting_rows:
        session.add(
            AccountingEntry(
                sales_voucher_id=sales_voucher.id,
                guid=_normalize(row.get("guid")),
                master_id=_normalize(row.get("master_id")),
                alter_id=_normalize(row.get("alter_id")),
                voucher_date=_parse_tally_date(row.get("date")),
                voucher_number=_normalize(row.get("voucher_number")),
                voucher_type=_normalize(row.get("voucher_type")),
                party_ledger=_normalize(row.get("party_ledger")),
                reference=_normalize(row.get("reference")),
                is_invoice=_normalize(row.get("is_invoice")),
                ledger_name=_normalize(row.get("ledger_name")),
                amount=_numeric(row.get("amount")),
                is_deemed_positive=_normalize(row.get("is_deemed_positive")),
                is_party_ledger=_normalize(row.get("is_party_ledger")),
                ledger_from_item=_normalize(row.get("ledger_from_item")),
                bill_reference=_normalize(row.get("bill_reference")),
                bill_date=_normalize(row.get("bill_date")),
                bill_type=_normalize(row.get("bill_type")),
                cost_centre=_normalize(row.get("cost_centre")),
            )
        )

    return sales_voucher


# --------------------------------------------------------------------------
# Purchase Milk
# --------------------------------------------------------------------------

_PM_NUMERIC_FIELDS = (
    "litres", "degree", "fat", "snf", "actual_rate", "actual_amount",
    "standard_amount", "litres_687866861", "litres_687866876",
    "litres_687872869", "udf_687866858", "udf_687872868", "udf_687872870",
)
_PM_RAW_STRING_FIELDS = (
    "standard_rate", "litres_721421314", "litres_721421315",
    "udf_553648248", "udf_671089661",
)
_PM_TEXT_FIELDS = (
    "master_id", "voucher_number", "voucher_type", "party_ledger",
    "milk_type", "shift", "godown", "group",
)


def _pm_numeric(value: Any) -> float | None:
    text = _normalize(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _extract_purchase_milk_voucher(ref: dict[str, Any]) -> dict[str, Any] | None:
    """Fetch one Purchase Milk voucher by MasterID and parse it with the
    already-validated purchase_milk_tally_parser.parse_purchase_milk()."""
    xml_text = _fetch_voucher_xml(ref)
    records = purchase_milk_tally_parser.parse_purchase_milk(
        xml_text, _normalize(ref.get("date")), _normalize(ref.get("date"))
    )
    for record in records:
        if _normalize(record.get("guid")) == _normalize(ref.get("guid")):
            return record
    return records[0] if records else None


def _persist_purchase_milk(session: Session, record: dict[str, Any]) -> PurchaseMilk:
    guid = _normalize(record.get("guid"))
    existing = session.scalar(select(PurchaseMilk).where(PurchaseMilk.guid == guid))

    if existing is not None:
        pm = existing
    else:
        pm = PurchaseMilk(guid=guid)
        session.add(pm)

    pm.master_id = _normalize(record.get("master_id"))
    pm.alter_id = _normalize(record.get("alter_id"))
    pm.date = _parse_tally_date(record.get("date"))

    for f in _PM_TEXT_FIELDS:
        setattr(pm, f, _normalize(record.get(f)) or None)
    for f in _PM_RAW_STRING_FIELDS:
        setattr(pm, f, _normalize(record.get(f)) or None)
    for f in _PM_NUMERIC_FIELDS:
        setattr(pm, f, _pm_numeric(record.get(f)))

    pm.last_synced_at = datetime.now(timezone.utc)
    return pm


# --------------------------------------------------------------------------
# Stock Journal
# --------------------------------------------------------------------------

def _extract_stock_journal_rows(ref: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch one Stock Journal voucher by MasterID and parse it with the
    already-validated stock_journal_parser_final.parse_file() -- reused
    unmodified by writing the fetched XML to a temp file, since that
    parser's proven logic operates on a file path."""
    xml_text = _fetch_voucher_xml(ref)

    temp_dir = Path("storage") / "sync_now_stock_journal"
    temp_dir.mkdir(parents=True, exist_ok=True)

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".xml",
            prefix=f"sj_{_normalize(ref.get('master_id')) or 'unknown'}_",
            dir=temp_dir,
            delete=False,
        ) as handle:
            handle.write(xml_text)
            temp_path = Path(handle.name)

        from dataclasses import asdict
        movements = stock_journal_parser_final.parse_xml(temp_path)
        return [asdict(m) if hasattr(m, "__dataclass_fields__") else dict(m) for m in movements]
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _sj_numeric(value: Any) -> float | None:
    """Sanitize numeric values from the stock journal parser.
    Same logic as _pm_numeric -- empty strings become None so
    PostgreSQL Numeric columns don't reject them."""
    text = _normalize(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _persist_stock_journal(session: Session, guid: str, rows: list[dict[str, Any]]) -> None:
    session.query(StockMovement).filter(StockMovement.guid == guid).delete(
        synchronize_session=False
    )

    for row in rows:
        session.add(
            StockMovement(
                guid=_normalize(row.get("guid")),
                master_id=_normalize(row.get("master_id")),
                alter_id=_normalize(row.get("alter_id")),
                voucher_date=_parse_tally_date(row.get("date")),
                voucher_number=_normalize(row.get("voucher_number")),
                voucher_type=_normalize(row.get("voucher_type")) or "Stock Journal",
                stock_item=_normalize(row.get("stock_item")),
                quantity=_sj_numeric(row.get("quantity")),
                unit=_normalize(row.get("unit")) or None,
                rate=_sj_numeric(row.get("rate")),
                amount=_sj_numeric(row.get("amount")),
                source_godown=_normalize(row.get("source_godown")) or None,
                destination_godown=_normalize(row.get("destination_godown")) or None,
                movement_type=_normalize(row.get("movement_type")) or "OUT",
                batch_name=_normalize(row.get("batch_name")) or None,
            )
        )


# --------------------------------------------------------------------------
# Checkpoint
# --------------------------------------------------------------------------

def get_checkpoint_date(session: Session) -> date_cls | None:
    cp = session.scalar(
        select(SyncCheckpoint).where(SyncCheckpoint.sync_type == CHECKPOINT_SYNC_TYPE)
    )
    if cp is None or not cp.metadata_json:
        return None
    last_to_date = cp.metadata_json.get("last_to_date")
    return _parse_tally_date(last_to_date) if last_to_date else None


def _advance_checkpoint(session: Session, to_date: str) -> None:
    cp = session.scalar(
        select(SyncCheckpoint).where(SyncCheckpoint.sync_type == CHECKPOINT_SYNC_TYPE)
    )
    if cp is None:
        cp = SyncCheckpoint(sync_type=CHECKPOINT_SYNC_TYPE)
        session.add(cp)
    cp.last_synced_at = datetime.now(timezone.utc)
    cp.status = "success"
    cp.metadata_json = {"last_to_date": to_date}


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

_EXTRACT_AND_PERSIST = None  # set below, kept as a lookup table for clarity


def _process_one(session: Session, ref: dict[str, Any]) -> None:
    voucher_type = _normalize(ref.get("voucher_type"))

    if voucher_type in ("Sales", "SALES MILKS"):
        voucher, inventory_rows, accounting_rows = _extract_sales_voucher(ref)
        _persist_sales(session, voucher, inventory_rows, accounting_rows)

    elif voucher_type == "Purchase Milk":
        record = _extract_purchase_milk_voucher(ref)
        if record is None:
            raise ValueError("Purchase Milk voucher fetch returned no matching record")
        _persist_purchase_milk(session, record)

    elif voucher_type == "Stock Journal":
        guid = _normalize(ref.get("guid"))
        rows = _extract_stock_journal_rows(ref)
        _persist_stock_journal(session, guid, rows)

    else:
        raise ValueError(f"Unsupported voucher_type for processing: {voucher_type!r}")


def sync_now(
    from_date: str,
    to_date: str,
    *,
    dry_run: bool = True,
    voucher_types: set[str] | None = None,
) -> dict[str, Any]:
    """Execute the Sync Now orchestration for [from_date, to_date] (YYYYMMDD).

    Dry-run (default): discovers and classifies only, no DB writes.
    Write mode: fetches and persists every NEW/CHANGED voucher across
    Sales, Purchase Milk, and Stock Journal, one voucher per commit so a
    single bad voucher never blocks the rest of the batch. On a
    zero-error write run, the sync checkpoint is advanced to `to_date`.

    `voucher_types`, if given, restricts discovery to just those Tally
    voucher-type names (e.g. {"Stock Journal"}) -- for isolating a single
    path during testing. When set, the checkpoint is NEVER advanced,
    even on a clean zero-error run: advancing it would mark the whole
    window done for every type, causing a later --since-last run to
    silently skip whatever types weren't included in this filtered run.
    """
    discovered = discover_daybook_vouchers(from_date, to_date)

    if voucher_types is not None:
        discovered = [
            d for d in discovered
            if _normalize(d.get("voucher_type")) in voucher_types
        ]

    with SessionLocal() as session:
        plan = build_plan(session, discovered)

        summary = {
            "from_date": from_date,
            "to_date": to_date,
            "discovered": len(discovered),
            "new": len(plan.new),
            "changed": len(plan.changed),
            "unchanged": len(plan.unchanged),
            "unsupported": len(plan.unsupported),
            "dry_run": dry_run,
            "processed": 0,
            "errors": [],
            "checkpoint_eligible": voucher_types is None,
        }

        if dry_run:
            return summary

        work = list(plan.new) + list(plan.changed)

        total_work = len(work)
        progress_step = max(1, total_work // 20)  # ~20 updates across the batch, never more
        for i, ref in enumerate(work, start=1):
            try:
                _process_one(session, ref)
                session.commit()
                summary["processed"] += 1
            except Exception as exc:
                session.rollback()
                summary["errors"].append({
                    "voucher_type": _normalize(ref.get("voucher_type")),
                    "guid": _normalize(ref.get("guid")),
                    "master_id": _normalize(ref.get("master_id")),
                    "voucher_number": _normalize(ref.get("voucher_number")),
                    "error": str(exc),
                })

            if total_work > 20 and (i % progress_step == 0 or i == total_work):
                print(f"  ... {i}/{total_work} processed "
                      f"({len(summary['errors'])} errors so far)")

        if not summary["errors"] and voucher_types is None:
            _advance_checkpoint(session, to_date)
            session.commit()

        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Prabha Dairy Sync Now")
    parser.add_argument("from_date", nargs="?", help="YYYYMMDD (omit with --since-last)")
    parser.add_argument("to_date", nargs="?", help="YYYYMMDD (defaults to today)")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Enable DB writes (default is dry-run: discover + classify only)",
    )
    parser.add_argument(
        "--since-last",
        action="store_true",
        help="Use the saved checkpoint as from_date (day after the last "
             "successful sync's to_date). Fails if no checkpoint exists yet "
             "-- run once with an explicit from_date first.",
    )
    parser.add_argument(
        "--types",
        help="Comma-separated Tally voucher-type names to restrict this run "
             "to, e.g. --types \"Stock Journal\" -- for isolating a single "
             "path during testing. Checkpoint is never advanced when this "
             "is set, even on a clean run, so it's safe to use repeatedly "
             "without disturbing --since-last.",
    )
    args = parser.parse_args()

    to_date = args.to_date or datetime.now().strftime("%Y%m%d")

    if args.since_last:
        with SessionLocal() as session:
            checkpoint_date = get_checkpoint_date(session)
        if checkpoint_date is None:
            print("No checkpoint found yet -- run once with an explicit "
                  "from_date first, e.g.:\n"
                  "  python sync_now_service.py 20260401 20260819 --write")
            return 1
        from_date = (checkpoint_date + timedelta(days=1)).strftime("%Y%m%d")
    elif args.from_date:
        from_date = args.from_date
    else:
        print("Provide from_date, or pass --since-last to resume from the "
              "last checkpoint.")
        return 1

    print("PRABHA DAIRY - SYNC NOW")
    print("=" * 60)
    print(f"Window : {from_date} -> {to_date}")
    print(f"Mode   : {'WRITE' if args.write else 'DRY RUN'}")
    voucher_types = None
    if args.types:
        voucher_types = {t.strip() for t in args.types.split(",") if t.strip()}
        print(f"Types  : {', '.join(sorted(voucher_types))} (checkpoint will NOT advance)")
    print()

    try:
        result = sync_now(from_date, to_date, dry_run=not args.write, voucher_types=voucher_types)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Discovered : {result['discovered']}")
    print(f"New        : {result['new']}")
    print(f"Changed    : {result['changed']}")
    print(f"Unchanged  : {result['unchanged']}")
    print(f"Unsupported: {result['unsupported']}")
    print(f"Processed  : {result['processed']}")
    if result["errors"]:
        print("Errors     :")
        for error in result["errors"]:
            print(f"  - {error}")
    else:
        if args.write and result["checkpoint_eligible"]:
            print("Checkpoint : advanced to", to_date)
        elif args.write:
            print("Checkpoint : NOT advanced (--types filter was active)")
    print()
    print("SYNC NOW COMPLETE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
