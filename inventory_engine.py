#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

def s(v):
    return "" if v is None else str(v).strip()

def dec(v):
    if v is None or v == "": return None
    try: return Decimal(str(v).strip())
    except (InvalidOperation, ValueError): return None

def dj(v): return None if v is None else float(v)

def placeholder(r):
    return (not s(r.get("stock_item")) and r.get("quantity") is None
            and r.get("amount") is None and not s(r.get("source_godown"))
            and not s(r.get("destination_godown")))

def normalize(r, source, idx):
    q=dec(r.get("quantity"))
    out={
        "date":s(r.get("date") or r.get("voucher_date")),
        "voucher_number":s(r.get("voucher_number")),
        "voucher_type":s(r.get("voucher_type")),
        "guid":s(r.get("guid")),
        "master_id":s(r.get("master_id")),
        "alter_id":s(r.get("alter_id")),
        "stock_item":s(r.get("stock_item")),
        "quantity":dj(q),
        "unit":s(r.get("unit")),
        "rate":dj(dec(r.get("rate"))),
        "amount":dj(dec(r.get("amount"))),
        "source_godown":s(r.get("source_godown")),
        "destination_godown":s(r.get("destination_godown")),
        "batch_name":s(r.get("batch_name")),
        "movement_type":s(r.get("movement_type") or r.get("direction")).upper(),
        "source_system":source,
        "source_index":idx,
    }
    if out["movement_type"] in ("TRANSFER","GODOWN_TRANSFER"):
        out["movement_type"]="GODOWN_TRANSFER"
    elif out["movement_type"] in ("IN_SAME_GODOWN","OUT_SAME_GODOWN"):
        out["movement_type"]=out["movement_type"].split("_")[0]
    return None if placeholder(out) else out

def load(path, source):
    path=Path(path)
    if not path.exists():
        print(f"[WARN] Missing {source}: {path}")
        return []
    data=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data,list): raise ValueError(f"{path} must contain a JSON list")
    return [x for i,r in enumerate(data,1) if isinstance(r,dict)
            for x in [normalize(r,source,i)] if x is not None]

def key(r):
    return tuple(r.get(k) for k in (
        "guid","master_id","alter_id","voucher_type","voucher_number",
        "stock_item","quantity","unit","rate","amount",
        "source_godown","destination_godown","batch_name","movement_type"))

def dedup(rows):
    seen=set(); out=[]; n=0
    for r in rows:
        k=key(r)
        if k in seen: n+=1
        else: seen.add(k); out.append(r)
    return out,n

def write_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding="utf-8")

def write_csv(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    if not rows: path.write_text("",encoding="utf-8-sig"); return
    with path.open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)

def build(a):
    sales=load(a.sales,"Sales"); sj=load(a.stock_journal,"Stock Journal")
    rows=sorted(sales+sj,key=lambda r:(r["date"],r["voucher_type"],r["voucher_number"],
                                       r["source_system"],r["source_index"]))
    rows,dups=dedup(rows)
    out=Path(a.output); out.mkdir(parents=True,exist_ok=True)
    write_json(out/"unified_inventory_movements.json",rows)
    write_csv(out/"unified_inventory_movements.csv",rows)
    types={m:sum(r["movement_type"]==m for r in rows)
           for m in sorted({r["movement_type"] for r in rows})}
    write_json(out/"inventory_validation.json",{
        "ledger_build":{"sales_input_rows":len(sales),"stock_journal_input_rows":len(sj),
        "unified_rows":len(rows),"duplicates_removed":dups,
        "movement_types":types,
        "rows_without_quantity":sum(r["quantity"] is None for r in rows)}})
    print("="*60); print("UNIFIED INVENTORY LEDGER"); print("="*60)
    print(f"Sales rows loaded         : {len(sales)}")
    print(f"Stock Journal rows loaded : {len(sj)}")
    print(f"Duplicates removed        : {dups}")
    print(f"Unified movement rows     : {len(rows)}")
    print(f"Rows without quantity     : {sum(r['quantity'] is None for r in rows)}")
    for m,n in types.items(): print(f"{m:<25}: {n}")
    print(f"\nJSON -> {out/'unified_inventory_movements.json'}")
    print(f"CSV  -> {out/'unified_inventory_movements.csv'}")

def stock(a):
    lp=Path(a.ledger)
    if not lp.exists(): raise FileNotFoundError(f"Unified ledger not found: {lp}. Run build-ledger first.")
    rows=json.loads(lp.read_text(encoding="utf-8"))
    bal=defaultdict(Decimal); warnings=[]
    for r in rows:
        item=s(r.get("stock_item")); q=dec(r.get("quantity"))
        if not item: warnings.append(("missing_stock_item",r)); continue
        if q is None: warnings.append(("quantity_unavailable",r)); continue
        unit=s(r.get("unit")); batch=s(r.get("batch_name"))
        src=s(r.get("source_godown")); dst=s(r.get("destination_godown"))
        m=s(r.get("movement_type")).upper()
        if m=="IN":
            g=dst or src
            if g: bal[(item,g,batch,unit)]+=q
            else: warnings.append(("IN_without_godown",r))
        elif m=="OUT":
            g=src or dst
            if g: bal[(item,g,batch,unit)]-=q
            else: warnings.append(("OUT_without_godown",r))
        elif m=="GODOWN_TRANSFER":
            if src and dst:
                bal[(item,src,batch,unit)]-=q
                bal[(item,dst,batch,unit)]+=q
            else: warnings.append(("transfer_missing_source_or_destination",r))
        else: warnings.append(("unknown_movement_type",r))
    detailed=[{"stock_item":i,"godown":g,"batch_name":b,"unit":u,"quantity":float(q)}
              for (i,g,b,u),q in sorted(bal.items())]
    agg=defaultdict(Decimal)
    for r in detailed: agg[(r["stock_item"],r["godown"],r["unit"])]+=Decimal(str(r["quantity"]))
    summary=[{"stock_item":i,"godown":g,"unit":u,"quantity":float(q)}
             for (i,g,u),q in sorted(agg.items())]
    out=Path(a.output)
    write_json(out/"current_stock.json",{"detailed_batch_stock":detailed,"item_godown_stock":summary})
    write_csv(out/"current_stock.csv",detailed)
    counts={x:sum(w[0]==x for w in warnings) for x in sorted({w[0] for w in warnings})}
    write_json(out/"inventory_validation.json",{"stock_calculation":{
        "ledger_rows":len(rows),"detailed_stock_keys":len(detailed),
        "item_godown_keys":len(summary),"warnings":len(warnings),
        "warning_breakdown":counts},"warnings":[r for _,r in warnings]})
    print("="*60); print("CURRENT STOCK CALCULATION"); print("="*60)
    print(f"Ledger rows            : {len(rows)}")
    print(f"Detailed stock keys    : {len(detailed)}")
    print(f"Item/godown stock keys : {len(summary)}")
    print(f"Warnings               : {len(warnings)}")
    for k,v in counts.items(): print(f"{k:<34}: {v}")
    print(f"\nJSON -> {out/'current_stock.json'}")
    print(f"CSV  -> {out/'current_stock.csv'}")

def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest="cmd",required=True)
    b=sub.add_parser("build-ledger")
    b.add_argument("--sales",default="storage/sales_batch_inventory_movements.json")
    b.add_argument("--stock-journal",default="storage/stock_journal_movements.json")
    b.add_argument("--output",default="storage")
    b.set_defaults(fn=build)
    c=sub.add_parser("calculate-stock")
    c.add_argument("--ledger",default="storage/unified_inventory_movements.json")
    c.add_argument("--output",default="storage")
    c.set_defaults(fn=stock)
    a=p.parse_args(); a.fn(a)

if __name__=="__main__": main()
