"""
Sales / Sales MILKS inventory parser for Tally ERP 9.

Input:
  Raw XML files exported with inventory details using:
    <WALK>ALLINVENTORYENTRIES</WALK>

The parser:
- processes INVENTORYENTRIES.LIST only
- uses BATCHALLOCATIONS.LIST when available for godown/batch detail
- skips Tally's empty/header-only inventory nodes
- treats actual stock quantity as the movement quantity
- classifies negative/deemed-no sales quantities as OUT
- preserves source voucher GUID + ALTERID for sync identity
- does NOT modify Tally
"""

import csv
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


INPUT_DIR = Path("tally_extracted_data")
OUTPUT_DIR = Path("storage")
OUTPUT_JSON = OUTPUT_DIR / "sales_inventory_movements.json"
OUTPUT_CSV = OUTPUT_DIR / "sales_inventory_movements.csv"

SALES_TYPES = {"Sales", "SALES MILKS"}


def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def tag_value(text, tag):
    m = re.search(
        rf"<{re.escape(tag)}(?:\s[^>]*)?>(.*?)</{re.escape(tag)}>",
        text,
        flags=re.I | re.S,
    )
    return clean_text(m.group(1)) if m else ""


def attr_value(text, tag, attr):
    m = re.search(
        rf"<{re.escape(tag)}\b[^>]*\b{re.escape(attr)}=[\"'](.*?)[\"'][^>]*>",
        text,
        flags=re.I | re.S,
    )
    return clean_text(m.group(1)) if m else ""


def number_from(value):
    value = clean_text(value)
    if not value:
        return None
    value = value.replace(",", "")
    value = re.sub(r"[^\d.\-+]", "", value)
    if value in {"", "-", "+", "."}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def blocks(text, tag):
    return re.findall(
        rf"<{re.escape(tag)}(?:\s[^>]*)?>(.*?)</{re.escape(tag)}>",
        text,
        flags=re.I | re.S,
    )


def split_vouchers(xml_text):
    return blocks(xml_text, "VOUCHER")


def parse_batch(batch_text):
    qty = tag_value(batch_text, "ACTUALQTY") or tag_value(batch_text, "BILLEDQTY")
    return {
        "quantity": number_from(qty),
        "unit": clean_text(tag_value(batch_text, "BASEUNITS")),
        "rate": number_from(tag_value(batch_text, "RATE")),
        "amount": number_from(tag_value(batch_text, "AMOUNT")),
        "source_godown": clean_text(tag_value(batch_text, "GODOWNNAME")),
        "destination_godown": clean_text(tag_value(batch_text, "DESTINATIONGODOWNNAME")),
        "batch_name": clean_text(tag_value(batch_text, "BATCHNAME")),
    }


def movement_type(quantity, deemed_positive, source, destination):
    if source and destination and source.lower() != destination.lower():
        return "GODOWN_TRANSFER"

    # In Sales vouchers, actual sales quantities normally appear negative
    # with ISDEEMEDPOSITIVE = No. Quantity is the primary stock-direction
    # signal; deemed-positive is retained as source information.
    if quantity is not None:
        return "IN" if quantity > 0 else "OUT"

    if deemed_positive.lower() in {"yes", "true", "1"}:
        return "IN"

    return "OUT"


def is_empty_placeholder(row):
    return (
        not row.get("stock_item")
        and row.get("quantity") is None
        and row.get("amount") is None
        and not row.get("source_godown")
        and not row.get("destination_godown")
    )


def parse_inventory_entry(entry_text, meta):
    item = clean_text(tag_value(entry_text, "STOCKITEMNAME"))
    qty_raw = tag_value(entry_text, "ACTUALQTY") or tag_value(entry_text, "BILLEDQTY")
    quantity = number_from(qty_raw)
    deemed_positive = clean_text(tag_value(entry_text, "ISDEEMEDPOSITIVE"))

    entry_rate = number_from(tag_value(entry_text, "RATE"))
    entry_amount = number_from(tag_value(entry_text, "AMOUNT"))

    batch_blocks = blocks(entry_text, "BATCHALLOCATIONS.LIST")
    rows = []

    if batch_blocks:
        for batch in batch_blocks:
            b = parse_batch(batch)

            row_qty = b["quantity"] if b["quantity"] is not None else quantity
            row_rate = b["rate"] if b["rate"] is not None else entry_rate
            row_amount = b["amount"] if b["amount"] is not None else entry_amount

            row = {
                **meta,
                "stock_item": item,
                "quantity": row_qty,
                "unit": b["unit"] or clean_text(tag_value(entry_text, "BASEUNITS")),
                "rate": row_rate,
                "amount": row_amount,
                "source_godown": b["source_godown"],
                "destination_godown": b["destination_godown"],
                "batch_name": b["batch_name"],
                "is_deemed_positive": deemed_positive,
                "movement_type": movement_type(
                    row_qty,
                    deemed_positive,
                    b["source_godown"],
                    b["destination_godown"],
                ),
            }

            if not is_empty_placeholder(row):
                rows.append(row)
    else:
        row = {
            **meta,
            "stock_item": item,
            "quantity": quantity,
            "unit": clean_text(tag_value(entry_text, "BASEUNITS")),
            "rate": entry_rate,
            "amount": entry_amount,
            "source_godown": clean_text(tag_value(entry_text, "GODOWNNAME")),
            "destination_godown": clean_text(tag_value(entry_text, "DESTINATIONGODOWNNAME")),
            "batch_name": clean_text(tag_value(entry_text, "BATCHNAME")),
            "is_deemed_positive": deemed_positive,
            "movement_type": movement_type(
                quantity,
                deemed_positive,
                clean_text(tag_value(entry_text, "GODOWNNAME")),
                clean_text(tag_value(entry_text, "DESTINATIONGODOWNNAME")),
            ),
        }

        if not is_empty_placeholder(row):
            rows.append(row)

    return rows


def parse_voucher(voucher_text):
    voucher_type = clean_text(tag_value(voucher_text, "VOUCHERTYPENAME"))
    if voucher_type not in SALES_TYPES:
        return []

    meta = {
        "date": clean_text(tag_value(voucher_text, "DATE")),
        "voucher_number": clean_text(tag_value(voucher_text, "VOUCHERNUMBER")),
        "voucher_type": voucher_type,
        "party_ledger": clean_text(tag_value(voucher_text, "PARTYLEDGERNAME")),
        "guid": attr_value(voucher_text, "VOUCHER", "REMOTEID")
                or tag_value(voucher_text, "GUID"),
        "master_id": clean_text(tag_value(voucher_text, "MASTERID")),
        "alter_id": clean_text(tag_value(voucher_text, "ALTERID")),
    }

    rows = []
    for entry in blocks(voucher_text, "INVENTORYENTRIES.LIST"):
        rows.extend(parse_inventory_entry(entry, meta))

    return rows


def parse_file(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    rows = []
    for voucher in split_vouchers(text):
        rows.extend(parse_voucher(voucher))
    return rows


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(
        p for p in INPUT_DIR.glob("*.xml")
        if "sales" in p.name.lower()
        and "raw" in p.name.lower()
    )

    all_rows = []
    for path in files:
        rows = parse_file(path)
        print(f"{path.name}: {len(rows)} movement rows")
        all_rows.extend(rows)

    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(all_rows, f, indent=2, ensure_ascii=False)

    fields = [
        "date", "voucher_number", "voucher_type", "party_ledger",
        "guid", "master_id", "alter_id",
        "stock_item", "quantity", "unit", "rate", "amount",
        "source_godown", "destination_godown", "batch_name",
        "is_deemed_positive", "movement_type",
    ]

    with OUTPUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"Total movement rows: {len(all_rows)}")
    print(f"JSON: {OUTPUT_JSON}")
    print(f"CSV : {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
