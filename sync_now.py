"""
Sync Now orchestration scaffold.

This module is intentionally conservative:
- Uses the proven Day Book discovery path.
- Does not guess/replace existing domain extractors.
- Classifies new/changed vouchers.
- Provides the production orchestration boundary where the existing
  Sales / Purchase Milk / Stock Journal extractors can be called.
- Database writes are not performed until the existing service-specific
  normalization/upsert functions are wired in.

The discovery + change detection logic is real and runnable.

Usage:
    python sync_now.py 20260819 20260819
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from daybook_discovery import discover_daybook_vouchers

SYNC_STATE_FILE = Path("storage") / "sync_now_state.json"

SUPPORTED_TYPES = {
    "Sales",
    "Purchase Milk",
    "Stock Journal",
}


def load_state() -> dict[str, dict[str, Any]]:
    if not SYNC_STATE_FILE.exists():
        return {}

    try:
        data = json.loads(SYNC_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: dict[str, dict[str, Any]]) -> None:
    SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SYNC_STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def classify_vouchers(
    discovered: list[dict[str, Any]],
    state: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """
    Compare Day Book identities against the local Sync Now state.

    Change rules:
      - GUID not present -> NEW
      - GUID present and AlterID changed -> CHANGED
      - GUID present and same AlterID -> UNCHANGED

    Unsupported voucher types are ignored by the sync plan.
    """
    result = {
        "new": [],
        "changed": [],
        "unchanged": [],
    }

    for voucher in discovered:
        voucher_type = voucher.get("voucher_type", "")
        if voucher_type not in SUPPORTED_TYPES:
            continue

        guid = (voucher.get("guid") or "").strip()
        if not guid:
            continue

        old = state.get(guid)

        if old is None:
            result["new"].append(voucher)
        elif str(old.get("alter_id", "")).strip() != str(
            voucher.get("alter_id", "")
        ).strip():
            result["changed"].append(voucher)
        else:
            result["unchanged"].append(voucher)

    return result


def update_discovery_state(
    state: dict[str, dict[str, Any]],
    discovered: list[dict[str, Any]],
) -> None:
    """
    Store the latest Day Book identity metadata.

    This state is deliberately separate from the DB checkpoint until the
    complete DB-backed transaction flow is wired.
    """
    for voucher in discovered:
        guid = (voucher.get("guid") or "").strip()
        if not guid or voucher.get("voucher_type") not in SUPPORTED_TYPES:
            continue

        state[guid] = {
            "date": voucher.get("date", ""),
            "voucher_number": voucher.get("voucher_number", ""),
            "voucher_type": voucher.get("voucher_type", ""),
            "master_id": str(voucher.get("master_id", "")).strip(),
            "alter_id": str(voucher.get("alter_id", "")).strip(),
            "party_ledger": voucher.get("party_ledger", ""),
        }


def build_sync_plan(
    from_date: str,
    to_date: str,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    discovered = discover_daybook_vouchers(from_date, to_date)
    state = load_state()
    plan = classify_vouchers(discovered, state)
    return plan, state


def main() -> int:
    import sys

    if len(sys.argv) != 3:
        print("Usage: python sync_now.py YYYYMMDD YYYYMMDD")
        return 2

    from_date, to_date = sys.argv[1], sys.argv[2]

    print("SYNC NOW — DISCOVERY + CHANGE DETECTION")
    print("=" * 60)
    print(f"Window: {from_date} -> {to_date}")
    print()

    try:
        plan, state = build_sync_plan(from_date, to_date)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    total = sum(len(v) for v in plan.values())

    print(f"New       : {len(plan['new'])}")
    print(f"Changed   : {len(plan['changed'])}")
    print(f"Unchanged : {len(plan['unchanged'])}")
    print(f"Relevant : {total}")
    print()

    if plan["new"]:
        print("NEW:")
        for row in plan["new"]:
            print(
                f"  {row['voucher_type']:<15} "
                f"#{row['voucher_number']:<8} "
                f"GUID={row['guid']} "
                f"AlterID={row['alter_id']}"
            )

    if plan["changed"]:
        print("CHANGED:")
        for row in plan["changed"]:
            old = state.get(row["guid"], {})
            print(
                f"  {row['voucher_type']:<15} "
                f"#{row['voucher_number']:<8} "
                f"AlterID {old.get('alter_id', '')} -> {row['alter_id']}"
            )

    print()
    print("DISCOVERY/CHANGE-DETECTION COMPLETE.")
    print("Database write phase is intentionally not enabled yet.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
