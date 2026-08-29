from __future__ import annotations

import csv
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

INPUT = Path("storage/sales_accounting_transactions.json")
OUT_JSON = Path("storage/unified_accounting_ledger.json")
OUT_CSV = Path("storage/unified_accounting_ledger.csv")
VALIDATION_JSON = Path("storage/accounting_validation.json")


def s(v):
    return "" if v is None else str(v).strip()


def money(v):
    if v is None or v == "":
        return None
    try:
        return float(Decimal(str(v)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def normalize(r):
    return {
        "date": s(r.get("date")),
        "voucher_number": s(r.get("voucher_number")),
        "voucher_type": s(r.get("voucher_type")),
        "guid": s(r.get("guid")),
        "master_id": s(r.get("master_id")),
        "alter_id": s(r.get("alter_id")),
        "party_ledger": s(r.get("party_ledger")),
        "reference": s(r.get("reference")),
        "is_invoice": s(r.get("is_invoice")),
        "ledger_name": s(r.get("ledger_name")),
        "amount": money(r.get("amount")),
        "is_deemed_positive": s(r.get("is_deemed_positive")),
        "is_party_ledger": s(r.get("is_party_ledger")),
        "ledger_from_item": s(r.get("ledger_from_item")),
        "bill_reference": s(r.get("bill_reference")),
        "bill_date": s(r.get("bill_date")),
        "bill_type": s(r.get("bill_type")),
        "cost_centre": s(r.get("cost_centre")),
    }


def dedup_key(r):
    return (
        r["guid"], r["master_id"], r["alter_id"], r["voucher_number"],
        r["ledger_name"], r["amount"], r["is_deemed_positive"],
        r["is_party_ledger"], r["bill_reference"], r["bill_date"],
        r["bill_type"], r["cost_centre"],
    )


def main():
    if not INPUT.exists():
        raise SystemExit(f"Missing input: {INPUT}")

    raw = json.loads(INPUT.read_text(encoding="utf-8"))
    rows = [normalize(r) for r in raw]

    seen = set()
    ledger = []
    duplicates = 0

    for r in rows:
        k = dedup_key(r)
        if k in seen:
            duplicates += 1
            continue
        seen.add(k)
        ledger.append(r)

    ledger.sort(key=lambda r: (
        r["date"], r["voucher_number"], r["ledger_name"], r["guid"]
    ))

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    fields = list(ledger[0].keys()) if ledger else []
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(ledger)

    voucher_ids = {
        (r["guid"], r["master_id"], r["alter_id"], r["voucher_number"])
        for r in ledger
        if r["guid"] or r["master_id"] or r["voucher_number"]
    }

    ledger_totals = defaultdict(float)
    for r in ledger:
        if r["ledger_name"] and r["amount"] is not None:
            ledger_totals[r["ledger_name"]] += r["amount"]

    deemed = defaultdict(float)
    for r in ledger:
        if r["amount"] is not None:
            deemed[r["is_deemed_positive"] or ""] += r["amount"]

    validation = {
        "source_rows": len(raw),
        "normalized_rows": len(rows),
        "duplicates_removed": duplicates,
        "unified_accounting_rows": len(ledger),
        "vouchers_with_ledger_entries": len(voucher_ids),
        "rows_without_voucher_identity": sum(
            1 for r in ledger
            if not (r["guid"] or r["master_id"] or r["voucher_number"])
        ),
        "rows_without_ledger_name": sum(
            1 for r in ledger if not r["ledger_name"]
        ),
        "rows_without_amount": sum(
            1 for r in ledger if r["amount"] is None
        ),
        "deemed_positive_amount_totals": dict(sorted(deemed.items())),
        "top_ledger_totals_by_absolute_amount": [
            {"ledger_name": name, "amount": amount}
            for name, amount in sorted(
                ledger_totals.items(),
                key=lambda x: abs(x[1]),
                reverse=True
            )[:20]
        ],
    }

    VALIDATION_JSON.write_text(
        json.dumps(validation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("UNIFIED ACCOUNTING LEDGER")
    print(f"Source accounting rows       : {len(raw)}")
    print(f"Duplicates removed           : {duplicates}")
    print(f"Unified accounting rows      : {len(ledger)}")
    print(f"Vouchers with ledger entries : {len(voucher_ids)}")
    print(f"Rows without voucher ID      : {validation['rows_without_voucher_identity']}")
    print(f"Rows without ledger name     : {validation['rows_without_ledger_name']}")
    print(f"Rows without amount          : {validation['rows_without_amount']}")
    print()
    print(f"JSON -> {OUT_JSON}")
    print(f"CSV  -> {OUT_CSV}")
    print(f"Validation -> {VALIDATION_JSON}")


if __name__ == "__main__":
    main()
