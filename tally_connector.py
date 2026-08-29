import requests
import xml.etree.ElementTree as ET
import re
from datetime import datetime
import sys
import os

# ============================================================
# TALLY CONNECTION
# ============================================================

TALLY_URL = "http://localhost:9000"
TIMEOUT = 15

OUTPUT_DIR = "tally_extracted_data"
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ============================================================
# XML SANITIZATION
# ============================================================
# Tally's XML export frequently contains stray unescaped '&' characters
# (e.g. inside names like "R&D" or "S&K Traders") and occasional illegal
# control characters. Both break strict XML parsing even though the data
# itself is fine. Clean the text before handing it to ElementTree.

def _is_legal_xml_codepoint(codepoint):
    # XML 1.0 legal character ranges
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
    return match.group(0) if _is_legal_xml_codepoint(codepoint) else ''


def _strip_illegal_hex_ref(match):
    try:
        codepoint = int(match.group(1), 16)
    except ValueError:
        return match.group(0)
    return match.group(0) if _is_legal_xml_codepoint(codepoint) else ''


def clean_xml(text):
    if text is None:
        return text
    # Strip raw illegal control bytes (keep tab, newline, carriage return)
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F]', '', text)

    # Strip numeric character references that point to illegal XML chars.
    # Tally emits e.g. &#4; as a bullet before "Primary" in PARENT fields —
    # that's a validly-formatted entity, but codepoint 4 is illegal in XML
    # even when properly escaped, so ElementTree still rejects it.
    text = re.sub(r'&#(\d+);', _strip_illegal_numeric_ref, text)
    text = re.sub(r'&#x([0-9a-fA-F]+);', _strip_illegal_hex_ref, text)

    # Escape stray ampersands that aren't already valid XML entities
    text = re.sub(r'&(?!amp;|lt;|gt;|apos;|quot;|#\d+;|#x[0-9a-fA-F]+;)', '&amp;', text)

    # Voucher exports contain custom-field tags like <UDF:_UDF_805306571.LIST>.
    # XML treats "UDF:" as a namespace prefix, but it's never declared with
    # an xmlns, so ElementTree raises "unbound prefix" and refuses to parse
    # the whole document. Replace the colon with an underscore so it's just
    # an ordinary (if odd-looking) tag name instead of a namespace prefix.
    text = re.sub(r'</?UDF:', lambda m: m.group(0).replace(':', '_'), text)

    return text


def safe_parse(xml_text):
    """Parse Tally's XML after sanitizing. Returns the root Element, or None
    if it still can't be parsed after cleaning."""
    try:
        return ET.fromstring(clean_xml(xml_text))
    except ET.ParseError as e:
        print(f"⚠️ XML still failed to parse after cleaning: {e}")
        return None


# ============================================================
# COMMON REQUEST FUNCTION
# ============================================================

def send_to_tally(xml_request, timeout=None):
    request_timeout = timeout if timeout is not None else TIMEOUT
    try:
        response = requests.post(
            TALLY_URL,
            data=xml_request.encode("utf-8"),
            headers={
                "Content-Type": "text/xml; charset=utf-8"
            },
            timeout=request_timeout
        )

        # Only print the status line on failure -- for a batch sync
        # this fires once per voucher fetched, so on a healthy 2000+
        # voucher run it used to print 2000+ identical "HTTP Status:
        # 200" lines for no reason. Success stays silent; only
        # problems get printed.
        if response.status_code != 200:
            print(f"HTTP Status: {response.status_code}")
            print("❌ Tally returned an HTTP error.")
            print(response.text[:3000])
            return None

        # HTTP 200 does not necessarily mean Tally accepted the request.
        # Tally can return HTTP 200 with an XML error such as:
        # <RESPONSE>Unknown Request, cannot be processed</RESPONSE>
        root = safe_parse(response.text)

        if root is not None:
            errors = []
            for elem in root.iter():
                if elem.tag.upper() == "RESPONSE" and elem.text:
                    value = elem.text.strip()
                    if value:
                        errors.append(value)

            if errors:
                print("❌ Tally rejected the request.")
                for error in errors:
                    print(f"   {error}")

                print("\nRaw Tally response:")
                print(response.text[:3000])
                return None
        else:
            # Could not parse even after cleaning — still return the raw
            # text so it gets saved to disk for manual inspection, but warn.
            print("⚠️ Tally returned HTTP 200, but the response could not be parsed as XML even after cleaning.")
            print(response.text[:2000])

        return response.text

    except requests.exceptions.ConnectionError:
        print("\n❌ Could not connect to Tally.")
        print("Make sure:")
        print("  1. Tally ERP 9 is running")
        print("  2. The correct company is loaded")
        print("  3. Tally is listening on port 9000")
        return None

    except requests.exceptions.Timeout:
        print("\n❌ Tally request timed out.")
        print("Try a smaller request/date range if this happens during voucher extraction.")
        return None

    except Exception as e:
        print(f"\n❌ Error: {e}")
        return None


# ============================================================
# SAVE RAW XML
# ============================================================

def save_xml(filename, xml_data):
    path = os.path.join(OUTPUT_DIR, filename)

    with open(path, "w", encoding="utf-8") as f:
        f.write(xml_data)

    print(f"✅ Saved: {path}")


def filter_vouchers_by_date(root, from_date, to_date):
    """Tally's server-side SVFROMDATE/SVTODATE filtering has proven
    unreliable (both the raw Voucher Collection and the Day Book report
    have returned vouchers from outside the requested range). This does
    the filtering ourselves: walk every VOUCHER, read its own <DATE>, and
    only keep the ones actually inside [from_date, to_date]. Returns
    (matched_vouchers, total_found, out_of_range_count)."""
    all_vouchers = [elem for elem in root.iter() if elem.tag.upper() == "VOUCHER"]
    matched = []
    out_of_range = 0

    for v in all_vouchers:
        date_elem = v.find("DATE")
        date_text = (date_elem.text or "").strip() if date_elem is not None else ""
        if from_date <= date_text <= to_date:
            matched.append(v)
        else:
            out_of_range += 1

    return matched, len(all_vouchers), out_of_range


# ============================================================
# 1. CONNECTION / COMPANY TEST
# ============================================================

def test_connection():

    print("\n========================================")
    print("TESTING TALLY CONNECTION")
    print("========================================")

    xml_request = """
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>List of Companies</ID>
    </HEADER>
    <BODY>
        <DESC>
        </DESC>
    </BODY>
</ENVELOPE>
"""

    response = send_to_tally(xml_request)

    if response:
        save_xml("companies_raw.xml", response)

        print("\n✅ Tally connection successful.")

        root = safe_parse(response)

        if root is not None:
            companies = []

            for elem in root.iter():
                if elem.tag.upper() == "NAME" and elem.text:
                    companies.append(elem.text.strip())

                # Tally may return the company name as an attribute:
                # <COMPANY NAME="...">
                if elem.tag.upper() == "COMPANY":
                    name = elem.attrib.get("NAME")
                    if name:
                        companies.append(name.strip())

            companies = list(dict.fromkeys(companies))

            if companies:
                print("\nCompanies found:")
                for company in companies:
                    print("  •", company)
            else:
                print("Response received, but company names were not parsed.")
                print("Raw XML has been saved for inspection.")
        else:
            print("⚠️ Tally response was received but XML parsing failed even after cleaning.")

    return response


# ============================================================
# 2. GET LEDGERS
# ============================================================

def get_ledgers():

    print("\n========================================")
    print("EXTRACTING LEDGERS")
    print("========================================")

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

                    <COLLECTION NAME="Ledger Collection">
                        <TYPE>Ledger</TYPE>
                        <FETCH>
                            NAME
                            PARENT
                            GUID
                            OPENINGBALANCE
                            CLOSINGBALANCE
                        </FETCH>
                    </COLLECTION>

                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""

    response = send_to_tally(xml_request)

    if response:
        save_xml("ledgers_raw.xml", response)
        print("✅ Ledger extraction completed.")

        root = safe_parse(response)
        if root is not None:
            ledgers = [elem for elem in root.iter() if elem.tag.upper() == "LEDGER"]
            print(f"Ledgers found: {len(ledgers)}")
            for ledger in ledgers[:20]:
                print("  •", ledger.attrib.get("NAME", ""))

    return response


# ============================================================
# 3. GET STOCK ITEMS
# ============================================================

def get_stock_items():

    print("\n========================================")
    print("EXTRACTING STOCK ITEMS")
    print("========================================")

    # Minimal StockItem collection — confirmed working against the
    # client's live Tally instance.
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

    response = send_to_tally(xml_request)

    if response:
        save_xml("stock_items_raw.xml", response)

        root = safe_parse(response)

        if root is not None:
            stock_items = [
                elem for elem in root.iter()
                if elem.tag.upper() == "STOCKITEM"
            ]

            print("✅ Stock item extraction completed.")
            print(f"Stock items found: {len(stock_items)}")

            if stock_items:
                print("\nFirst stock items:")
                for item in stock_items[:20]:
                    name = item.attrib.get("NAME", "")
                    print("  •", name)
            else:
                print("⚠️ No STOCKITEM records were found.")
                print("Raw XML has been saved for inspection.")
        else:
            print("⚠️ Response could not be parsed even after cleaning — raw file still saved above.")

    return response


# ============================================================
# 4. GET GODOWNS
# ============================================================

def get_godowns():

    print("\n========================================")
    print("EXTRACTING GODOWNS")
    print("========================================")

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

    response = send_to_tally(xml_request)

    if response:
        save_xml("godowns_raw.xml", response)

        root = safe_parse(response)

        if root is not None:
            godowns = [
                elem for elem in root.iter()
                if elem.tag.upper() == "GODOWN"
            ]

            print("✅ Godown extraction completed.")
            print(f"Godowns found: {len(godowns)}")

            if godowns:
                print("\nGodowns found:")
                for godown in godowns[:20]:
                    name = godown.attrib.get("NAME", "")
                    print("  •", name)
            else:
                print("⚠️ No GODOWN records were found.")
                print("Raw XML has been saved for inspection.")
        else:
            print("⚠️ Response could not be parsed even after cleaning — raw file still saved above.")

    return response


# ============================================================
# 5. GET VOUCHERS FOR A DATE RANGE
# ============================================================

def get_vouchers(from_date, to_date):

    print("\n========================================")
    print("EXTRACTING VOUCHERS")
    print("========================================")
    print(f"From: {from_date}")
    print(f"To  : {to_date}")

    # $$Date: expects a human date string like "1-Aug-2026", not raw
    # YYYYMMDD. Passing YYYYMMDD directly makes the filter fail to parse
    # silently — Tally returns HTTP 200 / STATUS 1 with an EMPTY collection
    # instead of an error, which is exactly what happened on the first run.
    # First transaction test is intentionally minimal.
    # We are not requesting ALLINVENTORYENTRIES.* yet.
    # Once this returns valid voucher data, we will expand it
    # to capture inventory, godown, rate, quantity and R3 fields.
    #
    # NOTE: earlier attempts added a custom FILTER (first with a hand
    # formatted $$Date: literal, then with ##SVFROMDATE/##SVTODATE) to try
    # to force date-scoping. Both behaved unpredictably — the $$Date:
    # version under-matched (found 1 voucher for a 5-day range), and the
    # ##SV version appears to have failed silently and matched everything,
    # causing the request to time out again. Removing the custom FILTER
    # entirely and relying purely on STATICVARIABLES SVFROMDATE/SVTODATE —
    # the standard, documented way Tally scopes a Voucher collection to a
    # period — is the most reliable approach.
    xml_request = f"""
<ENVELOPE>
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
</ENVELOPE>
"""

    response = send_to_tally(xml_request, timeout=180)

    if response:
        filename = f"vouchers_{from_date}_{to_date}_RAW.xml"
        save_xml(filename, response)

        root = safe_parse(response)

        if root is not None:
            matched, total_found, out_of_range = filter_vouchers_by_date(root, from_date, to_date)

            print(f"\nTotal <VOUCHER> records in response: {total_found}")

            if out_of_range > 0:
                print(f"⚠️ {out_of_range} of those are OUTSIDE {from_date}–{to_date}.")
                print("   Tally's date filter did not actually restrict the results —")
                print("   this script filtered them out itself before saving.")
            else:
                print("✅ All returned vouchers are within the requested date range.")

            print(f"Vouchers actually in range: {len(matched)}")

            if matched:
                print("\nFirst vouchers (in-range only):")
                for voucher in matched[:10]:
                    print(
                        "  •",
                        voucher.attrib.get("VCHTYPE", ""),
                        voucher.attrib.get("VOUCHERNUMBER", "")
                    )

                # Save a filtered file containing ONLY the verified in-range
                # vouchers — this is what downstream tools should actually
                # read from, not the raw (possibly full-history) response.
                filtered_xml = "<ENVELOPE><VERIFIED_VOUCHERS>" + \
                    "".join(ET.tostring(v, encoding="unicode") for v in matched) + \
                    "</VERIFIED_VOUCHERS></ENVELOPE>"
                filtered_filename = f"vouchers_{from_date}_{to_date}_VERIFIED.xml"
                save_xml(filtered_filename, filtered_xml)
            else:
                print("⚠️ No vouchers found in this date range after filtering.")
        else:
            print("⚠️ Voucher response could not be parsed even after cleaning — raw file still saved above.")

    return response


# ============================================================
# 6. CUSTOM / EVERYTHING TEST
# ============================================================

def get_raw_voucher_test(from_date, to_date):

    print("\n========================================")
    print("RAW VOUCHER TEST — DAY BOOK")
    print("========================================")

    # 'Voucher Register' was a guess and it hung. 'Day Book' is confirmed —
    # it's the exact report you can see live in Tally's UI for a date
    # range, so it's guaranteed to be period-scoped and reasonably light,
    # unlike a raw Voucher collection walk which appears to ignore
    # SVFROMDATE/SVTODATE and scan the entire voucher history instead.
    xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Day Book</ID>
    </HEADER>

    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVFROMDATE>{from_date}</SVFROMDATE>
                <SVTODATE>{to_date}</SVTODATE>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
        </DESC>
    </BODY>
</ENVELOPE>
"""

    response = send_to_tally(xml_request, timeout=90)

    if response:
        filename = f"daybook_{from_date}_{to_date}_RAW.xml"
        save_xml(filename, response)

        root = safe_parse(response)
        if root is not None:
            matched, total_found, out_of_range = filter_vouchers_by_date(root, from_date, to_date)

            print(f"\nTotal <VOUCHER> records in response: {total_found}")
            if out_of_range > 0:
                print(f"⚠️ {out_of_range} of those are OUTSIDE {from_date}–{to_date}.")
                print("   Day Book's date filter did not actually restrict the results either.")
            else:
                print("✅ All returned vouchers are within the requested date range.")

            print(f"Vouchers actually in range: {len(matched)}")
            for v in matched[:15]:
                print("  •", v.attrib.get("VCHTYPE", ""), v.attrib.get("VOUCHERNUMBER", ""))
        else:
            print("\n✅ Day Book received (raw file saved — inspect it directly).")

    return response


# ============================================================
# 7. R3 UDF DISCOVERY / MAPPING TOOL
# ============================================================

def discover_udf_fields(from_date, to_date):
    """Fetches vouchers for the given range, verifies/filters them to the
    actual date range client-side (see filter_vouchers_by_date), then walks
    every custom UDF field in those vouchers and groups them by their
    internal INDEX number — showing type, sample values, how often each
    appears, and which voucher types use them. This does NOT guess which
    index means 'Litres' or 'Degree' or 'Rate' — it only surfaces the raw
    pattern so it can be compared against what's actually shown on a real
    Tally voucher screen."""

    print("\n========================================")
    print("R3 UDF DISCOVERY")
    print("========================================")
    print(f"From: {from_date}")
    print(f"To  : {to_date}")
    print("(This may take a while — it pulls the full voucher collection")
    print(" first, then filters client-side, since Tally's own date filter")
    print(" cannot be trusted yet.)")

    xml_request = f"""
<ENVELOPE>
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
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""
    # NOTE: unlike get_vouchers(), no FETCH list is given here — we want
    # Tally's FULL default voucher dump (including all UDF fields), since
    # a narrow FETCH list may otherwise suppress the custom fields we're
    # trying to discover.

    response = send_to_tally(xml_request, timeout=180)
    if not response:
        return

    save_xml(f"udf_discovery_{from_date}_{to_date}_RAW.xml", response)

    root = safe_parse(response)
    if root is None:
        print("⚠️ Could not parse response even after cleaning — raw file saved above for manual inspection.")
        return

    matched, total_found, out_of_range = filter_vouchers_by_date(root, from_date, to_date)
    print(f"\nTotal <VOUCHER> records in response: {total_found}")
    if out_of_range > 0:
        print(f"⚠️ {out_of_range} outside the requested range — excluded from this analysis.")
    print(f"Vouchers analyzed (in range): {len(matched)}")

    if not matched:
        print("⚠️ Nothing to analyze — no vouchers matched the date range.")
        return

    # Walk every voucher and collect its leaf UDF fields.
    # Structure in the XML looks like:
    #   <UDF__UDF_687866876.LIST TYPE="Amount" INDEX="1019">
    #       <UDF__UDF_687866876 DESC="">43.72</UDF__UDF_687866876>
    #   </UDF__UDF_687866876.LIST>
    # The wrapper (".LIST") carries INDEX + TYPE; the inner element carries
    # the actual value. Group (non-leaf) wrappers don't have a TYPE
    # attribute, so filtering on TYPE presence skips them automatically.
    udf_map = {}  # index -> {"type": str, "values": {value: count}, "vch_types": {vchtype: count}}

    for v in matched:
        vchtype = v.attrib.get("VCHTYPE", "(unknown)")
        for elem in v.iter():
            if not elem.tag.endswith(".LIST"):
                continue
            index = elem.attrib.get("INDEX")
            utype = elem.attrib.get("TYPE")
            if not index or not utype:
                continue  # group wrapper, not a leaf field

            children = list(elem)
            value = (children[0].text or "").strip() if children else (elem.text or "").strip()

            entry = udf_map.setdefault(index, {"type": utype, "values": {}, "vch_types": {}})
            entry["values"][value] = entry["values"].get(value, 0) + 1
            entry["vch_types"][vchtype] = entry["vch_types"].get(vchtype, 0) + 1

    if not udf_map:
        print("⚠️ No UDF fields were found in these vouchers.")
        return

    # Build the report, sorted by index
    def sort_key(idx):
        try:
            return int(idx)
        except ValueError:
            return 0

    lines = []
    lines.append(f"R3 UDF DISCOVERY REPORT — {from_date} to {to_date}")
    lines.append(f"Vouchers analyzed: {len(matched)}")
    lines.append("=" * 80)
    lines.append("")

    for index in sorted(udf_map.keys(), key=sort_key):
        entry = udf_map[index]
        total = sum(entry["values"].values())
        sample_values = list(entry["values"].items())
        sample_values.sort(key=lambda x: -x[1])
        sample_str = ", ".join(f"{val!r}({cnt})" for val, cnt in sample_values[:10])
        vch_types_sorted = sorted(entry["vch_types"].items(), key=lambda x: -x[1])
        vch_str = ", ".join(f"{vt}({cnt})" for vt, cnt in vch_types_sorted[:6])

        lines.append(f"UDF INDEX {index}  |  TYPE: {entry['type']}  |  seen {total}x")
        lines.append(f"    Voucher types: {vch_str}")
        lines.append(f"    Sample values: {sample_str}")
        lines.append("")

    report = "\n".join(lines)

    print("\n" + "=" * 60)
    print(report[:4000])  # print a preview to the console
    if len(report) > 4000:
        print(f"... ({len(report) - 4000} more characters — see the saved file)")

    report_path = os.path.join(OUTPUT_DIR, f"udf_discovery_report_{from_date}_{to_date}.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n✅ Full report saved: {report_path}")
    print("\nNext step: open a specific voucher in Tally (same date/number),")
    print("note its on-screen Litres / Degree / FAT / SNF / Rate, and match")
    print("those numbers against the sample values above by UDF INDEX.")


# ============================================================
# 8. INSPECT ONE PURCHASE MILK VOUCHER
# ============================================================

# Field IDs found in the client's data so far, with their best-known
# meaning. Keyed by the raw internal field ID (the numeric part of the
# tag name), NOT by INDEX — INDEX values are not unique (two different
# fields have been observed sharing the same INDEX), so grouping by ID
# is the only reliable way to avoid conflating unrelated fields.
KNOWN_FIELD_MEANINGS = {
    "687866865": "Amount (confirmed — matches on-screen Amount)",
    "687866866": "Rate (confirmed — matches on-screen Rate)",
    "687866876": "Litres (candidate)",
    "687872869": "Litres (candidate, duplicate)",
    "687866861": "Litres (candidate, duplicate)",
    "788530154": "Milk Type — Cow/Buffalo (candidate)",
    "788530155": "Shift — Morning/Evening (candidate)",
    "788530165": "Godown (candidate)",
    "687866864": "Degree (candidate)",
    "687872868": "Degree (candidate, duplicate)",
    "687866863": "FAT (candidate)",
    "687872870": "FAT (candidate, duplicate)",
    "687866862": "SNF (candidate)",
    "738198533": "Internal/standard Rate (not the billed rate)",
    "687866886": "Amount at internal/standard rate",
    "788530158": "Group",
}


def _extract_field_id(tag):
    """Pulls the numeric field ID out of a tag like 'UDF__UDF_687866865'
    or 'UDF__UDF_687866865.LIST'."""
    m = re.search(r'UDF_(\d+)', tag)
    return m.group(1) if m else None


def inspect_one_purchase_milk_voucher(date):
    """Fetches every voucher for a single day, picks the first
    'Purchase Milk' voucher after client-side date verification, and
    prints every field on it — header fields, inventory line (if Tally
    includes it), and every UDF field keyed by its real field ID with
    its best-known meaning where one exists."""

    print("\n========================================")
    print("INSPECT ONE PURCHASE MILK VOUCHER")
    print("========================================")
    print(f"Date: {date}")

    # No FETCH restriction on the header fields (ISINITIALIZE=Yes already
    # returns the full default/UDF set regardless, as observed). We ADD an
    # explicit fetch for ALLINVENTORYENTRIES.LIST to try to also pull the
    # normal inventory line (Stock Item / Qty / Rate / Amount / Godown) —
    # this hasn't been confirmed to work yet since no voucher pulled so far
    # has included it; if it comes back empty, that's useful information
    # too (it would mean inventory detail needs a different request shape).
    xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Voucher Collection</ID>
    </HEADER>

    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVFROMDATE>{date}</SVFROMDATE>
                <SVTODATE>{date}</SVTODATE>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>

            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Voucher Collection" ISINITIALIZE="Yes">
                        <TYPE>Voucher</TYPE>
                        <FETCH>ALLINVENTORYENTRIES.LIST</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""

    response = send_to_tally(xml_request, timeout=90)
    if not response:
        return

    save_xml(f"inspect_purchase_milk_{date}_RAW.xml", response)

    root = safe_parse(response)
    if root is None:
        print("⚠️ Could not parse response even after cleaning — raw file saved above.")
        return

    matched, total_found, out_of_range = filter_vouchers_by_date(root, date, date)
    print(f"\nTotal <VOUCHER> records in response: {total_found}")
    if out_of_range > 0:
        print(f"⚠️ {out_of_range} outside {date} — excluded.")
    print(f"Vouchers in range: {len(matched)}")

    purchase_milk_vouchers = [v for v in matched if v.attrib.get("VCHTYPE", "") == "Purchase Milk"]

    if not purchase_milk_vouchers:
        print(f"⚠️ No 'Purchase Milk' voucher found on {date}.")
        print("   Try a different date, or check the exact VCHTYPE spelling in Tally.")
        return

    v = purchase_milk_vouchers[0]
    print(f"\nFound {len(purchase_milk_vouchers)} Purchase Milk voucher(s) on {date} — inspecting the first one.\n")

    def field_text(tagname):
        elem = v.find(tagname)
        return (elem.text or "").strip() if elem is not None else "(not present)"

    print("-" * 60)
    print("HEADER FIELDS")
    print("-" * 60)
    print(f"Voucher Date     : {field_text('DATE')}")
    print(f"Voucher Number   : {field_text('VOUCHERNUMBER')}")
    print(f"Voucher Type     : {v.attrib.get('VCHTYPE', '')}")
    print(f"Party/Ledger     : {field_text('PARTYLEDGERNAME')}")
    print(f"GUID             : {field_text('GUID')}")
    print(f"Master ID        : {field_text('MASTERID')}")

    print("\n" + "-" * 60)
    print("INVENTORY ENTRY (if present)")
    print("-" * 60)
    inv_entries = [e for e in v.iter() if e.tag.upper().replace("UDF_", "").endswith("ALLINVENTORYENTRIES.LIST")
                   or e.tag.upper() == "ALLINVENTORYENTRIES.LIST"]
    if not inv_entries:
        print("(No ALLINVENTORYENTRIES.LIST found in this response —")
        print(" inventory line detail was not returned for this voucher.)")
    else:
        for inv in inv_entries:
            stock_item = inv.find("STOCKITEMNAME")
            actual_qty = inv.find("ACTUALQTY")
            rate = inv.find("RATE")
            amount = inv.find("AMOUNT")
            godown_elem = None
            for e in inv.iter():
                if e.tag.upper() == "GODOWNNAME":
                    godown_elem = e
                    break
            print(f"Stock Item       : {(stock_item.text or '').strip() if stock_item is not None else '(not present)'}")
            print(f"Actual Quantity  : {(actual_qty.text or '').strip() if actual_qty is not None else '(not present)'}")
            print(f"Actual Rate      : {(rate.text or '').strip() if rate is not None else '(not present)'}")
            print(f"Actual Amount    : {(amount.text or '').strip() if amount is not None else '(not present)'}")
            print(f"Godown           : {(godown_elem.text or '').strip() if godown_elem is not None else '(not present)'}")
            print()

    print("-" * 60)
    print("UDF FIELDS (keyed by real field ID, not INDEX — see note below)")
    print("-" * 60)

    seen_ids = set()
    udf_lines = []
    for elem in v.iter():
        if not elem.tag.endswith(".LIST"):
            continue
        index = elem.attrib.get("INDEX")
        utype = elem.attrib.get("TYPE")
        if not index or not utype:
            continue  # group wrapper

        field_id = _extract_field_id(elem.tag)
        if not field_id or field_id in seen_ids:
            continue
        seen_ids.add(field_id)

        children = list(elem)
        value = (children[0].text or "").strip() if children else (elem.text or "").strip()

        meaning = KNOWN_FIELD_MEANINGS.get(field_id, "(unmapped)")
        udf_lines.append((index, field_id, utype, value, meaning))

    for index, field_id, utype, value, meaning in sorted(udf_lines, key=lambda x: x[1]):
        print(f"  Field {field_id} (INDEX {index}, {utype}) = {value!r}   →  {meaning}")

    print("\nNOTE: two fields can share the same INDEX number — the field ID")
    print("(the number in the tag name) is what's actually unique. Compare")
    print("the values above against what Tally shows on-screen for this")
    print("exact voucher (Date, Voucher No, Party) to confirm each mapping.")


# ============================================================
# 13. STOCK JOURNAL GODOWN TRANSFER SCANNER
# ============================================================

def scan_stock_journals_for_godown_transfers(from_date="20260401", to_date="20260819"):
    """
    Read-only scan of Stock Journal vouchers.

    Important:
    - Uses directional INVENTORYENTRIESIN.LIST / INVENTORYENTRIESOUT.LIST.
    - Reads nested BATCHALLOCATIONS.LIST.
    - A transfer is reported only when GODOWNNAME and
      DESTINATIONGODOWNNAME are both present and different.
    - Does NOT process generic INVENTORYENTRIES.LIST because that is a
      flattened/combined representation and would double-count entries.
    """
    print("\n========================================")
    print("STOCK JOURNAL GODOWN TRANSFER SCAN")
    print("========================================")
    print(f"From: {from_date}")
    print(f"To  : {to_date}")
    print("Read-only: no Tally data will be changed.")

    # First obtain the Stock Journal vouchers. We intentionally fetch
    # inventory detail in the same request so we can inspect transfers.
    xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Stock Journal Transfer Scan</ID>
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
                    <COLLECTION NAME="Stock Journal Transfer Scan" ISINITIALIZE="Yes">
                        <TYPE>Voucher</TYPE>
                        <BELONGSTO>Yes</BELONGSTO>
                        <FETCH>DATE</FETCH>
                        <FETCH>VOUCHERNUMBER</FETCH>
                        <FETCH>VOUCHERTYPENAME</FETCH>
                        <FETCH>GUID</FETCH>
                        <FETCH>MASTERID</FETCH>
                        <FETCH>ALTERID</FETCH>
                        <FETCH>ALLINVENTORYENTRIES.LIST</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""

    # Use the common sender, which keeps the connector read-only and
    # validates HTTP/Tally errors.
    response = send_to_tally(xml_request, timeout=180)
    if not response:
        return

    save_xml(
        f"stock_journal_godown_transfer_scan_{from_date}_{to_date}_RAW.xml",
        response
    )

    root = safe_parse(response)
    if root is None:
        return

    matched, total_found, out_of_range = filter_vouchers_by_date(
        root, from_date, to_date
    )

    stock_journals = [
        v for v in matched
        if (v.attrib.get("VCHTYPE", "") or "").strip().upper() == "STOCK JOURNAL"
    ]

    print(f"\nTotal vouchers in response : {total_found}")
    print(f"Outside requested range     : {out_of_range}")
    print(f"Stock Journals in range     : {len(stock_journals)}")

    transfers = []

    def txt(parent, tag):
        elem = parent.find(tag)
        return (elem.text or "").strip() if elem is not None and elem.text else ""

    for voucher in stock_journals:
        voucher_date = txt(voucher, "DATE")
        voucher_number = txt(voucher, "VOUCHERNUMBER")
        guid = txt(voucher, "GUID")
        master_id = txt(voucher, "MASTERID")
        alter_id = txt(voucher, "ALTERID")

        # Only directional lists are authoritative for this scan.
        directional_entries = []
        for elem in voucher.iter():
            tag = elem.tag.upper()
            if tag in ("INVENTORYENTRIESIN.LIST", "INVENTORYENTRIESOUT.LIST"):
                direction = "IN" if tag.endswith("IN.LIST") else "OUT"
                directional_entries.append((elem, direction))

        for inv, direction in directional_entries:
            stock_item = txt(inv, "STOCKITEMNAME")
            actual_qty = txt(inv, "ACTUALQTY")
            billed_qty = txt(inv, "BILLEDQTY")
            rate = txt(inv, "RATE")
            amount = txt(inv, "AMOUNT")
            deemed_positive = txt(inv, "ISDEEMEDPOSITIVE")

            batches = [
                e for e in inv.iter()
                if e.tag.upper() == "BATCHALLOCATIONS.LIST"
            ]

            for batch in batches:
                source = txt(batch, "GODOWNNAME")
                destination = txt(batch, "DESTINATIONGODOWNNAME")

                # A real godown transfer requires two distinct godowns.
                if not source or not destination or source == destination:
                    continue

                transfers.append({
                    "date": voucher_date,
                    "voucher_number": voucher_number,
                    "voucher_type": "Stock Journal",
                    "guid": guid,
                    "master_id": master_id,
                    "alter_id": alter_id,
                    "direction": direction,
                    "stock_item": stock_item,
                    "source_godown": source,
                    "destination_godown": destination,
                    "batch_name": txt(batch, "BATCHNAME"),
                    "actual_qty": actual_qty,
                    "billed_qty": billed_qty,
                    "rate": rate,
                    "amount": amount,
                    "batch_qty": txt(batch, "ACTUALQTY") or txt(batch, "BILLEDQTY"),
                    "batch_amount": txt(batch, "AMOUNT"),
                    "is_deemed_positive": deemed_positive,
                })

    json_path = os.path.join(
        OUTPUT_DIR,
        f"stock_journal_godown_transfers_{from_date}_{to_date}.json"
    )
    csv_path = os.path.join(
        OUTPUT_DIR,
        f"stock_journal_godown_transfers_{from_date}_{to_date}.csv"
    )

    import json
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(transfers, f, ensure_ascii=False, indent=2)

    csv_fields = [
        "date", "voucher_number", "voucher_type", "guid", "master_id",
        "alter_id", "direction", "stock_item", "source_godown",
        "destination_godown", "batch_name", "actual_qty", "billed_qty",
        "rate", "amount", "batch_qty", "batch_amount",
        "is_deemed_positive"
    ]

    import csv
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()
        writer.writerows(transfers)

    print(f"\nGodown-to-godown transfers found: {len(transfers)}")

    if transfers:
        print("\nTransfers:")
        for t in transfers:
            print(
                f"  {t['date']} | #{t['voucher_number']} | "
                f"{t['stock_item']} | "
                f"{t['source_godown']} -> {t['destination_godown']} | "
                f"Qty: {t['batch_qty'] or t['actual_qty']}"
            )
    else:
        print("No Stock Journal batch allocation was found where")
        print("source GODOWNNAME differs from DESTINATIONGODOWNNAME.")

    print(f"\nSaved JSON: {json_path}")
    print(f"Saved CSV : {csv_path}")

    return transfers


# ============================================================
# MENU
# ============================================================

def main():

    print("\n")
    print("================================================")
    print("       TALLY ERP 9 READ-ONLY CONNECTOR")
    print("================================================")
    print(f"Tally URL: {TALLY_URL}")
    print(f"Output  : {OUTPUT_DIR}/")
    print("================================================")

    while True:

        print("\nChoose an option:")
        print("--------------------------------")
        print("1. Test Tally connection")
        print("2. Extract Ledgers")
        print("3. Extract Stock Items")
        print("4. Extract Godowns")
        print("5. Extract Vouchers (date-verified)")
        print("6. Day Book Test (date-verified)")
        print("7. R3 UDF Discovery (map milk data fields)")
        print("8. Inspect ONE Purchase Milk Voucher")
        print("9. Run ALL basic extractions")
        print("13. Scan Stock Journals for Godown Transfers")
        print("0. Exit")
        print("--------------------------------")

        choice = input("Enter choice: ").strip()

        if choice == "1":
            test_connection()

        elif choice == "2":
            get_ledgers()

        elif choice == "3":
            get_stock_items()

        elif choice == "4":
            get_godowns()

        elif choice == "5":
            from_date = input("From date (YYYYMMDD): ").strip()
            to_date = input("To date (YYYYMMDD): ").strip()
            get_vouchers(from_date, to_date)

        elif choice == "6":
            from_date = input("From date (YYYYMMDD): ").strip()
            to_date = input("To date (YYYYMMDD): ").strip()
            get_raw_voucher_test(from_date, to_date)

        elif choice == "7":
            from_date = input("From date (YYYYMMDD): ").strip()
            to_date = input("To date (YYYYMMDD): ").strip()
            discover_udf_fields(from_date, to_date)

        elif choice == "8":
            date = input("Date to inspect (YYYYMMDD): ").strip()
            inspect_one_purchase_milk_voucher(date)

        elif choice == "9":
            print("\nRunning basic extraction...")

            test_connection()
            get_ledgers()
            get_stock_items()
            get_godowns()

            print("\n✅ Basic master extraction finished.")
            print("Voucher extraction is intentionally a separate test.")
            print("Use option 5 with a ONE-DAY date range only after masters are verified.")

        elif choice == "13":
            from_date = input("From date (YYYYMMDD) [default 20260401]: ").strip() or "20260401"
            to_date = input("To date (YYYYMMDD) [default 20260819]: ").strip() or "20260819"
            scan_stock_journals_for_godown_transfers(from_date, to_date)

        elif choice == "0":
            print("\nExiting...")
            sys.exit(0)

        else:
            print("\n❌ Invalid choice.")


if __name__ == "__main__":
    main()