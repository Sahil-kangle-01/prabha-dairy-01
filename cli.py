"""
cli.py

CLI for the Purchase Milk sync layer.

Usage:
    # Live sync from Tally (manual "Sync Now")
    python cli.py sync --from 20260401 --to 20260819

    # Initial historical load from an already-parsed JSON export
    python cli.py load-json --file storage/purchase_milk_20260401_20260819_LIVE.json

    # Load parsed Stock Journal movements into PostgreSQL
    python cli.py load-stock-journal
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

from services.sync_service import (
    sync_purchase_milk,
    sync_purchase_milk_from_records,
    sync_stock_journal_from_records,
    SyncResult,
)


def _print_result(result: SyncResult) -> None:
    print("=" * 40)
    print("PURCHASE MILK SYNC")
    print("=" * 40)
    print()
    if result.status == "failed" and result.fetched == 0:
        print(f"SYNC FAILED: {result.error}")
        print("=" * 40)
        return

    print(f"Fetched:     {result.fetched:,}")
    print(f"Inserted:    {result.inserted:,}")
    print(f"Updated:     {result.updated:,}")
    print(f"Unchanged:   {result.unchanged:,}")
    print(f"Failed:      {result.failed:,}")
    print()
    print("=" * 40)
    print("SYNC COMPLETE" if result.status == "success" else "SYNC FAILED")
    print("=" * 40)
    print()
    print(f"Last sync: {datetime.now():%Y-%m-%d %H:%M:%S}")
    if result.error:
        print(f"Note: {result.error}")


def _parse_tally_date(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def cmd_sync(args: argparse.Namespace) -> int:
    print("Fetching from Tally...")
    result = sync_purchase_milk(
        from_date=_parse_tally_date(args.from_date),
        to_date=_parse_tally_date(args.to_date),
    )
    _print_result(result)
    return 0 if result.status == "success" else 1


def cmd_load_json(args: argparse.Namespace) -> int:
    with open(args.file, "r", encoding="utf-8") as f:
        records = json.load(f)
    if not isinstance(records, list):
        print("Expected a JSON array of records.", file=sys.stderr)
        return 1

    print(f"Loading {len(records):,} records from {args.file}...")
    result = sync_purchase_milk_from_records(records)
    _print_result(result)
    return 0 if result.status == "success" else 1


def cmd_load_stock_journal(args: argparse.Namespace) -> int:
    with open(args.file, "r", encoding="utf-8") as f:
        records = json.load(f)

    if not isinstance(records, list):
        print(
            "Expected a JSON array of stock movement records.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Loading {len(records):,} stock movement records "
        f"from {args.file}...\n"
    )

    result = sync_stock_journal_from_records(records)

    print("=" * 40)
    print("STOCK JOURNAL SYNC")
    print("=" * 40)
    print()
    print(f"Fetched:     {result.fetched:,}")
    print(f"Inserted:    {result.inserted:,}")
    print(f"Updated:     {result.updated:,}")
    print(f"Unchanged:   {result.unchanged:,}")
    print(f"Failed:      {result.failed:,}")
    print()

    if result.error:
        print(f"Error: {result.error}")
        print()

    print("SYNC COMPLETE" if result.status == "success" else "SYNC FAILED")
    print("=" * 40)

    return 0 if result.status == "success" else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Purchase Milk sync CLI")
    sub = ap.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Sync Now: fetch live from Tally")
    p_sync.add_argument("--from", dest="from_date", required=True, help="YYYYMMDD")
    p_sync.add_argument("--to", dest="to_date", required=True, help="YYYYMMDD")
    p_sync.set_defaults(func=cmd_sync)

    p_load = sub.add_parser(
        "load-json",
        help="Initial load from parsed JSON export",
    )
    p_load.add_argument("--file", required=True, help="Path to *_LIVE.json")
    p_load.set_defaults(func=cmd_load_json)

    p_stock = sub.add_parser(
        "load-stock-journal",
        help="Load parsed Stock Journal movements into PostgreSQL",
    )
    p_stock.add_argument(
        "--file",
        default="storage/stock_journal_movements.json",
        help="Path to stock_journal_movements.json",
    )
    p_stock.set_defaults(func=cmd_load_stock_journal)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
