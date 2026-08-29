"""
schemas/purchase_milk.py

Validates and normalizes a raw parsed record (dict from
purchase_milk_tally_parser.parse_purchase_milk) before it's synchronized
into the database.

The real parser (see purchase_milk_tally_parser.py) returns every field as
a plain string, including numeric ones (e.g. litres="43.72"), and "" when
a field wasn't found on that voucher. This module is the one place that:
  - requires guid/alter_id to be present (a record without them can't be
    identified for upsert -- it's rejected and counted as `failed`, never
    silently dropped)
  - coerces the parser's numeric-looking strings into floats for columns
    that are NUMERIC in the database, turning "" into NULL
  - leaves standard_rate and udf_671089661 as raw strings, since Tally
    sometimes appends a unit to those ("74.00/ltr", "18.26 ltr") and
    stripping it would be lossy
  - parses the date string into a real date
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_cls, datetime
from typing import Any

REQUIRED_FIELDS = ("guid", "alter_id")

# Columns whose source value is a clean numeric string (or "") and should
# be stored as NUMERIC in the database.
_NUMERIC_COLUMNS = (
    "litres", "degree", "fat", "snf",
    "actual_rate", "actual_amount", "standard_amount",
    "litres_687866861", "litres_687866876", "litres_687872869",
    "udf_687866858", "udf_687872868", "udf_687872870",
)

# Columns kept as raw strings -- may carry a unit suffix from Tally.
_RAW_STRING_COLUMNS = (
    "standard_rate", "udf_671089661",
    "litres_721421314", "litres_721421315", "udf_553648248",
)

# Plain text columns, no numeric coercion.
_TEXT_COLUMNS = (
    "master_id", "voucher_number", "voucher_type", "party_ledger",
    "milk_type", "shift", "godown", "group",
)

_ALL_VALUE_COLUMNS = _NUMERIC_COLUMNS + _RAW_STRING_COLUMNS + _TEXT_COLUMNS + ("date",)


class RecordValidationError(Exception):
    """Raised for a single record that fails validation. Caught by the
    sync service and counted as `records_failed` -- it never aborts the
    whole run and never causes a silent drop."""


@dataclass
class ValidatedRecord:
    guid: str
    alter_id: str
    values: dict[str, Any]  # full column-name -> value mapping, DB-ready


def _parse_date(value: Any) -> date_cls | None:
    if value is None or value == "":
        return None
    if isinstance(value, date_cls):
        return value
    if isinstance(value, str):
        # Parser emits Tally-style YYYYMMDD; accept ISO too, defensively.
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        raise RecordValidationError(f"Unparseable date: {value!r}")
    raise RecordValidationError(f"Unexpected date type: {type(value)!r}")


def _to_numeric(field: str, value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        raise RecordValidationError(f"Non-numeric value for '{field}': {value!r}")


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def validate_record(raw: dict[str, Any]) -> ValidatedRecord:
    """
    Validates and normalizes a single parsed record.

    Raises RecordValidationError if the record is missing a required
    identity field (guid/alter_id) or has a value that can't be safely
    stored. Never mutates `raw`.
    """
    for field in REQUIRED_FIELDS:
        if not raw.get(field):
            raise RecordValidationError(
                f"Missing required field '{field}' (record keys: {sorted(raw.keys())})"
            )

    values: dict[str, Any] = {}
    for col in _ALL_VALUE_COLUMNS:
        v = raw.get(col)
        if col == "date":
            v = _parse_date(v)
        elif col in _NUMERIC_COLUMNS:
            v = _to_numeric(col, v)
        elif col in _RAW_STRING_COLUMNS or col in _TEXT_COLUMNS:
            v = _to_text(v)
        values[col] = v

    return ValidatedRecord(
        guid=str(raw["guid"]).strip(),
        alter_id=str(raw["alter_id"]).strip(),
        values=values,
    )
