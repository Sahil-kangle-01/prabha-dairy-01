"""
api/live_stock.py

Requirement #4 (live godown stock lookup) and the #6 constraint (rate/
stock must always come fresh from Tally, never cached).

This is the ONLY module in the API that talks to Tally directly. Every
other route reads Postgres. Keeping that boundary in its own file means
it's obvious at a glance which endpoints are "always live" vs "reads our
synced data" -- see the design note in master_sync_service.py for why
that separation matters here.

IMPLEMENTATION NOTE: Uses a Collection query filtered by stock item name,
with BATCHALLOCATIONS.LIST fetch to get godown-wise breakdown. Each
BATCHALLOCATIONS.LIST element contains GODOWNNAME and OPENINGBALANCE
(which is the current balance, not historical — Tally uses "opening" to
mean "current state at batch level").
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

from tally_connector import TALLY_URL, safe_parse, send_to_tally


@dataclass
class GodownStockRow:
    godown: str
    closing_balance: float | None
    closing_rate: float | None


def _to_number(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    if not text or text in ("0", "0.00"):
        return None
    # Strip trailing unit like " ltr" or " Nos" or " KG"
    text = text.split()[0].strip()
    # Strip /ltr suffix in rate fields
    text = text.split("/")[0].strip()
    try:
        return float(text)
    except ValueError:
        return None


def get_live_godown_stock(item_name: str) -> list[GodownStockRow]:
    """
    Queries Tally directly (no cache, no DB read) for the current
    godown-wise closing balance and rate of one stock item.

    Uses a Collection query with BATCHALLOCATIONS.LIST fetch, which returns
    godown-wise breakdown. This is confirmed working with the client's Tally.

    Raises RuntimeError if Tally is unreachable or returns an HTTP error.
    """
    safe_item = item_name.replace('"', '\\"')

    xml_request = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>StockItemWithGodownBalance</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <SYSTEM TYPE="Formulae" NAME="ItemFilter">$Name = "{safe_item}"</SYSTEM>
                    <COLLECTION NAME="StockItemWithGodownBalance" ISINITIALIZE="Yes">
                        <TYPE>StockItem</TYPE>
                        <FILTER>ItemFilter</FILTER>
                        <FETCH>Name</FETCH>
                        <FETCH>ClosingBalance</FETCH>
                        <FETCH>ClosingRate</FETCH>
                        <FETCH>BATCHALLOCATIONS.LIST</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>"""

    response_text = send_to_tally(xml_request)
    if response_text is None:
        raise RuntimeError(f"Could not reach Tally at {TALLY_URL} for live stock lookup")

    root = safe_parse(response_text)
    if root is None:
        raise RuntimeError("Tally response for live stock lookup could not be parsed")

    rows: list[GodownStockRow] = []

    # Response structure:
    # <STOCKITEM NAME="...">
    #   <CLOSINGRATE>rate/ltr</CLOSINGRATE>
    #   <BATCHALLOCATIONS.LIST>
    #     <GODOWNNAME>godown name</GODOWNNAME>
    #     <OPENINGBALANCE>qty ltr</OPENINGBALANCE>
    #     <OPENINGRATE>rate/ltr</OPENINGRATE>
    #   </BATCHALLOCATIONS.LIST>
    #   ...
    # </STOCKITEM>

    for stock_item in root.iter("STOCKITEM"):
        item_name_attr = stock_item.attrib.get("NAME", "").strip()
        if item_name_attr.upper() != item_name.upper():
            continue

        # Get the overall closing rate from the stock item (fallback if godown doesn't have one)
        overall_rate_el = stock_item.find("CLOSINGRATE")
        overall_rate = _to_number(overall_rate_el.text if overall_rate_el is not None else None)

        # Parse each BATCHALLOCATIONS.LIST for godown breakdown
        for batch in stock_item.iter("BATCHALLOCATIONS.LIST"):
            godown_el = batch.find("GODOWNNAME")
            balance_el = batch.find("OPENINGBALANCE")  # "OPENINGBALANCE" is current balance in batch context
            rate_el = batch.find("OPENINGRATE")

            if godown_el is None or balance_el is None:
                continue

            godown_name = (godown_el.text or "").strip()
            balance_val = _to_number(balance_el.text)
            # Use godown-specific rate if present, otherwise fall back to item's overall rate
            rate_val = _to_number(rate_el.text if rate_el is not None else None)
            if rate_val is None:
                rate_val = overall_rate

            # Only include godowns with non-zero balance
            # Note: negative balance means stock on hand (Tally convention for inventory)
            if balance_val and balance_val != 0:
                rows.append(
                    GodownStockRow(
                        godown=godown_name,
                        closing_balance=abs(balance_val),  # Convert to positive for display
                        closing_rate=rate_val,
                    )
                )

    return rows
