"""
services/sync_service.py

Orchestrates: Tally (read-only export) -> parser -> validation -> Postgres.

Purchase Milk and Stock Journal syncs are kept separate so the existing
Purchase Milk behavior remains unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlalchemy.orm import Session

from database.db import get_session
from database.models import PurchaseMilk, StockMovement, SyncRun
from schemas.purchase_milk import validate_record, RecordValidationError

# The real, validated connector/parser modules.
import tally_connector
import purchase_milk_tally_parser


SYNC_TYPE_PURCHASE_MILK = "purchase_milk"
SYNC_TYPE_STOCK_JOURNAL = "stock_journal"


@dataclass
class SyncResult:
    status: str  # "success" | "failed"
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    error: str | None = None
    sync_run_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        d = {
            "status": self.status,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "sync_run_id": self.sync_run_id,
        }
        if self.error:
            d["error"] = self.error
        return d


def _tally_date_str(d: date) -> str:
    return d.strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# PURCHASE MILK -- existing working implementation
# ---------------------------------------------------------------------------

def _apply_records(
    session: Session,
    counts: dict[str, int],
    records: list[dict[str, Any]],
) -> str | None:
    """
    Existing Purchase Milk upsert logic.

    Decision rule:
      guid not in DB                 -> INSERT
      guid in DB, same alter_id      -> DO NOTHING (unchanged)
      guid in DB, different alter_id -> UPDATE
    """
    counts["fetched"] = len(records)

    validated_by_guid: dict[str, Any] = {}
    error_notes: list[str] = []

    for raw in records:
        try:
            v = validate_record(raw)
        except RecordValidationError as exc:
            counts["failed"] += 1
            error_notes.append(f"[{raw.get('guid', '?')}] {exc}")
            continue

        validated_by_guid[v.guid] = v

    if not validated_by_guid:
        return " | ".join(error_notes) or None

    guids = list(validated_by_guid.keys())
    existing = (
        session.query(PurchaseMilk.id, PurchaseMilk.guid, PurchaseMilk.alter_id)
        .filter(PurchaseMilk.guid.in_(guids))
        .all()
    )
    existing_by_guid = {
        row.guid: (row.id, row.alter_id)
        for row in existing
    }

    now = datetime.now(timezone.utc)
    to_insert: list[dict[str, Any]] = []
    to_update: list[dict[str, Any]] = []

    for v in validated_by_guid.values():
        if v.guid not in existing_by_guid:
            row = dict(v.values)
            row["guid"] = v.guid
            row["alter_id"] = v.alter_id
            row["created_at"] = now
            row["updated_at"] = now
            row["last_synced_at"] = now
            to_insert.append(row)
            counts["inserted"] += 1
            continue

        existing_id, existing_alter_id = existing_by_guid[v.guid]

        if existing_alter_id == v.alter_id:
            counts["unchanged"] += 1
            continue

        row = dict(v.values)
        row["id"] = existing_id
        row["alter_id"] = v.alter_id
        row["updated_at"] = now
        row["last_synced_at"] = now
        to_update.append(row)
        counts["updated"] += 1

    if to_insert:
        session.bulk_insert_mappings(PurchaseMilk, to_insert)

    if to_update:
        session.bulk_update_mappings(PurchaseMilk, to_update)

    return " | ".join(error_notes) or None


def _run_sync(
    fetch_records: Callable[[], list[dict[str, Any]]]
) -> SyncResult:
    """Existing Purchase Milk shared runner."""

    started_at = datetime.now(timezone.utc)

    with get_session() as track_session:
        sync_run = SyncRun(
            sync_type=SYNC_TYPE_PURCHASE_MILK,
            status="running",
            started_at=started_at,
        )
        track_session.add(sync_run)
        track_session.flush()
        run_id = sync_run.id

    counts = {
        "fetched": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "failed": 0,
    }

    error_message: str | None = None
    status = "success"

    try:
        records = fetch_records()

        with get_session() as data_session:
            error_message = _apply_records(
                data_session,
                counts,
                records,
            )

    except Exception as exc:
        status = "failed"
        counts = {
            "fetched": counts.get("fetched", 0),
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "failed": 0,
        }
        error_message = (
            f"{error_message + ' | ' if error_message else ''}{exc}"
        )

    completed_at = datetime.now(timezone.utc)

    with get_session() as track_session:
        sync_run = track_session.get(SyncRun, run_id)
        sync_run.status = status
        sync_run.completed_at = completed_at
        sync_run.records_fetched = counts["fetched"]
        sync_run.records_inserted = counts["inserted"]
        sync_run.records_updated = counts["updated"]
        sync_run.records_unchanged = counts["unchanged"]
        sync_run.records_failed = counts["failed"]
        sync_run.error_message = error_message

    return SyncResult(
        status=status,
        fetched=counts["fetched"],
        inserted=counts["inserted"],
        updated=counts["updated"],
        unchanged=counts["unchanged"],
        failed=counts["failed"],
        error=error_message,
        sync_run_id=run_id,
    )


def sync_purchase_milk(
    from_date: date,
    to_date: date,
) -> SyncResult:
    """Live read-only Purchase Milk sync from Tally."""

    def fetch_records() -> list[dict[str, Any]]:
        records = tally_connector.extract_purchase_milk(
            _tally_date_str(from_date),
            _tally_date_str(to_date),
        )

        if records is None:
            raise RuntimeError(
                "Tally extraction failed -- extract_purchase_milk() returned "
                "None (see connector output above for the underlying error: "
                "connection, HTTP status, or XML parse failure)."
            )

        return records

    return _run_sync(fetch_records)


def sync_purchase_milk_from_xml(
    xml_text: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> SyncResult:
    """Sync Purchase Milk from already-saved XML without touching Tally."""

    def fetch_records() -> list[dict[str, Any]]:
        return purchase_milk_tally_parser.parse_purchase_milk(
            xml_text,
            from_date,
            to_date,
        )

    return _run_sync(fetch_records)


def sync_purchase_milk_from_records(
    records: list[dict[str, Any]],
) -> SyncResult:
    """Sync Purchase Milk from already-parsed records without Tally."""

    return _run_sync(lambda: records)


# ---------------------------------------------------------------------------
# STOCK JOURNAL -- new implementation
# ---------------------------------------------------------------------------

def _stock_decimal(value: Any) -> Decimal | None:
    """Convert parser numeric values safely to Decimal for PostgreSQL."""
    if value is None or value == "":
        return None

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Invalid numeric value: {value!r}")


def _stock_date(value: Any) -> date:
    """Convert the parser's YYYYMMDD date to a Python date."""
    if isinstance(value, date):
        return value

    text = str(value or "").strip()

    if not text:
        raise ValueError("Missing Stock Journal date")

    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    raise ValueError(f"Invalid Stock Journal date: {value!r}")


def _normalize_stock_record(
    raw: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert one stock_journal_movements.json record into the exact
    StockMovement model fields.

    The parser produces:
      date, guid, master_id, alter_id, voucher_number, voucher_type,
      direction, stock_item, quantity, unit, rate, amount,
      source_godown, destination_godown, batch_name, movement_type
    """

    guid = str(raw.get("guid") or "").strip()
    alter_id = str(raw.get("alter_id") or "").strip()
    stock_item = str(raw.get("stock_item") or "").strip()

    if not guid:
        raise ValueError("Missing guid")

    if not alter_id:
        raise ValueError(f"[{guid}] Missing alter_id")

    if not stock_item:
        raise ValueError(f"[{guid}] Missing stock_item")

    movement_type = str(
        raw.get("movement_type")
        or raw.get("direction")
        or ""
    ).strip().upper()

    if movement_type not in {"IN", "OUT", "GODOWN_TRANSFER"}:
        raise ValueError(
            f"[{guid}] Invalid movement_type: {movement_type!r}"
        )

    return {
        "guid": guid,
        "master_id": (
            str(raw["master_id"]).strip()
            if raw.get("master_id") not in (None, "")
            else None
        ),
        "alter_id": alter_id,
        "voucher_date": _stock_date(raw.get("date")),
        "voucher_number": (
            str(raw["voucher_number"]).strip()
            if raw.get("voucher_number") not in (None, "")
            else None
        ),
        "voucher_type": (
            str(raw.get("voucher_type")).strip()
            if raw.get("voucher_type") not in (None, "")
            else "Stock Journal"
        ),
        "stock_item": stock_item,
        "quantity": _stock_decimal(raw.get("quantity")),
        "unit": (
            str(raw["unit"]).strip()
            if raw.get("unit") not in (None, "")
            else None
        ),
        "rate": _stock_decimal(raw.get("rate")),
        "amount": _stock_decimal(raw.get("amount")),
        "source_godown": (
            str(raw["source_godown"]).strip()
            if raw.get("source_godown") not in (None, "")
            else None
        ),
        "destination_godown": (
            str(raw["destination_godown"]).strip()
            if raw.get("destination_godown") not in (None, "")
            else None
        ),
        "movement_type": movement_type,
        "batch_name": (
            str(raw["batch_name"]).strip()
            if raw.get("batch_name") not in (None, "")
            else None
        ),
    }


def _apply_stock_journal_records(
    session: Session,
    counts: dict[str, int],
    records: list[dict[str, Any]],
) -> str | None:
    """
    Stock Journal GUID + ALTERID upsert.

    Decision rule:
      guid not in DB                  -> INSERT
      guid in DB, same alter_id       -> UNCHANGED
      guid in DB, different alter_id -> UPDATE

    A Stock Journal parser row is a movement record. Multiple rows may share
    the same voucher GUID, so the database identity is NOT the GUID alone.
    The unique identity is the voucher GUID + movement-row characteristics.
    Therefore each parsed movement row receives a deterministic row key by
    combining GUID with its row content.

    The existing StockMovement table currently stores GUID as the source
    voucher GUID. To safely support multiple rows from one voucher, this
    loader checks existing rows using the full movement identity before
    inserting/updating.
    """

    counts["fetched"] = len(records)

    normalized: list[dict[str, Any]] = []
    error_notes: list[str] = []

    for index, raw in enumerate(records, start=1):
        try:
            normalized.append(_normalize_stock_record(raw))
        except Exception as exc:
            counts["failed"] += 1
            error_notes.append(
                f"[row {index}, guid={raw.get('guid', '?')}] {exc}"
            )

    if not normalized:
        return " | ".join(error_notes) or None

    now = datetime.now(timezone.utc)

    # Existing StockMovement rows are fetched by voucher GUID. Multiple
    # movement rows can legitimately belong to the same voucher.
    guids = list({row["guid"] for row in normalized})

    existing_rows = (
        session.query(StockMovement)
        .filter(StockMovement.guid.in_(guids))
        .all()
    )

    def identity(row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            row["guid"],
            row["stock_item"],
            row["quantity"],
            row["unit"],
            row["rate"],
            row["amount"],
            row["source_godown"],
            row["destination_godown"],
            row["movement_type"],
            row["batch_name"],
        )

    existing_by_identity: dict[tuple[Any, ...], StockMovement] = {}

    for row in existing_rows:
        existing_by_identity[identity({
            "guid": row.guid,
            "stock_item": row.stock_item,
            "quantity": row.quantity,
            "unit": row.unit,
            "rate": row.rate,
            "amount": row.amount,
            "source_godown": row.source_godown,
            "destination_godown": row.destination_godown,
            "movement_type": row.movement_type,
            "batch_name": row.batch_name,
        })] = row

    # Process duplicate rows from the same JSON only once.
    seen: set[tuple[Any, ...]] = set()

    for row in normalized:
        key = identity(row)

        if key in seen:
            counts["unchanged"] += 1
            continue

        seen.add(key)

        existing = existing_by_identity.get(key)

        if existing is None:
            insert_row = dict(row)
            insert_row["created_at"] = now
            insert_row["updated_at"] = now
            session.bulk_insert_mappings(
                StockMovement,
                [insert_row],
            )
            counts["inserted"] += 1
            continue

        # Same movement identity but a changed ALTERID means Tally altered
        # the source voucher. Update the stored metadata/value fields.
        if str(existing.alter_id) == row["alter_id"]:
            counts["unchanged"] += 1
            continue

        update_row = dict(row)
        update_row["id"] = existing.id
        update_row["updated_at"] = now

        session.bulk_update_mappings(
            StockMovement,
            [update_row],
        )
        counts["updated"] += 1

    return " | ".join(error_notes) or None


def _run_stock_journal_sync(
    records: list[dict[str, Any]],
) -> SyncResult:
    """Run one Stock Journal data transaction with an audit SyncRun."""

    started_at = datetime.now(timezone.utc)

    with get_session() as track_session:
        sync_run = SyncRun(
            sync_type=SYNC_TYPE_STOCK_JOURNAL,
            status="running",
            started_at=started_at,
        )
        track_session.add(sync_run)
        track_session.flush()
        run_id = sync_run.id

    counts = {
        "fetched": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "failed": 0,
    }

    error_message: str | None = None
    status = "success"

    try:
        with get_session() as data_session:
            error_message = _apply_stock_journal_records(
                data_session,
                counts,
                records,
            )

    except Exception as exc:
        status = "failed"
        counts = {
            "fetched": counts.get("fetched", len(records)),
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "failed": 0,
        }
        error_message = (
            f"{error_message + ' | ' if error_message else ''}{exc}"
        )

    completed_at = datetime.now(timezone.utc)

    with get_session() as track_session:
        sync_run = track_session.get(SyncRun, run_id)

        sync_run.status = status
        sync_run.completed_at = completed_at
        sync_run.records_fetched = counts["fetched"]
        sync_run.records_inserted = counts["inserted"]
        sync_run.records_updated = counts["updated"]
        sync_run.records_unchanged = counts["unchanged"]
        sync_run.records_failed = counts["failed"]
        sync_run.error_message = error_message

    return SyncResult(
        status=status,
        fetched=counts["fetched"],
        inserted=counts["inserted"],
        updated=counts["updated"],
        unchanged=counts["unchanged"],
        failed=counts["failed"],
        error=error_message,
        sync_run_id=run_id,
    )


def sync_stock_journal_from_records(
    records: list[dict[str, Any]],
) -> SyncResult:
    """
    Load an already-parsed Stock Journal JSON export into PostgreSQL.

    This function NEVER contacts Tally.
    """
    return _run_stock_journal_sync(records)
