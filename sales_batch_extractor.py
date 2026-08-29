#!/usr/bin/env python3
"""
sales_batch_extractor.py
=========================

Extracts per-voucher inventory movements for a given voucher type
(default: "Sales") from Tally, using the EXISTING verified general
voucher extraction as the source of truth for the voucher list.

Architecture
------------
    EXISTING VERIFIED GENERAL VOUCHERS  (general_vouchers_*_VERIFIED.xml)
                  |
           filter VOUCHERTYPENAME
                  |
              "Sales"  (e.g. 2,752 vouchers)
                  |
        Individual Tally voucher requests
        (TYPE=Voucher, BELONGSTO=Yes, FILTER=TargetVoucher,
         WALK=ALLINVENTORYENTRIES)
                  |
        Parse ONLY INVENTORYENTRIESIN.LIST / INVENTORYENTRIESOUT.LIST
        (never the flattened INVENTORYENTRIES.LIST)
                  |
        Raw XML checkpoints  (tally_extracted_data/<Type>_inventory/)
                  |
        Cumulative JSON + CSV (storage/)
                  |
        (downstream) PostgreSQL stock_movements

This script:
  * Does NOT issue a broad "Voucher Register" request to Tally.
  * Does NOT modify anything in Tally.
  * Is safe to interrupt and rerun at any point -- already-fetched
    raw XML is never re-requested, and already-parsed movements are
    never duplicated in the cumulative output.

Usage
-----
    python sales_batch_extractor.py --type Sales --limit 10
    python sales_batch_extractor.py --type Sales --limit 100
    python sales_batch_extractor.py --type Sales
    python sales_batch_extractor.py --type "SALES MILKS"

    # If auto-discovery can't find the verified voucher XML:
    python sales_batch_extractor.py --type Sales --source /path/to/general_vouchers_20260401_20260819_VERIFIED.xml

    # If Tally isn't on the default host/port:
    python sales_batch_extractor.py --type Sales --host localhost --port 9000
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

try:
    from tally_connector import send_to_tally
except ImportError as e:
    print("ERROR: tally_connector.py could not be imported.")
    print("Make sure this script is in the same project as the working Tally connector.")
    print(f"Details: {e}")
    sys.exit(1)


# --------------------------------------------------------------------------
# Configuration / constants
# --------------------------------------------------------------------------

DEFAULT_FROM_DATE = "20260401"
DEFAULT_TO_DATE = "20260819"

# Candidate filenames/patterns for the existing verified general voucher
# extraction. Auto-discovery searches for these (in order of preference)
# under a set of candidate directories.
VERIFIED_FILENAME_PATTERNS = [
    r"general_vouchers.*VERIFIED.*\.xml$",
    r".*VERIFIED.*\.xml$",
    r"general_vouchers.*\.xml$",
]

# Directories to search for the verified voucher XML, relative to the
# script's working directory (and its parents, up to a small depth).
CANDIDATE_SEARCH_DIRS = [
    ".",
    "storage",
    "tally_extracted_data",
    "tally_extracted_data/general",
    "data",
    "output",
    "outputs",
]

CHECKPOINT_ROOT = Path("tally_extracted_data")
STORAGE_ROOT = Path("storage")

MOVEMENT_FIELDS = [
    "date", "voucher_number", "voucher_type", "party_ledger", "guid",
    "master_id", "alter_id", "stock_item", "quantity", "unit",
    "billed_quantity", "rate", "amount", "source_godown",
    "destination_godown", "batch_name", "is_deemed_positive",
    "movement_type",
]


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------

@dataclass
class VoucherRef:
    """A single row from the existing verified general voucher extraction."""
    date: str
    voucher_number: str
    voucher_type: str
    party_ledger: str
    guid: str
    master_id: str
    alter_id: str


@dataclass
class Movement:
    date: str = ""
    voucher_number: str = ""
    voucher_type: str = ""
    party_ledger: str = ""
    guid: str = ""
    master_id: str = ""
    alter_id: str = ""
    stock_item: str = ""
    quantity: str = ""
    unit: str = ""
    billed_quantity: str = ""
    rate: str = ""
    amount: str = ""
    source_godown: str = ""
    destination_godown: str = ""
    batch_name: str = ""
    is_deemed_positive: str = ""
    movement_type: str = ""  # IN | OUT

    def key(self):
        # Uniqueness key used to prevent duplicate movements across reruns.
        return (
            self.guid, self.stock_item, self.movement_type,
            self.amount, self.quantity, self.batch_name,
        )


# --------------------------------------------------------------------------
# Step 1: locate + parse the existing verified general voucher extraction
# --------------------------------------------------------------------------

def discover_verified_source(explicit_path: Optional[str]) -> Path:
    """Find the verified general voucher XML file.

    If --source is given, use it directly. Otherwise search a set of
    candidate directories for a file matching the known naming pattern
    (general_vouchers_<from>_<to>_VERIFIED.xml or similar).
    """
    if explicit_path:
        p = Path(explicit_path)
        if not p.exists():
            raise FileNotFoundError(f"--source given but file does not exist: {p}")
        return p

    candidates = []
    search_dirs = []
    for d in CANDIDATE_SEARCH_DIRS:
        p = Path(d)
        search_dirs.append(p)
        search_dirs.append(Path("..") / d)

    seen_dirs = set()
    for d in search_dirs:
        try:
            resolved = d.resolve()
        except OSError:
            continue
        if resolved in seen_dirs or not resolved.is_dir():
            continue
        seen_dirs.add(resolved)
        for pattern in VERIFIED_FILENAME_PATTERNS:
            regex = re.compile(pattern, re.IGNORECASE)
            for f in resolved.glob("*.xml"):
                if regex.match(f.name):
                    candidates.append(f)

    # Also do a shallow recursive search from cwd as a fallback, in case
    # the file lives in a subfolder we didn't anticipate (e.g. tally_extracted_data/general/).
    if not candidates:
        cwd = Path(".").resolve()
        for pattern in VERIFIED_FILENAME_PATTERNS:
            regex = re.compile(pattern, re.IGNORECASE)
            for f in cwd.rglob("*.xml"):
                # Avoid descending into our own checkpoint output tree.
                if CHECKPOINT_ROOT.name in f.parts:
                    continue
                if regex.match(f.name):
                    candidates.append(f)
            if candidates:
                break

    if not candidates:
        raise FileNotFoundError(
            "Could not auto-locate the verified general voucher XML "
            "(looked for names matching 'general_vouchers*VERIFIED*.xml' "
            f"under {', '.join(str(d) for d in CANDIDATE_SEARCH_DIRS)} and subfolders of the "
            "current directory).\n"
            "Pass it explicitly with --source /path/to/general_vouchers_20260401_20260819_VERIFIED.xml"
        )

    # Prefer a filename that literally contains "VERIFIED", then the most
    # recently modified match.
    candidates.sort(key=lambda f: ("VERIFIED" not in f.name.upper(), -f.stat().st_mtime))
    return candidates[0]


_TAG_ALIASES = {
    "date": ["DATE", "VOUCHERDATE"],
    "voucher_number": ["VOUCHERNUMBER", "VCHNUMBER"],
    "voucher_type": ["VOUCHERTYPENAME", "VCHTYPE", "VOUCHERTYPE"],
    "party_ledger": ["PARTYLEDGERNAME", "PARTYNAME"],
    "guid": ["GUID"],
    "master_id": ["MASTERID"],
    "alter_id": ["ALTERID"],
}


def _findtext_any(elem: ET.Element, aliases) -> str:
    for tag in aliases:
        # direct child
        v = elem.findtext(tag)
        if v is not None:
            return v.strip()
        # case-insensitive / nested search fallback
        for child in elem.iter():
            if child.tag.upper() == tag.upper() and child.text:
                return child.text.strip()
    return ""


def load_verified_vouchers(source_path: Path) -> list:
    """Parse the verified general voucher XML into a flat list of VoucherRef.

    The parser is tolerant of exact tag nesting: it looks for any element
    that has a VOUCHERNUMBER-like child (i.e. looks like a voucher record)
    rather than assuming a fixed schema, since the exact export shape can
    vary.
    """
    try:
        tree = ET.parse(source_path)
    except ET.ParseError as e:
        raise ValueError(f"Failed to parse {source_path} as XML: {e}")

    root = tree.getroot()

    voucher_elems = []
    for elem in root.iter():
        # A "voucher-like" element has at least a voucher number and a
        # voucher type somewhere among its direct/near children.
        has_vch_no = any(elem.findtext(t) is not None for t in _TAG_ALIASES["voucher_number"])
        if has_vch_no:
            voucher_elems.append(elem)

    if not voucher_elems:
        raise ValueError(
            f"No voucher-like elements found in {source_path}. "
            "Expected elements containing a VOUCHERNUMBER field."
        )

    vouchers = []
    for elem in voucher_elems:
        vouchers.append(VoucherRef(
            date=_findtext_any(elem, _TAG_ALIASES["date"]),
            voucher_number=_findtext_any(elem, _TAG_ALIASES["voucher_number"]),
            voucher_type=_findtext_any(elem, _TAG_ALIASES["voucher_type"]),
            party_ledger=_findtext_any(elem, _TAG_ALIASES["party_ledger"]),
            guid=_findtext_any(elem, _TAG_ALIASES["guid"]),
            master_id=_findtext_any(elem, _TAG_ALIASES["master_id"]),
            alter_id=_findtext_any(elem, _TAG_ALIASES["alter_id"]),
        ))
    return vouchers


def filter_vouchers(vouchers, voucher_type: str, from_date: str, to_date: str):
    vt = voucher_type.strip().upper()
    out = []
    for v in vouchers:
        if v.voucher_type.strip().upper() != vt:
            continue
        if not v.date:
            continue
        d = v.date.strip().replace("-", "")
        if len(d) != 8 or not d.isdigit():
            # Can't verify date range confidently; skip rather than guess.
            continue
        if not (from_date <= d <= to_date):
            continue
        out.append(v)
    # Deterministic ordering so checkpoint index numbering is stable across reruns.
    out.sort(key=lambda v: (v.date, v.voucher_number, v.guid))
    return out


# --------------------------------------------------------------------------
# Step 2: individual Tally voucher request (proven-working shape)
# --------------------------------------------------------------------------

TDL_REQUEST_TEMPLATE = """<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>Voucher MasterID Fetch</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="Voucher MasterID Fetch">
      <TYPE>Voucher</TYPE>
      <BELONGSTO>Yes</BELONGSTO>
      <FILTER>TargetVoucher</FILTER>
      <FETCH>DATE</FETCH>
      <FETCH>VOUCHERNUMBER</FETCH>
      <FETCH>VOUCHERTYPENAME</FETCH>
      <FETCH>GUID</FETCH>
      <FETCH>MASTERID</FETCH>
      <FETCH>ALTERID</FETCH>
      <FETCH>PARTYLEDGERNAME</FETCH>
      <FETCH>ALLINVENTORYENTRIES.LIST</FETCH>
      <FETCH>INVENTORYENTRIES.LIST</FETCH>
      <FETCH>BATCHALLOCATIONS.LIST</FETCH>
     </COLLECTION>
     <SYSTEM TYPE="Formulae" NAME="TargetVoucher">
      $MASTERID = {master_id}
     </SYSTEM>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""


def _xml_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _strip_invalid_xml_chars(text: str) -> str:
    """Remove XML 1.0-forbidden control characters for parsing only.

    Tally can return otherwise-valid XML containing an illegal control
    character in a text field. The original response is preserved as the
    raw checkpoint; this sanitizer is used only for ElementTree parsing.
    """
    if not text:
        return text
    # Remove literal forbidden XML 1.0 control characters.
    text = re.sub(
        r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x84\x86-\x9F]",
        "",
        text,
    )

    # Tally may encode the same forbidden controls as numeric character
    # references (for example &#x1F; or &#31;). ElementTree rejects those
    # references even though the control character is not literally present
    # in the response text, so remove those references too.
    def _clean_numeric_char_ref(match: re.Match) -> str:
        raw = match.group(1)
        try:
            value = int(raw[1:], 16) if raw.lower().startswith("x") else int(raw)
        except ValueError:
            return match.group(0)

        if (
            value < 0
            or value > 0x10FFFF
            or (0 <= value <= 0x08)
            or value in (0x0B, 0x0C)
            or (0x0E <= value <= 0x1F)
            or (0x7F <= value <= 0x84)
            or (0x86 <= value <= 0x9F)
        ):
            return ""

        return match.group(0)

    text = re.sub(r"&#([xX][0-9A-Fa-f]+|[0-9]+);", _clean_numeric_char_ref, text)

    # Some Tally responses can contain prefixed XML names without declaring
    # the corresponding namespace (for example UDF:_UDF_687866862.LIST).
    # ElementTree raises "unbound prefix" for those tags. The prefix is not
    # needed for our voucher/inventory extraction, so remove it from element
    # names and prefixed attribute names before parsing.
    text = re.sub(
        r"(<\s*/?\s*)[A-Za-z_][A-Za-z0-9_.-]*:",
        r"\1",
        text,
    )
    text = re.sub(
        r"(\s)[A-Za-z_][A-Za-z0-9_.-]*:([A-Za-z_][A-Za-z0-9_.-]*\s*=)",
        r"\1\2",
        text,
    )

    return text


def build_request_xml(voucher: VoucherRef, from_date: str, to_date: str, company: str) -> str:
    # The verified voucher list contains the Tally MasterID.  MasterID is the
    # stable identifier we use to retrieve exactly one voucher.  The request
    # deliberately does not filter by voucher number because voucher numbers
    # are not globally unique across voucher types/dates.
    master_id = _xml_escape(voucher.master_id.strip())
    if not master_id or not master_id.isdigit():
        raise ValueError(
            f"Voucher {voucher.voucher_number} ({voucher.date}) has no valid MasterID: "
            f"{voucher.master_id!r}"
        )

    return TDL_REQUEST_TEMPLATE.format(
        company=_xml_escape(company),
        master_id=master_id,
    )


def fetch_voucher_xml(url: str, voucher: VoucherRef,
                       from_date: str, to_date: str, company: str,
                       timeout: int, retries: int) -> str:
    """Fetch exactly one voucher from Tally using its MasterID.

    This request shape was verified against the client's Tally instance with:
        MasterID 17577
        Date     20260401
        Type     Sales
        Number   1

    Tally returned the complete voucher, including INVENTORYENTRIES.LIST and
    nested BATCHALLOCATIONS.LIST.
    """
    body = build_request_xml(voucher, from_date, to_date, company)
    last_err = None

    for attempt in range(1, retries + 1):
        try:
            response = send_to_tally(body, timeout=timeout)

            if response is None:
                raise ValueError("Tally connector returned no response")

            text = response.decode("utf-8", errors="replace") if isinstance(response, bytes) else str(response)
            text = text.lstrip("\ufeff").strip()

            if not text:
                raise ValueError("Tally connector returned an empty response")

            if "<ENVELOPE" not in text.upper():
                snippet = re.sub(r"\s+", " ", text[:500])
                raise ValueError(f"Non-XML response from Tally: {snippet!r}")

            if "<ERRORMSG>" in text.upper() or "<RESPONSE>UNKNOWN REQUEST" in text.upper():
                snippet = re.sub(r"\s+", " ", text[:1000])
                raise ValueError(f"Tally returned an error: {snippet!r}")

            # Verify that Tally actually returned the requested voucher.
            # A successful HTTP/XML response with zero matching vouchers is
            # not a successful fetch.
            root = ET.fromstring(_strip_invalid_xml_chars(text))
            voucher_elems = [
                e for e in root.iter()
                if e.tag.upper() == "VOUCHER"
            ]
            if not voucher_elems:
                raise ValueError(
                    f"Tally returned no VOUCHER for MasterID {voucher.master_id}"
                )

            found_master_ids = {
                (_text(v, "MASTERID") or v.attrib.get("ID", "")).strip()
                for v in voucher_elems
            }
            if voucher.master_id.strip() not in found_master_ids:
                raise ValueError(
                    f"Tally returned VOUCHER data, but not requested MasterID "
                    f"{voucher.master_id}; found {sorted(found_master_ids)!r}"
                )

            return text

        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * attempt)

    raise RuntimeError(
        f"Failed to fetch voucher {voucher.voucher_number} "
        f"(MasterID {voucher.master_id}) after {retries} attempts: {last_err}"
    )


# --------------------------------------------------------------------------
# Step 3: parse directional inventory entries only
# --------------------------------------------------------------------------

def _text(elem: Optional[ET.Element], tag: str) -> str:
    if elem is None:
        return ""
    v = elem.findtext(tag)
    return v.strip() if v else ""


def _split_quantity(value: str):
    """Split Tally quantity text such as '-5.50 ltr' into value + unit."""
    value = (value or "").strip()
    m = re.match(r"^(-?(?:\d+(?:\.\d*)?|\.\d+))\s*(.*)$", value)
    if not m:
        return value, ""
    return m.group(1), m.group(2).strip()


def _numeric_sign(value: str) -> int:
    """Return -1/0/+1 for a Tally numeric string."""
    m = re.search(r"-?(?:\d+(?:\.\d*)?|\.\d+)", value or "")
    if not m:
        return 0
    try:
        number = float(m.group(0))
    except ValueError:
        return 0
    return -1 if number < 0 else (1 if number > 0 else 0)


def _movement_type(entry: ET.Element, quantity_text: str) -> str:
    """Determine IN/OUT from the actual Tally entry.

    For this client's Sales vouchers, quantities are negative and
    ISDEEMEDPOSITIVE is No for inventory going out.  We preserve the generic
    sign/deemed-positive fallback for other voucher types.
    """
    sign = _numeric_sign(quantity_text)
    deemed = _text(entry, "ISDEEMEDPOSITIVE").strip().lower()

    if sign < 0:
        return "OUT"
    if sign > 0:
        return "IN"
    if deemed == "no":
        return "OUT"
    if deemed == "yes":
        return "IN"
    return ""


def _make_movement(v_elem: ET.Element, entry: ET.Element, fallback: VoucherRef,
                   quantity_text: str = "", billed_quantity: str = "",
                   rate: str = "", amount: str = "", source_godown: str = "",
                   destination_godown: str = "", batch_name: str = "",
                   is_deemed_positive: str = "") -> Movement:
    date = _text(v_elem, "DATE") or fallback.date
    vch_no = _text(v_elem, "VOUCHERNUMBER") or fallback.voucher_number
    vch_type = _text(v_elem, "VOUCHERTYPENAME") or fallback.voucher_type
    party = _text(v_elem, "PARTYLEDGERNAME") or fallback.party_ledger
    guid = _text(v_elem, "GUID") or fallback.guid
    master_id = _text(v_elem, "MASTERID") or fallback.master_id
    alter_id = _text(v_elem, "ALTERID") or fallback.alter_id

    raw_quantity = quantity_text or _text(entry, "ACTUALQTY")
    quantity, unit = _split_quantity(raw_quantity)
    if not quantity:
        quantity, unit = _split_quantity(_text(entry, "BILLEDQTY"))

    if not billed_quantity:
        billed_quantity = _text(entry, "BILLEDQTY")
    if not rate:
        rate = _text(entry, "RATE")
    if not amount:
        amount = _text(entry, "AMOUNT")
    if not source_godown:
        source_godown = _text(entry, "GODOWNNAME")
    if not destination_godown:
        destination_godown = _text(entry, "DESTINATIONGODOWNNAME")
    if not batch_name:
        batch_name = _text(entry, "BATCHNAME")
    if not is_deemed_positive:
        is_deemed_positive = _text(entry, "ISDEEMEDPOSITIVE")

    return Movement(
        date=date,
        voucher_number=vch_no,
        voucher_type=vch_type,
        party_ledger=party,
        guid=guid,
        master_id=master_id,
        alter_id=alter_id,
        stock_item=_text(entry, "STOCKITEMNAME"),
        quantity=quantity.lstrip("+").lstrip("-"),
        unit=unit,
        billed_quantity=billed_quantity,
        rate=rate,
        amount=amount.lstrip("+").lstrip("-"),
        source_godown=source_godown,
        destination_godown=destination_godown,
        batch_name=batch_name,
        is_deemed_positive=is_deemed_positive,
        movement_type=_movement_type(entry, raw_quantity),
    )


def parse_movements_from_voucher_xml(xml_text: str, fallback: VoucherRef) -> list:
    """Parse inventory movements from a single fetched Tally voucher.

    The client's verified voucher response uses:
        INVENTORYENTRIES.LIST
            BATCHALLOCATIONS.LIST

    When batch allocations exist, they are authoritative for the detailed
    movement rows because they carry Godown, Batch, quantity, amount and rate.
    The parent INVENTORYENTRIES.LIST row is then NOT emitted separately, which
    prevents double counting.

    If no BATCHALLOCATIONS.LIST exists, the parent inventory entry itself is
    emitted.  Directional INVENTORYENTRIESIN/OUT lists are also supported for
    compatibility with other Tally voucher views.
    """
    try:
        root = ET.fromstring(_strip_invalid_xml_chars(xml_text))
    except ET.ParseError as e:
        raise ValueError(f"Malformed voucher XML: {e}")

    movements = []
    voucher_elems = [e for e in root.iter() if e.tag.upper() == "VOUCHER"]
    if not voucher_elems:
        voucher_elems = [root]

    for v_elem in voucher_elems:
        # First preference: the actual flattened inventory representation
        # returned by the client's proven MasterID collection request.
        flattened_entries = [
            e for e in v_elem.iter()
            if e.tag.upper() == "INVENTORYENTRIES.LIST"
        ]

        for entry in flattened_entries:
            batches = [
                e for e in entry
                if e.tag.upper() == "BATCHALLOCATIONS.LIST"
            ]

            if batches:
                for batch in batches:
                    # Batch fields override the parent values where present.
                    qty = _text(batch, "ACTUALQTY") or _text(entry, "ACTUALQTY")
                    billed = _text(batch, "BILLEDQTY") or _text(entry, "BILLEDQTY")
                    rate = _text(batch, "BATCHRATE") or _text(entry, "RATE")
                    amount = _text(batch, "AMOUNT") or _text(entry, "AMOUNT")
                    source = _text(batch, "GODOWNNAME") or _text(entry, "GODOWNNAME")
                    destination = (
                        _text(batch, "DESTINATIONGODOWNNAME")
                        or _text(entry, "DESTINATIONGODOWNNAME")
                    )
                    batch_name = _text(batch, "BATCHNAME")

                    movements.append(
                        _make_movement(
                            v_elem,
                            entry,
                            fallback,
                            quantity_text=qty,
                            billed_quantity=billed,
                            rate=rate,
                            amount=amount,
                            source_godown=source,
                            destination_godown=destination,
                            batch_name=batch_name,
                        )
                    )
            else:
                movements.append(_make_movement(v_elem, entry, fallback))

        # Compatibility path: some Tally views expose explicit directional
        # inventory lists instead of INVENTORYENTRIES.LIST.
        directional_found = False
        for direction_tag in ("INVENTORYENTRIESIN.LIST", "INVENTORYENTRIESOUT.LIST"):
            for entry in v_elem.iter():
                if entry.tag.upper() != direction_tag:
                    continue
                directional_found = True
                batches = [
                    e for e in entry
                    if e.tag.upper() == "BATCHALLOCATIONS.LIST"
                ]
                if batches:
                    for batch in batches:
                        movements.append(
                            _make_movement(
                                v_elem,
                                entry,
                                fallback,
                                quantity_text=_text(batch, "ACTUALQTY") or _text(entry, "ACTUALQTY"),
                                billed_quantity=_text(batch, "BILLEDQTY") or _text(entry, "BILLEDQTY"),
                                rate=_text(batch, "BATCHRATE") or _text(entry, "RATE"),
                                amount=_text(batch, "AMOUNT") or _text(entry, "AMOUNT"),
                                source_godown=_text(batch, "GODOWNNAME") or _text(entry, "GODOWNNAME"),
                                destination_godown=_text(batch, "DESTINATIONGODOWNNAME") or _text(entry, "DESTINATIONGODOWNNAME"),
                                batch_name=_text(batch, "BATCHNAME"),
                            )
                        )
                else:
                    m = _make_movement(v_elem, entry, fallback)
                    # Explicit list name is authoritative when quantity itself
                    # is zero/blank.
                    m.movement_type = "IN" if direction_tag == "INVENTORYENTRIESIN.LIST" else "OUT"
                    movements.append(m)

    # De-duplicate defensively.  This matters because some Tally exports can
    # expose the same inventory row through more than one representation.
    unique = {}
    for m in movements:
        unique[m.key()] = m

    return list(unique.values())


# --------------------------------------------------------------------------
# Step 4: checkpoint helpers
# --------------------------------------------------------------------------

def safe_type_folder_name(voucher_type: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", voucher_type.strip()).strip("_") or "UNKNOWN"


def checkpoint_path(voucher_type: str, voucher: VoucherRef, index: int) -> Path:
    type_slug = safe_type_folder_name(voucher_type)
    folder = CHECKPOINT_ROOT / f"{type_slug}_inventory"
    folder.mkdir(parents=True, exist_ok=True)
    guid_slug = re.sub(r"[^A-Za-z0-9\-]+", "", voucher.guid) or "NOGUID"
    date_slug = voucher.date.replace("-", "")
    fname = f"{type_slug}_{date_slug}_{index}_{guid_slug}.xml"
    return folder / fname


# --------------------------------------------------------------------------
# Step 5: cumulative output helpers
# --------------------------------------------------------------------------

def output_paths(voucher_type: str):
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    slug = safe_type_folder_name(voucher_type).lower()
    json_path = STORAGE_ROOT / f"{slug}_batch_inventory_movements.json"
    csv_path = STORAGE_ROOT / f"{slug}_batch_inventory_movements.csv"
    failed_path = STORAGE_ROOT / f"{slug}_batch_failed_vouchers.json"
    return json_path, csv_path, failed_path


def load_existing_movements(json_path: Path):
    if not json_path.exists():
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def write_movements(json_path: Path, csv_path: Path, movements: list):
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(movements, f, indent=2, ensure_ascii=False)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MOVEMENT_FIELDS)
        writer.writeheader()
        for m in movements:
            writer.writerow({k: m.get(k, "") for k in MOVEMENT_FIELDS})


def load_failed(failed_path: Path):
    if not failed_path.exists():
        return []
    try:
        with open(failed_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def write_failed(failed_path: Path, failed: list):
    with open(failed_path, "w", encoding="utf-8") as f:
        json.dump(failed, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Main extraction loop
# --------------------------------------------------------------------------

def run(args):
    print(f"[1/4] Locating verified general voucher extraction...")
    source_path = discover_verified_source(args.source)
    print(f"      Using source: {source_path}")

    all_vouchers = load_verified_vouchers(source_path)
    print(f"      Parsed {len(all_vouchers)} total vouchers from source.")

    vouchers = filter_vouchers(all_vouchers, args.type, args.from_date, args.to_date)
    print(f"[2/4] Filtered to voucher_type == '{args.type}' "
          f"and date {args.from_date} -> {args.to_date}: {len(vouchers)} vouchers.")

    if args.limit is not None:
        vouchers = vouchers[: args.limit]
        print(f"      --limit {args.limit} applied -> processing {len(vouchers)} vouchers this run.")

    if not vouchers:
        print("No matching vouchers found. Nothing to do.")
        return

    json_path, csv_path, failed_path = output_paths(args.type)
    existing_movements = load_existing_movements(json_path)
    movements_by_key = {}
    guids_already_recorded = set()
    for m in existing_movements:
        movements_by_key[tuple(m.get(k, "") for k in
                                ("guid", "stock_item", "movement_type", "amount", "quantity", "batch_name"))] = m
        guids_already_recorded.add(m.get("guid", ""))

    ordered_movements = list(existing_movements)  # preserve prior order, append new
    failed = load_failed(failed_path)
    failed_guids = {f.get("guid") for f in failed}

    url = f"http://{args.host}:{args.port}"

    print(f"[3/4] Fetching individual voucher inventory detail through tally_connector ({url}) ...")
    fetched, skipped_cached, skipped_done, errors = 0, 0, 0, 0

    for idx, voucher in enumerate(vouchers, start=1):
        ckpt = checkpoint_path(args.type, voucher, idx)

        already_recorded = voucher.guid and voucher.guid in guids_already_recorded

        try:
            if ckpt.exists():
                xml_text = ckpt.read_text(encoding="utf-8")
                skipped_cached += 1
            else:
                xml_text = fetch_voucher_xml(
                    url, voucher, args.from_date, args.to_date,
                    args.company, args.timeout, args.retries,
                )
                ckpt.write_text(xml_text, encoding="utf-8")
                fetched += 1

            if already_recorded:
                skipped_done += 1
            else:
                new_movements = parse_movements_from_voucher_xml(xml_text, voucher)
                added_any = False
                for m in new_movements:
                    k = m.key()
                    if k in movements_by_key:
                        continue
                    d = asdict(m)
                    movements_by_key[k] = d
                    ordered_movements.append(d)
                    added_any = True
                if voucher.guid:
                    guids_already_recorded.add(voucher.guid)
                # Checkpoint cumulative output after every successful voucher.
                if added_any or not new_movements:
                    write_movements(json_path, csv_path, ordered_movements)

            # A voucher that previously failed but now succeeds should be cleared.
            if voucher.guid in failed_guids:
                failed = [f for f in failed if f.get("guid") != voucher.guid]
                failed_guids.discard(voucher.guid)
                write_failed(failed_path, failed)

        except Exception as e:  # noqa: BLE001 - record and continue, never abort the batch
            errors += 1
            entry = {
                "index": idx,
                "date": voucher.date,
                "voucher_number": voucher.voucher_number,
                "voucher_type": voucher.voucher_type,
                "guid": voucher.guid,
                "master_id": voucher.master_id,
                "alter_id": voucher.alter_id,
                "error": str(e),
            }
            if voucher.guid not in failed_guids:
                failed.append(entry)
                failed_guids.add(voucher.guid)
            else:
                for f in failed:
                    if f.get("guid") == voucher.guid:
                        f["error"] = str(e)
                        break
            write_failed(failed_path, failed)
            print(f"      [FAIL] {idx}/{len(vouchers)} {voucher.voucher_number} ({voucher.date}): {e}")
            continue

        if idx % 25 == 0 or idx == len(vouchers):
            print(f"      progress: {idx}/{len(vouchers)} "
                  f"(fetched={fetched}, cached={skipped_cached}, already_done={skipped_done}, errors={errors})")

    print(f"[4/4] Done.")
    print(f"      Newly fetched from Tally : {fetched}")
    print(f"      Loaded from checkpoint   : {skipped_cached}")
    print(f"      Already in cumulative out: {skipped_done}")
    print(f"      Failed                   : {errors}")
    print(f"      Cumulative movements     : {len(ordered_movements)}")
    print(f"      JSON  -> {json_path}")
    print(f"      CSV   -> {csv_path}")
    print(f"      Failed log -> {failed_path}")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Extract per-voucher inventory movements for a given voucher type, "
                    "using the existing verified general voucher extraction as the voucher list."
    )
    p.add_argument("--type", default="Sales",
                    help="Voucher type to filter and extract, e.g. 'Sales' or 'SALES MILKS'. Default: Sales")
    p.add_argument("--limit", type=int, default=None,
                    help="Only process the first N matching vouchers (for staged testing). Default: all.")
    p.add_argument("--from", "--from-date", dest="from_date", default=DEFAULT_FROM_DATE, help=f"Start date YYYYMMDD. Default {DEFAULT_FROM_DATE}")
    p.add_argument("--to", "--to-date", dest="to_date", default=DEFAULT_TO_DATE, help=f"End date YYYYMMDD. Default {DEFAULT_TO_DATE}")
    p.add_argument("--source", default=None,
                    help="Explicit path to the verified general voucher XML "
                         "(general_vouchers_<from>_<to>_VERIFIED.xml). If omitted, auto-discovered.")
    p.add_argument("--host", default="localhost", help="Tally ODBC/HTTP host. Default: localhost")
    p.add_argument("--port", default="9000", help="Tally HTTP port. Default: 9000")
    p.add_argument("--company", default="SHRI JAIN BANDHU GRAMODYOG - (from 1-Apr-2026)", help="SVCURRENTCOMPANY value.")
    p.add_argument("--timeout", type=int, default=30, help="Per-request timeout in seconds. Default: 30")
    p.add_argument("--retries", type=int, default=3, help="Retries per voucher before marking it failed. Default: 3")
    return p.parse_args(argv)


if __name__ == "__main__":
    run(parse_args())
