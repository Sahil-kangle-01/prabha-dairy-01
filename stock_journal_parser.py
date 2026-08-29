"""
Stock Journal parser for Tally ERP 9 raw XML exports.

This version intentionally uses a tolerant text parser rather than
ElementTree because Tally's raw XML can contain invalid XML 1.0 character
references and unbound prefixes. The original files are never modified.

Only INVENTORYENTRIESIN.LIST and INVENTORYENTRIESOUT.LIST are processed.
The combined INVENTORYENTRIES.LIST is deliberately ignored.
"""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass
class StockMovement:
    voucher_date: str
    voucher_number: str
    voucher_type: str
    guid: str
    master_id: str
    alter_id: str
    stock_item: str
    quantity: str
    unit: str
    rate: str
    amount: str
    source_godown: str
    destination_godown: str
    movement_type: str
    batch_name: str


def clean(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def field(block: str, tag: str) -> str:
    """
    Extract the first <TAG ...>value</TAG> from a block.
    Works with Tally's TYPE attributes and optional prefixes.
    """
    pattern = rf"<(?:[\w.-]+:)?{re.escape(tag)}(?:\s[^>]*)?>(.*?)</(?:[\w.-]+:)?{re.escape(tag)}>"
    match = re.search(pattern, block, flags=re.IGNORECASE | re.DOTALL)
    return clean(match.group(1)) if match else ""


def blocks(block: str, tag: str) -> list[str]:
    """
    Return complete nested blocks for a Tally .LIST tag.
    """
    pattern = rf"<(?:[\w.-]+:)?{re.escape(tag)}(?:\s[^>]*)?>(.*?)</(?:[\w.-]+:)?{re.escape(tag)}>"
    return [m.group(1) for m in re.finditer(pattern, block, flags=re.IGNORECASE | re.DOTALL)]


def number(value: str) -> str:
    value = clean(value).replace(",", "")
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value)
    if not match:
        return ""
    try:
        return str(Decimal(match.group(0)).normalize())
    except InvalidOperation:
        return match.group(0)


def unit_from_qty(value: str) -> str:
    value = clean(value)
    if not value:
        return ""
    match = re.search(r"/([A-Za-z][A-Za-z0-9 ._-]*)\.?\s*$", value)
    if match:
        return clean(match.group(1))
    match = re.search(r"\s+([A-Za-z][A-Za-z0-9 ._-]*)\.?\s*$", value)
    return clean(match.group(1)) if match else ""


def read_text(path: Path) -> str:
    # Decode while preserving all usable Tally text. Invalid XML characters
    # are irrelevant because this parser does not feed the text to an XML
    # parser.
    return path.read_bytes().decode("utf-8", errors="replace")


def find_voucher(text: str) -> str:
    matches = list(re.finditer(
        r"<(?:[\w.-]+:)?VOUCHER(?:\s[^>]*)?>(.*?)</(?:[\w.-]+:)?VOUCHER>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ))
    if not matches:
        raise ValueError("No VOUCHER block found")
    return matches[0].group(1)


def parse_xml(path: Path) -> list[StockMovement]:
    text = read_text(path)
    voucher = find_voucher(text)

    voucher_date = field(voucher, "DATE")
    voucher_number = field(voucher, "VOUCHERNUMBER")
    voucher_type = field(voucher, "VOUCHERTYPENAME") or "Stock Journal"
    guid = field(voucher, "GUID")
    master_id = field(voucher, "MASTERID")
    alter_id = field(voucher, "ALTERID")

    results: list[StockMovement] = []

    for list_tag, direction in (
        ("INVENTORYENTRIESIN.LIST", "IN"),
        ("INVENTORYENTRIESOUT.LIST", "OUT"),
    ):
        entry_blocks = blocks(voucher, list_tag)

        for entry in entry_blocks:
            stock_item = field(entry, "STOCKITEMNAME")
            if not stock_item:
                continue

            entry_qty = field(entry, "ACTUALQTY") or field(entry, "BILLEDQTY")
            entry_rate = field(entry, "RATE")
            entry_amount = field(entry, "AMOUNT")

            batch_blocks = blocks(entry, "BATCHALLOCATIONS.LIST")
            if not batch_blocks:
                batch_blocks = [""]

            for batch in batch_blocks:
                qty = field(batch, "ACTUALQTY") or field(batch, "BILLEDQTY") or entry_qty
                rate = field(batch, "RATE") or field(batch, "BATCHRATE") or entry_rate
                amount = field(batch, "AMOUNT") or entry_amount

                source = field(batch, "GODOWNNAME")
                destination = field(batch, "DESTINATIONGODOWNNAME")
                batch_name = field(batch, "BATCHNAME")

                if source and destination and source != destination:
                    movement_type = "TRANSFER"
                elif source and destination and source == destination:
                    movement_type = f"{direction}_SAME_GODOWN"
                else:
                    movement_type = direction

                results.append(
                    StockMovement(
                        voucher_date=voucher_date,
                        voucher_number=voucher_number,
                        voucher_type=voucher_type,
                        guid=guid,
                        master_id=master_id,
                        alter_id=alter_id,
                        stock_item=stock_item,
                        quantity=number(qty),
                        unit=unit_from_qty(qty),
                        rate=number(rate),
                        amount=number(amount),
                        source_godown=source,
                        destination_godown=destination,
                        movement_type=movement_type,
                        batch_name=batch_name,
                    )
                )

    return results


def parse_directory(directory: Path) -> list[StockMovement]:
    files = sorted(directory.glob("inventory_Stock_Journal_*_RAW.xml"))
    if not files:
        raise FileNotFoundError(
            f"No inventory_Stock_Journal_*_RAW.xml files found in {directory}"
        )

    all_rows: list[StockMovement] = []

    for path in files:
        rows = parse_xml(path)
        print(f"{path.name}: {len(rows)} movement rows")
        all_rows.extend(rows)

    return all_rows


def write_csv(rows: list[StockMovement], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(StockMovement.__dataclass_fields__.keys()),
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="tally_extracted_data")
    parser.add_argument(
        "--output",
        default="storage/stock_journal_movements.csv",
    )
    args = parser.parse_args()

    rows = parse_directory(Path(args.input))

    print()
    print(f"Total normalized movement rows: {len(rows)}")
    print(f"IN: {sum(r.movement_type == 'IN' for r in rows)}")
    print(f"OUT: {sum(r.movement_type == 'OUT' for r in rows)}")
    print(
        "IN same godown:",
        sum(r.movement_type == "IN_SAME_GODOWN" for r in rows),
    )
    print(
        "OUT same godown:",
        sum(r.movement_type == "OUT_SAME_GODOWN" for r in rows),
    )
    print(f"Transfers: {sum(r.movement_type == 'TRANSFER' for r in rows)}")

    print("\nFirst 10 normalized rows:")
    for i, row in enumerate(rows[:10], 1):
        print(
            f"{i}. {row.voucher_date} | #{row.voucher_number} | "
            f"{row.stock_item} | {row.quantity} {row.unit} | "
            f"rate={row.rate} | amount={row.amount} | "
            f"{row.source_godown} -> {row.destination_godown} | "
            f"{row.movement_type}"
        )

    write_csv(rows, Path(args.output))
    print(f"\nCSV written: {args.output}")


if __name__ == "__main__":
    main()
