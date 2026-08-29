import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

TALLY_URL = "http://localhost:9000"
TIMEOUT = 15
OUTPUT_DIR = "tally_extracted_data"
MASTER_DIR = os.path.join(OUTPUT_DIR, "masters")

os.makedirs(MASTER_DIR, exist_ok=True)


# ============================================================
# XML SANITIZATION
# Reuses the same protections already required by this Tally
# instance: illegal XML chars, stray &, and undeclared UDF:
# prefixes.
# ============================================================

def _is_legal_xml_codepoint(codepoint):
    return (
        codepoint in (0x9, 0xA, 0xD)
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def _strip_illegal_numeric_ref(match):
    try:
        codepoint = int(match.group(1))
    except ValueError:
        return match.group(0)
    return match.group(0) if _is_legal_xml_codepoint(codepoint) else ""


def _strip_illegal_hex_ref(match):
    try:
        codepoint = int(match.group(1), 16)
    except ValueError:
        return match.group(0)
    return match.group(0) if _is_legal_xml_codepoint(codepoint) else ""


def clean_xml(text):
    if text is None:
        return text

    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)
    text = re.sub(r"&#(\d+);", _strip_illegal_numeric_ref, text)
    text = re.sub(r"&#x([0-9a-fA-F]+);", _strip_illegal_hex_ref, text)
    text = re.sub(
        r"&(?!amp;|lt;|gt;|apos;|quot;|#\d+;|#x[0-9a-fA-F]+;)",
        "&amp;",
        text,
    )

    # Tally may emit undeclared UDF namespace prefixes.
    text = re.sub(r"</?UDF:", lambda m: m.group(0).replace(":", "_"), text)

    return text


def safe_parse(xml_text):
    try:
        return ET.fromstring(clean_xml(xml_text))
    except ET.ParseError as exc:
        print(f"ERROR: XML parse failed: {exc}")
        return None


def send_to_tally(xml_request):
    try:
        response = requests.post(
            TALLY_URL,
            data=xml_request.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8"},
            timeout=TIMEOUT,
        )

        print(f"HTTP Status: {response.status_code}")

        if response.status_code != 200:
            print("ERROR: Tally returned an HTTP error.")
            print(response.text[:2000])
            return None

        root = safe_parse(response.text)
        if root is not None:
            errors = []
            for elem in root.iter():
                if elem.tag.upper() == "RESPONSE" and elem.text:
                    text = elem.text.strip()
                    if text:
                        errors.append(text)

            for err in errors:
                if "error" in err.lower() or "unknown" in err.lower():
                    print(f"ERROR: Tally response: {err}")
                    return None

        return response.text

    except requests.RequestException as exc:
        print(f"ERROR: Tally connection failed: {exc}")
        return None


def text_of(elem, tag):
    child = elem.find(tag)
    return (child.text or "").strip() if child is not None else ""


def write_json(filename, data):
    path = os.path.join(MASTER_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def save_raw(filename, xml_text):
    path = os.path.join(MASTER_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml_text)
    return path


# ============================================================
# GENERIC MASTER EXTRACTION
# ============================================================

def _child_text(elem, tag):
    """
    Read a Tally-exported field that comes back as a CHILD ELEMENT
    (e.g. <PARENT TYPE="String">Foo</PARENT>), not an XML attribute.
    This is the standard shape for NATIVEMETHOD/FETCH-requested fields
    in Tally's XML export -- only NAME (and RESERVEDNAME) come back as
    attributes on the parent tag itself. Confirmed against real
    STOCKITEM and GODOWN exports from this Tally instance.

    Also strips Tally's internal hierarchy-depth control characters
    (e.g. PARENT sometimes comes back as "\x04 Primary") which are
    formatting artifacts, not part of the actual name.
    """
    child = elem.find(tag)
    if child is None or not child.text:
        return ""
    text = child.text.strip()
    text = "".join(ch for ch in text if ch.isprintable())
    return text.strip()


def extract_stock_items():
    print("\n" + "=" * 60)
    print("MASTER DATA: STOCK ITEMS")
    print("=" * 60)

    xml_request = """
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Stock Item Collection</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Stock Item Collection" ISINITIALIZE="Yes">
                        <TYPE>StockItem</TYPE>
                        <NATIVEMETHOD>Name</NATIVEMETHOD>
                        <NATIVEMETHOD>Parent</NATIVEMETHOD>
                        <NATIVEMETHOD>BaseUnits</NATIVEMETHOD>
                        <NATIVEMETHOD>OpeningBalance</NATIVEMETHOD>
                        <NATIVEMETHOD>ClosingBalance</NATIVEMETHOD>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""

    raw = send_to_tally(xml_request)
    if not raw:
        return []

    save_raw("stock_items_raw.xml", raw)
    root = safe_parse(raw)
    if root is None:
        return []

    rows = []
    for elem in root.iter():
        if elem.tag.upper() != "STOCKITEM":
            continue

        # NAME/RESERVEDNAME are attributes; everything else Tally
        # returns as a child element -- see _child_text() docstring.
        rows.append({
            "name": elem.attrib.get("NAME", "").strip(),
            "parent": _child_text(elem, "PARENT"),
            "base_units": _child_text(elem, "BASEUNITS"),
            "opening_balance": _child_text(elem, "OPENINGBALANCE"),
            "closing_balance": _child_text(elem, "CLOSINGBALANCE"),
        })

    rows.sort(key=lambda x: x["name"].lower())
    write_json("stock_items.json", rows)

    print(f"Stock items found: {len(rows)}")
    non_blank_parent = sum(1 for r in rows if r["parent"])
    print(f"  with parent populated  : {non_blank_parent}/{len(rows)}")
    return rows


def extract_ledgers():
    print("\n" + "=" * 60)
    print("MASTER DATA: LEDGERS / PARTIES")
    print("=" * 60)

    xml_request = """
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Ledger Collection</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Ledger Collection" ISINITIALIZE="Yes">
                        <TYPE>Ledger</TYPE>
                        <FETCH>NAME</FETCH>
                        <FETCH>PARENT</FETCH>
                        <FETCH>GUID</FETCH>
                        <FETCH>OPENINGBALANCE</FETCH>
                        <FETCH>CLOSINGBALANCE</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""

    raw = send_to_tally(xml_request)
    if not raw:
        return []

    save_raw("ledgers_raw.xml", raw)
    root = safe_parse(raw)
    if root is None:
        return []

    rows = []
    for elem in root.iter():
        if elem.tag.upper() != "LEDGER":
            continue

        # NAME/RESERVEDNAME are attributes; GUID/PARENT/balances are
        # FETCH-requested child elements -- see _child_text() docstring.
        rows.append({
            "name": elem.attrib.get("NAME", "").strip(),
            "parent": _child_text(elem, "PARENT"),
            "guid": _child_text(elem, "GUID"),
            "opening_balance": _child_text(elem, "OPENINGBALANCE"),
            "closing_balance": _child_text(elem, "CLOSINGBALANCE"),
        })

    rows.sort(key=lambda x: x["name"].lower())
    write_json("ledgers.json", rows)

    print(f"Ledgers found: {len(rows)}")
    non_blank_guid = sum(1 for r in rows if r["guid"])
    print(f"  with guid populated    : {non_blank_guid}/{len(rows)}")
    return rows


def extract_godowns():
    print("\n" + "=" * 60)
    print("MASTER DATA: GODOWNS")
    print("=" * 60)

    xml_request = """
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Godown Collection</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Godown Collection" ISINITIALIZE="Yes">
                        <TYPE>Godown</TYPE>
                        <NATIVEMETHOD>Name</NATIVEMETHOD>
                        <NATIVEMETHOD>Parent</NATIVEMETHOD>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""

    raw = send_to_tally(xml_request)
    if not raw:
        return []

    save_raw("godowns_raw.xml", raw)
    root = safe_parse(raw)
    if root is None:
        return []

    rows = []
    for elem in root.iter():
        if elem.tag.upper() != "GODOWN":
            continue

        rows.append({
            "name": elem.attrib.get("NAME", "").strip(),
            "parent": _child_text(elem, "PARENT"),
        })

    rows.sort(key=lambda x: x["name"].lower())
    write_json("godowns.json", rows)

    print(f"Godowns found: {len(rows)}")
    non_blank_parent = sum(1 for r in rows if r["parent"])
    print(f"  with parent populated  : {non_blank_parent}/{len(rows)}")
    return rows


# ============================================================
# DERIVED UNIT MASTER
# Units are not fetched as a separate Tally collection yet.
# We derive the units actually referenced by StockItem master
# records. This avoids assuming an unverified Tally collection.
# ============================================================

def build_units(stock_items):
    print("\n" + "=" * 60)
    print("MASTER DATA: UNITS (DERIVED)")
    print("=" * 60)

    units = sorted({
        item["base_units"]
        for item in stock_items
        if item.get("base_units")
    }, key=str.lower)

    rows = [{"name": unit} for unit in units]
    write_json("units.json", rows)

    print(f"Units derived from stock items: {len(rows)}")
    return rows


def build_master_index(stock_items, ledgers, godowns, units):
    print("\n" + "=" * 60)
    print("BUILDING MASTER INDEX")
    print("=" * 60)

    index = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {
            "stock_items": len(stock_items),
            "ledgers": len(ledgers),
            "godowns": len(godowns),
            "units": len(units),
        },
        "stock_items_by_name": {
            x["name"]: x for x in stock_items if x["name"]
        },
        "ledgers_by_name": {
            x["name"]: x for x in ledgers if x["name"]
        },
        "godowns_by_name": {
            x["name"]: x for x in godowns if x["name"]
        },
        "units_by_name": {
            x["name"]: x for x in units if x["name"]
        },
    }

    write_json("master_index.json", index)
    print("Master index created.")
    return index


def main():
    print("=" * 60)
    print("TALLY MASTER DATA EXTRACTOR")
    print("=" * 60)
    print(f"Tally URL : {TALLY_URL}")
    print(f"Output    : {MASTER_DIR}")
    print("Mode      : READ-ONLY")
    print("=" * 60)

    stock_items = extract_stock_items()
    ledgers = extract_ledgers()
    godowns = extract_godowns()
    units = build_units(stock_items)

    build_master_index(stock_items, ledgers, godowns, units)

    print("\n" + "=" * 60)
    print("MASTER DATA EXTRACTION COMPLETE")
    print("=" * 60)
    print(f"Stock Items : {len(stock_items)}")
    print(f"Ledgers     : {len(ledgers)}")
    print(f"Godowns     : {len(godowns)}")
    print(f"Units       : {len(units)}")
    print(f"Output      : {MASTER_DIR}")
    print("\nNext layer: normalize party/customer classification and")
    print("add remaining verified master fields before database/API work.")


if __name__ == "__main__":
    main()
