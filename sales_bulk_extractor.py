"""
Bulk Sales / SALES MILKS inventory extractor for Tally ERP 9.

Purpose:
    Read-only extraction of Sales inventory movements from Tally.

Date range:
    20260401 -> 20260819

Flow:
    1. Fetch vouchers from Tally using the existing connector.
    2. Request ALLINVENTORYENTRIES for each voucher type.
    3. Verify voucher dates client-side.
    4. Parse actual inventory movements.
    5. Save raw XML plus parsed JSON/CSV.

IMPORTANT:
    This script NEVER writes to Tally.
    It assumes the project already has a working tally_connector.py with
    a TallyConnector class exposing:
        connector.export(request_xml)

    If your connector class/method has a different name, adjust only the
    import/connection section below.
"""

import csv
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime


# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------

FROM_DATE = "20260401"
TO_DATE = "20260819"

INPUT_DIR = Path("tally_extracted_data")
OUTPUT_DIR = Path("storage")

RAW_DIR = INPUT_DIR
OUTPUT_JSON = OUTPUT_DIR / "sales_inventory_movements_bulk.json"
OUTPUT_CSV = OUTPUT_DIR / "sales_inventory_movements_bulk.csv"

VOUCHER_TYPES = [
    "Sales",
    "SALES MILKS",
]


# ---------------------------------------------------------------------
# CONNECTOR
# ---------------------------------------------------------------------

try:
    from tally_connector import TallyConnector
except ImportError:
    TallyConnector = None


# ---------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------

def clean(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def tag_value(text, tag):
    m = re.search(
        rf"<{re.escape(tag)}(?:\s[^>]*)?>(.*?)</{re.escape(tag)}>",
        text,
        flags=re.I | re.S,
    )
    return clean(m.group(1)) if m else ""


def attr_value(text, tag, attr):
    m = re.search(
        rf"<{re.escape(tag)}\b[^>]*\b{re.escape(attr)}=[\"'](.*?)[\"'][^>]*>",
        text,
        flags=re.I | re.S,
    )
    return clean(m.group(1)) if m else ""


def number(value):
    value = clean(value).replace(",", "")
    if not value:
        return None

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


def voucher_date(voucher):
    return clean(tag_value(voucher, "DATE"))


def in_requested_range(date_text):
    return FROM_DATE <= date_text <= TO_DATE


def is_empty_movement(row):
    return (
        not row.get("stock_item")
        and row.get("quantity") is None
        and row.get("amount") is None
        and not row.get("source_godown")
        and not row.get("destination_godown")
    )


# ---------------------------------------------------------------------
# REQUEST XML
# ---------------------------------------------------------------------

def build_voucher_request(voucher_type):
    """
    Export vouchers of one type with complete inventory detail.

    The date filter is intentionally broad enough that we do NOT trust it
    as the final boundary. Dates are verified after Tally responds.
    """

    return f"""<ENVELOPE>
<HEADER>
<TALLYREQUEST>Export Data</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>Sales Inventory Export</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVCURRENTCOMPANY>SHRI JAIN BANDHU GRAMODYOG - (from 1-Apr-2026)</SVCURRENTCOMPANY>
<SVFROMDATE>{FROM_DATE}</SVFROMDATE>
<SVTODATE>{TO_DATE}</SVTODATE>
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>

<COLLECTION NAME="Sales Inventory Collection">
<TYPE>Voucher</TYPE>
<FILTER>SalesTypeFilter</FILTER>
<FETCH>
DATE,
VOUCHERNUMBER,
VOUCHERTYPENAME,
PARTYLEDGERNAME,
GUID,
MASTERID,
ALTERID,
ALLINVENTORYENTRIES
</FETCH>
</COLLECTION>

<SYSTEM TYPE="Formula" NAME="SalesTypeFilter">
$VOUCHERTYPENAME = "{voucher_type}"
</SYSTEM>

</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""


# ---------------------------------------------------------------------
# INVENTORY PARSING
# ---------------------------------------------------------------------

def parse_batch(batch_text):
    qty = tag_value(batch_text, "ACTUALQTY") or tag_value(batch_text, "BILLEDQTY")

    return {
        "quantity": number(qty),
        "unit": clean(tag_value(batch_text, "BASEUNITS")),
        "rate": number(tag_value(batch_text, "RATE")),
        "amount": number(tag_value(batch_text, "AMOUNT")),
        "source_godown": clean(tag_value(batch_text, "GODOWNNAME")),
        "destination_godown": clean(
            tag_value(batch_text, "DESTINATIONGODOWNNAME")
        ),
        "batch_name": clean(tag_value(batch_text, "BATCHNAME")),
    }


def get_movement_type(quantity, source, destination):
    if source and destination and source.lower() != destination.lower():
        return "GODOWN_TRANSFER"

    if quantity is not None and quantity > 0:
        return "IN"

    return "OUT"


def parse_inventory_entry(entry_text, meta):
    stock_item = clean(tag_value(entry_text, "STOCKITEMNAME"))

    entry_qty = tag_value(entry_text, "ACTUALQTY")
    if not entry_qty:
        entry_qty = tag_value(entry_text, "BILLEDQTY")

    quantity = number(entry_qty)
    rate = number(tag_value(entry_text, "RATE"))
    amount = number(tag_value(entry_text, "AMOUNT"))
    deemed = clean(tag_value(entry_text, "ISDEEMEDPOSITIVE"))

    batch_nodes = blocks(entry_text, "BATCHALLOCATIONS.LIST")

    rows = []

    if batch_nodes:
        for batch_text in batch_nodes:
            batch = parse_batch(batch_text)

            row = {
                **meta,
                "stock_item": stock_item,
                "quantity": (
                    batch["quantity"]
                    if batch["quantity"] is not None
                    else quantity
                ),
                "unit": (
                    batch["unit"]
                    or clean(tag_value(entry_text, "BASEUNITS"))
                ),
                "rate": (
                    batch["rate"]
                    if batch["rate"] is not None
                    else rate
                ),
                "amount": (
                    batch["amount"]
                    if batch["amount"] is not None
                    else amount
                ),
                "source_godown": batch["source_godown"],
                "destination_godown": batch["destination_godown"],
                "batch_name": batch["batch_name"],
                "is_deemed_positive": deemed,
            }

            if not is_empty_movement(row):
                row["movement_type"] = get_movement_type(
                    row["quantity"],
                    row["source_godown"],
                    row["destination_godown"],
                )
                rows.append(row)

    else:
        source = clean(tag_value(entry_text, "GODOWNNAME"))
        destination = clean(
            tag_value(entry_text, "DESTINATIONGODOWNNAME")
        )

        row = {
            **meta,
            "stock_item": stock_item,
            "quantity": quantity,
            "unit": clean(tag_value(entry_text, "BASEUNITS")),
            "rate": rate,
            "amount": amount,
            "source_godown": source,
            "destination_godown": destination,
            "batch_name": clean(tag_value(entry_text, "BATCHNAME")),
            "is_deemed_positive": deemed,
        }

        if not is_empty_movement(row):
            row["movement_type"] = get_movement_type(
                quantity,
                source,
                destination,
            )
            rows.append(row)

    return rows


def parse_voucher(voucher_text, expected_type):
    actual_type = clean(tag_value(voucher_text, "VOUCHERTYPENAME"))

    if actual_type != expected_type:
        return []

    date = voucher_date(voucher_text)

    # Tally's date filtering has previously proved unreliable.
    if not in_requested_range(date):
        return []

    meta = {
        "date": date,
        "voucher_number": clean(tag_value(voucher_text, "VOUCHERNUMBER")),
        "voucher_type": actual_type,
        "party_ledger": clean(tag_value(voucher_text, "PARTYLEDGERNAME")),
        "guid": (
            attr_value(voucher_text, "VOUCHER", "REMOTEID")
            or tag_value(voucher_text, "GUID")
        ),
        "master_id": clean(tag_value(voucher_text, "MASTERID")),
        "alter_id": clean(tag_value(voucher_text, "ALTERID")),
    }

    rows = []

    for entry in blocks(voucher_text, "INVENTORYENTRIES.LIST"):
        rows.extend(parse_inventory_entry(entry, meta))

    return rows


# ---------------------------------------------------------------------
# TALLY RESPONSE PARSING
# ---------------------------------------------------------------------

def extract_vouchers(xml_text):
    return blocks(xml_text, "VOUCHER")


def parse_response(xml_text, voucher_type):
    rows = []

    for voucher in extract_vouchers(xml_text):
        rows.extend(parse_voucher(voucher, voucher_type))

    return rows


# ---------------------------------------------------------------------
# MAIN EXTRACTION
# ---------------------------------------------------------------------

def connect():
    if TallyConnector is None:
        raise RuntimeError(
            "Could not import TallyConnector from tally_connector.py. "
            "Use the same connector module that already works in this project."
        )

    return TallyConnector()


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    connector = connect()

    all_rows = []
    summary = {}

    for voucher_type in VOUCHER_TYPES:
        print()
        print("=" * 60)
        print(f"EXTRACTING: {voucher_type}")
        print("=" * 60)

        request_xml = build_voucher_request(voucher_type)

        response = connector.export(request_xml)

        if isinstance(response, bytes):
            response_text = response.decode("utf-8", errors="replace")
        else:
            response_text = str(response)

        safe_type = voucher_type.replace(" ", "_")

        raw_path = (
            RAW_DIR
            / f"inventory_{safe_type}_{FROM_DATE}_{TO_DATE}_RAW.xml"
        )

        raw_path.write_text(response_text, encoding="utf-8")

        voucher_count = len(extract_vouchers(response_text))

        rows = parse_response(response_text, voucher_type)

        all_rows.extend(rows)

        summary[voucher_type] = {
            "vouchers_returned": voucher_count,
            "movement_rows": len(rows),
            "raw_file": str(raw_path),
        }

        print(f"Vouchers returned: {voucher_count}")
        print(f"Movement rows:     {len(rows)}")
        print(f"Raw XML:           {raw_path}")

    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(all_rows, f, indent=2, ensure_ascii=False)

    fields = [
        "date",
        "voucher_number",
        "voucher_type",
        "party_ledger",
        "guid",
        "master_id",
        "alter_id",
        "stock_item",
        "quantity",
        "unit",
        "rate",
        "amount",
        "source_godown",
        "destination_godown",
        "batch_name",
        "is_deemed_positive",
        "movement_type",
    ]

    with OUTPUT_CSV.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)

    print()
    print("=" * 60)
    print("SALES EXTRACTION COMPLETE")
    print("=" * 60)

    for voucher_type, data in summary.items():
        print(
            f"{voucher_type}: "
            f"{data['vouchers_returned']} vouchers, "
            f"{data['movement_rows']} movement rows"
        )

    print(f"TOTAL MOVEMENT ROWS: {len(all_rows)}")
    print(f"JSON: {OUTPUT_JSON}")
    print(f"CSV : {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
