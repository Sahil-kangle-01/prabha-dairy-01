"""Parser for Prabha Dairy / R3 Purchase Milk Tally XML.

Read-only parser: consumes XML returned by Tally and produces structured records.
It never writes to Tally.
"""
import re
import xml.etree.ElementTree as ET


def clean_xml(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text or "")
    text = re.sub(r"&#(\d+);", lambda m: m.group(0) if int(m.group(1)) in (9,10,13) or int(m.group(1)) >= 32 else "", text)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: m.group(0) if int(m.group(1),16) in (9,10,13) or int(m.group(1),16) >= 32 else "", text)
    text = re.sub(r"&(?!amp;|lt;|gt;|apos;|quot;|#\d+;|#x[0-9a-fA-F]+;)", "&amp;", text)
    text = re.sub(r"</?UDF:", lambda m: m.group(0).replace(":", "_"), text)
    return text


def parse_xml(xml_text: str):
    return ET.fromstring(clean_xml(xml_text))


def _field_id(tag: str):
    m = re.search(r"_UDF_(\d+)", tag or "")
    return m.group(1) if m else None


def _text(parent, *names):
    for name in names:
        node = parent.find(name)
        if node is not None and node.text is not None:
            value = node.text.strip()
            if value:
                return value
    return ""


def _udf_values(voucher):
    values = {}
    for node in voucher.iter():
        fid = _field_id(node.tag)
        if not fid:
            continue
        # Only capture leaf values. The .LIST wrapper contains whitespace;
        # its child contains the actual R3 value.
        if node.tag.endswith(".LIST"):
            continue
        value = (node.text or "").strip()
        if value:
            values.setdefault(fid, []).append(value)
    return values


def _first(values, *ids):
    for fid in ids:
        vals = values.get(fid, [])
        if vals:
            return vals[0]
    return ""


def parse_voucher(voucher):
    u = _udf_values(voucher)
    return {
        "date": _text(voucher, "DATE"),
        "voucher_number": _text(voucher, "VOUCHERNUMBER"),
        "voucher_type": _text(voucher, "VOUCHERTYPENAME") or voucher.attrib.get("VCHTYPE", ""),
        "party_ledger": _text(voucher, "PARTYLEDGERNAME", "PARTYNAME", "LEDGERNAME", "BASICBUYERNAME"),
        "guid": _text(voucher, "GUID"),
        "master_id": _text(voucher, "MASTERID"),
        "alter_id": _text(voucher, "ALTERID"),
        "litres": _first(u, "687866876", "687866861", "687872869"),
        "milk_type": _first(u, "788530154"),
        "shift": _first(u, "788530155"),
        "degree": _first(u, "687866864", "687872868"),
        "fat": _first(u, "687866863", "687872870"),
        "snf": _first(u, "687866862"),
        "actual_rate": _first(u, "687866866"),
        "actual_amount": _first(u, "687866865"),
        "godown": _first(u, "788530165"),
        "standard_rate": _first(u, "738198533"),
        "standard_amount": _first(u, "687866886"),
        "group": _first(u, "788530158"),
        "litres_687866861": _first(u, "687866861"),
        "litres_687866876": _first(u, "687866876"),
        "litres_687872869": _first(u, "687872869"),
        "litres_721421314": _first(u, "721421314"),
        "litres_721421315": _first(u, "721421315"),
        "udf_687866858": _first(u, "687866858"),
        "udf_687872868": _first(u, "687872868"),
        "udf_687872870": _first(u, "687872870"),
        "udf_553648248": _first(u, "553648248"),
        "udf_671089661": _first(u, "671089661"),
    }


def parse_purchase_milk(xml_text: str, from_date=None, to_date=None):
    root = parse_xml(xml_text)
    records = []
    for voucher in root.iter():
        if voucher.tag.upper() != "VOUCHER":
            continue
        record = parse_voucher(voucher)
        if record["voucher_type"] != "Purchase Milk":
            continue
        if from_date and record["date"] < from_date:
            continue
        if to_date and record["date"] > to_date:
            continue
        records.append(record)
    return records
