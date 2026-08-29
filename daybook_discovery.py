"""
Live Tally voucher discovery for Sync Now.

Purpose:
- Discover vouchers available in Tally for a date window.
- Returns lightweight voucher identities only.
- Does NOT write to the database.
- Does NOT fetch full voucher details.

Usage:
    python daybook_discovery.py 20260401 20260819

Default Tally URL:
    http://localhost:9000

--------------------------------------------------------------------
WHY THIS NO LONGER USES TYPE=Data / ID=DayBook
--------------------------------------------------------------------
The original version of this script requested Tally's canned Day Book
report:

    <TYPE>Data</TYPE>
    <ID>DayBook</ID>

with <SVFROMDATE>/<SVTODATE> in STATICVARIABLES. That looks correct per
Tally's docs, but in practice this report export ignores the requested
date range entirely and just returns vouchers for Tally's *current*
system date, no matter what SVFROMDATE/SVTODATE say. Confirmed by
sending three different ranges (2026-04-01..2026-08-19,
2026-08-19..2026-08-19, and 2026-08-20..2026-08-22 — a range that
starts *after* Tally's current date) and getting back the exact same
30 vouchers, all dated 2026-08-19, every time.

The fix (matching the working extractor in tally_connector.py, which
pulled 18,404 real vouchers spanning April-August) is to request the
underlying Voucher Collection via a custom TDL COLLECTION instead of
the canned report:

    <TYPE>Collection</TYPE>
    <ID>Voucher Collection</ID>
    ...
    <COLLECTION NAME="Voucher Collection" ISINITIALIZE="Yes">
        <TYPE>Voucher</TYPE>
        <FETCH>...</FETCH>
    </COLLECTION>

Even this collection's SVFROMDATE/SVTODATE scoping has proven
unreliable on its own (see tally_connector.py comments), so exactly
like that script, this one does NOT trust Tally's server-side date
filter. It reads every returned voucher's own <DATE> and filters in
Python. That double-check is cheap and it's the only way to be sure
the date range was actually honored.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests

TALLY_URL = "http://localhost:9000"
RAW_DIR = Path("tally_extracted_data") / "daybook"


def _strip_invalid_xml_chars(text: str) -> str:
    """Remove XML 1.0-invalid control characters and invalid numeric refs."""
    text = re.sub(
        r"&#(?:0*([0-8]|11|12|14|15|1[6-9]|2[0-9]|30|31));",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return "".join(
        ch for ch in text
        if ch in "\t\n\r" or ord(ch) >= 32
    )


def _strip_undeclared_prefixes(text: str) -> str:
    """
    Tally can emit UDF:... tags without declaring the UDF namespace.
    Remove the prefix from tags/attributes so ElementTree can parse safely.
    """
    text = re.sub(r"<(/?)UDF:", r"<\1", text)
    text = re.sub(r"\s+UDF:[A-Za-z0-9_.:-]+=", " ", text)
    return text


def _safe_parse(xml_text: str) -> ET.Element:
    cleaned = _strip_invalid_xml_chars(xml_text)
    cleaned = _strip_undeclared_prefixes(cleaned)
    return ET.fromstring(cleaned)


def _text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _date_key(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def build_daybook_request(from_date: str, to_date: str) -> str:
    """
    Build a Voucher Collection request (NOT the DayBook report export —
    see module docstring for why). SVFROMDATE/SVTODATE are included
    because they're the documented, standard way to scope a Voucher
    collection, but the actual filtering is re-verified in Python
    against each voucher's own <DATE> — do not rely on Tally to have
    honored this range.
    """
    return f"""<ENVELOPE>
<HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>Voucher Collection</ID>
</HEADER>
<BODY>
    <DESC>
        <STATICVARIABLES>
            <SVFROMDATE>{from_date}</SVFROMDATE>
            <SVTODATE>{to_date}</SVTODATE>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        </STATICVARIABLES>
        <TDL>
            <TDLMESSAGE>
                <COLLECTION NAME="Voucher Collection" ISINITIALIZE="Yes">
                    <TYPE>Voucher</TYPE>
                    <FETCH>DATE</FETCH>
                    <FETCH>VOUCHERNUMBER</FETCH>
                    <FETCH>VOUCHERTYPENAME</FETCH>
                    <FETCH>PARTYLEDGERNAME</FETCH>
                    <FETCH>GUID</FETCH>
                    <FETCH>MASTERID</FETCH>
                    <FETCH>ALTERID</FETCH>
                </COLLECTION>
            </TDLMESSAGE>
        </TDL>
    </DESC>
</BODY>
</ENVELOPE>"""


def discover_daybook_vouchers(
    from_date: str,
    to_date: str,
    tally_url: str = TALLY_URL,
    timeout: int = 300,
    save_raw: bool = True,
) -> list[dict[str, Any]]:
    """
    Discover voucher identities from Tally for a date window.

    Dates must be YYYYMMDD.
    The actual DATE field on each returned VOUCHER is checked in Python —
    Tally's own SVFROMDATE/SVTODATE scoping is not trusted (see module
    docstring).
    """
    if not re.fullmatch(r"\d{8}", from_date):
        raise ValueError(f"Invalid from_date: {from_date}")
    if not re.fullmatch(r"\d{8}", to_date):
        raise ValueError(f"Invalid to_date: {to_date}")
    if from_date > to_date:
        raise ValueError("from_date cannot be after to_date")

    body = build_daybook_request(from_date, to_date)

    response = requests.post(
        tally_url,
        data=body.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8"},
        timeout=timeout,
    )
    response.raise_for_status()

    raw = response.text

    if save_raw:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / f"daybook_{from_date}_{to_date}_RAW.xml").write_text(
            raw, encoding="utf-8"
        )

    root = _safe_parse(raw)

    results: list[dict[str, Any]] = []

    for voucher in root.findall(".//VOUCHER"):
        date = _date_key(_text(voucher, "DATE"))

        # Tally may return vouchers outside the requested period —
        # this is the real, load-bearing filter, not a belt-and-braces
        # extra. Keep only what's actually in range.
        if not date or not (from_date <= date <= to_date):
            continue

        item = {
            "date": date,
            "voucher_number": _text(voucher, "VOUCHERNUMBER"),
            "voucher_type": _text(voucher, "VOUCHERTYPENAME"),
            "guid": _text(voucher, "GUID"),
            "master_id": _text(voucher, "MASTERID"),
            "alter_id": _text(voucher, "ALTERID"),
            "party_ledger": _text(voucher, "PARTYLEDGERNAME"),
        }

        # Ignore non-voucher/empty report nodes.
        if not item["guid"] and not item["master_id"] and not item["voucher_number"]:
            continue

        results.append(item)

    # De-duplicate by GUID, falling back to MasterID.
    unique: dict[str, dict[str, Any]] = {}
    for item in results:
        key = item["guid"] or f"MASTERID:{item['master_id']}"
        unique[key] = item

    return list(unique.values())


def discover_relevant_vouchers(
    from_date: str,
    to_date: str,
    tally_url: str = TALLY_URL,
    timeout: int = 300,
) -> list[dict[str, Any]]:
    """Return only voucher types currently handled by the sync reconstruction."""
    relevant = {"Sales", "Purchase Milk", "Stock Journal"}

    rows = discover_daybook_vouchers(
        from_date, to_date, tally_url=tally_url, timeout=timeout
    )

    return [row for row in rows if row["voucher_type"] in relevant]


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python daybook_discovery.py YYYYMMDD YYYYMMDD")
        return 2

    from_date, to_date = sys.argv[1], sys.argv[2]

    print("TALLY VOUCHER DISCOVERY (Voucher Collection)")
    print("=" * 50)
    print(f"Window : {from_date} -> {to_date}")
    print(f"Tally  : {TALLY_URL}")
    print()

    try:
        rows = discover_daybook_vouchers(from_date, to_date)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Vouchers discovered : {len(rows)}")

    type_counts: dict[str, int] = {}
    for row in rows:
        type_counts[row["voucher_type"]] = type_counts.get(row["voucher_type"], 0) + 1

    for voucher_type, count in sorted(type_counts.items()):
        print(f"  {voucher_type:<25} {count}")

    print()
    print("First 10 voucher identities:")
    for row in rows[:10]:
        print(row)

    print()
    print("RESULT: VOUCHER DISCOVERY WORKS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())