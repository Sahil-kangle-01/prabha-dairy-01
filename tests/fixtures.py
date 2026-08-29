"""
tests/fixtures.py

Generates synthetic parsed Purchase Milk records shaped exactly like what
the real purchase_milk_tally_parser.parse_purchase_milk() returns: every
field -- including numeric ones -- is a plain string, and "" when a field
wasn't present on that voucher. This exercises sync_service and the
schemas/purchase_milk.py coercion layer through the real, validated
contract; only the data source (Tally) is swapped for synthetic records.
"""

from __future__ import annotations

import random
from typing import Any

_SUPPLIERS = [
    "Dadarao Atude Patil",
    "Baba Bakle",
    "BALCHANDRA MOHINE CM",
    "Pandit Harne",
    "Rameshwar Auttade",
]
_MILK_TYPES = ["Cow", "Buffalo", "Mishr"]
_SHIFTS = ["Morning", "Evening"]
_GODOWNS = ["Godown 56", "Godown 54", "Godown 68"]


def make_record(seq: int, alter_id: int | str | None = None) -> dict[str, Any]:
    rnd = random.Random(seq)  # deterministic per seq
    litres = round(rnd.uniform(5, 60), 2)
    fat = round(rnd.uniform(3.0, 6.5), 2)
    return {
        "date": "20260401",
        "voucher_number": f"PM/2026/{seq:06d}",
        "voucher_type": "Purchase Milk",
        "party_ledger": _SUPPLIERS[seq % len(_SUPPLIERS)],
        "guid": f"GUID-PM-{seq:06d}",
        "master_id": str(10000 + seq),
        "alter_id": str(alter_id if alter_id is not None else 1),
        "litres": str(litres),
        "milk_type": _MILK_TYPES[seq % len(_MILK_TYPES)],
        "shift": _SHIFTS[seq % len(_SHIFTS)],
        "degree": str(round(rnd.uniform(26, 34), 2)),
        "fat": str(fat),
        "snf": str(round(rnd.uniform(8.0, 9.5), 2)),
        "actual_rate": str(round(rnd.uniform(30, 45), 2)),
        "actual_amount": str(round(litres * rnd.uniform(30, 45), 2)),
        "godown": _GODOWNS[seq % len(_GODOWNS)],
        "standard_rate": f"{round(rnd.uniform(70, 78), 2)}/ltr",
        "standard_amount": str(round(litres * rnd.uniform(70, 78), 2)),
        "group": "Purchase Milk",
        "litres_687866861": str(round(litres * 0.5, 2)),
        "litres_687866876": str(round(litres * 0.5, 2)),
        "litres_687872869": "",
        "litres_721421314": "",
        "litres_721421315": "",
        "udf_687866858": str(fat),
        "udf_687872868": str(round(rnd.uniform(8.0, 9.5), 2)),
        "udf_687872870": "",
        "udf_553648248": "",
        "udf_671089661": f"{litres} ltr",
    }


def make_dataset(n: int) -> list[dict[str, Any]]:
    return [make_record(i) for i in range(1, n + 1)]
