from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

SALES=Path("storage/sales_batch_inventory_movements.json")
ACCOUNTING=Path("storage/unified_accounting_ledger.json")
OUT=Path("storage/inventory_accounting_reconciliation_v2.json")

def s(v): return "" if v is None else str(v).strip()
def n(v):
    try: return float(v)
    except (TypeError,ValueError): return 0.0

def key(r):
    return (s(r.get("guid")),s(r.get("master_id")),s(r.get("alter_id")),s(r.get("voucher_number")))

sales=json.loads(SALES.read_text(encoding="utf-8"))
acct=json.loads(ACCOUNTING.read_text(encoding="utf-8"))

iv=defaultdict(list); av=defaultdict(list)
for r in sales: 
    if any(key(r)): iv[key(r)].append(r)
for r in acct:
    if any(key(r)): av[key(r)].append(r)

both=set(iv)&set(av)
rows=[]
reconciled=0
for k in sorted(both):
    inventory=sum(abs(n(r.get("amount"))) for r in iv[k] if r.get("amount") not in (None,""))
    party=sum(n(r.get("amount")) for r in av[k] if s(r.get("is_party_ledger")).lower()=="yes")
    other=sum(n(r.get("amount")) for r in av[k] if s(r.get("is_party_ledger")).lower()!="yes")
    # Tally sign convention: party side normally carries opposite sign to the
    # non-party components, so compare absolute party value with inventory + other.
    expected=inventory+other
    diff=round(expected-abs(party),2)
    ok=abs(diff)<0.005
    if ok: reconciled+=1
    rows.append({
        "guid":k[0],"master_id":k[1],"alter_id":k[2],"voucher_number":k[3],
        "inventory_value":round(inventory,2),
        "other_ledger_net":round(other,2),
        "expected_party_abs":round(expected,2),
        "actual_party_abs":round(abs(party),2),
        "difference":diff,
        "status":"RECONCILED" if ok else "REVIEW"
    })

report={
    "inventory_rows":len(sales),
    "accounting_rows":len(acct),
    "unique_inventory_vouchers":len(iv),
    "unique_accounting_vouchers":len(av),
    "vouchers_in_both":len(both),
    "reconciled":reconciled,
    "review":len(rows)-reconciled,
    "details":rows
}
OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print("INVENTORY ↔ ACCOUNTING RECONCILIATION V2")
print(f"Inventory rows                  : {len(sales)}")
print(f"Accounting rows                 : {len(acct)}")
print(f"Vouchers in BOTH                : {len(both)}")
print(f"RECONCILED                      : {reconciled}")
print(f"REVIEW                          : {len(rows)-reconciled}")
for r in [x for x in rows if x["status"]=="REVIEW"][:10]:
    print(r)
print(f"Report -> {OUT}")
