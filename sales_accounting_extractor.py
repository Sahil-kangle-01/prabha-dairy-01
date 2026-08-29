from pathlib import Path
import csv, json, re
import xml.etree.ElementTree as ET

CHECKPOINT_ROOT = Path('tally_extracted_data') / 'Sales_inventory'
STORAGE_ROOT = Path('storage')
OUT_JSON = STORAGE_ROOT / 'sales_accounting_transactions.json'
OUT_CSV = STORAGE_ROOT / 'sales_accounting_transactions.csv'
FAILED_JSON = STORAGE_ROOT / 'sales_accounting_failed_vouchers.json'

FIELDS = ['date','voucher_number','voucher_type','guid','master_id','alter_id','party_ledger','reference','is_invoice','ledger_name','amount','is_deemed_positive','is_party_ledger','ledger_from_item','bill_reference','bill_date','bill_type','cost_centre']

def txt(node, name):
    x = node.find(name)
    return (x.text or '').strip() if x is not None else ''

def clean_xml(text):
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F]', '', text)
    def badref(m):
        raw=m.group(1)
        try:
            cp=int(raw[1:],16) if raw.lower().startswith('x') else int(raw)
        except Exception:
            return ''
        legal = cp in (9,10,13) or 0x20 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD or 0x10000 <= cp <= 0x10FFFF
        return m.group(0) if legal else ''
    text = re.sub(r'&#(x[0-9A-Fa-f]+|[0-9]+);', badref, text)
    text = re.sub(r'<(/?)UDF:', r'<\1UDF_', text)
    return text

def amount(text):
    s=(text or '').strip().replace(',','')
    try: return float(s) if s else 0.0
    except Exception: return 0.0

def parse_file(path):
    root=ET.fromstring(clean_xml(path.read_text(encoding='utf-8', errors='replace')))
    vouchers=[]
    for v in root.iter('VOUCHER'):
        meta={
            'date':txt(v,'DATE'),'voucher_number':txt(v,'VOUCHERNUMBER'),'voucher_type':txt(v,'VOUCHERTYPENAME'),
            'guid':txt(v,'GUID') or v.attrib.get('REMOTEID',''),'master_id':txt(v,'MASTERID'),'alter_id':txt(v,'ALTERID'),
            'party_ledger':txt(v,'PARTYLEDGERNAME'),'reference':txt(v,'REFERENCE'),'is_invoice':txt(v,'ISINVOICE')
        }
        # Direct children only: actual accounting entries of this voucher.
        entries=[c for c in list(v) if c.tag=='LEDGERENTRIES.LIST']
        for le in entries:
            row=dict(meta)
            row.update({'ledger_name':txt(le,'LEDGERNAME'),'amount':amount(txt(le,'AMOUNT')),
                        'is_deemed_positive':txt(le,'ISDEEMEDPOSITIVE'),'is_party_ledger':txt(le,'ISPARTYLEDGER'),
                        'ledger_from_item':txt(le,'LEDGERFROMITEM'),'bill_reference':'','bill_date':'','bill_type':'','cost_centre':''})
            for b in list(le):
                if b.tag=='BILLALLOCATIONS.LIST':
                    name=txt(b,'NAME'); bd=txt(b,'BILLDATE'); bt=txt(b,'BILLTYPE')
                    if name: row['bill_reference']=name
                    if bd: row['bill_date']=bd
                    if bt: row['bill_type']=bt
                if b.tag=='CATEGORYALLOCATIONS.LIST':
                    for cc in b.iter('COSTCENTREALLOCATIONS.LIST'):
                        row['cost_centre']=txt(cc,'NAME')
                        if row['cost_centre']: break
            if row['ledger_name']:
                vouchers.append(row)
    return vouchers

def main():
    STORAGE_ROOT.mkdir(exist_ok=True)
    files=sorted(CHECKPOINT_ROOT.glob('Sales_*.xml'))
    print(f'Sales checkpoint XML files found : {len(files)}')
    rows=[]; failed=[]
    for i,p in enumerate(files,1):
        try: rows.extend(parse_file(p))
        except Exception as e: failed.append({'file':str(p),'error':str(e)})
        if i%250==0 or i==len(files): print(f'progress: {i}/{len(files)} (accounting_rows={len(rows)}, errors={len(failed)})')
    with OUT_JSON.open('w',encoding='utf-8') as f: json.dump(rows,f,ensure_ascii=False,indent=2)
    with OUT_CSV.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS); w.writeheader(); w.writerows(rows)
    FAILED_JSON.write_text(json.dumps(failed,ensure_ascii=False,indent=2),encoding='utf-8')
    print('\nSALES ACCOUNTING EXTRACTION COMPLETE')
    print(f'Accounting rows : {len(rows)}')
    print(f'Vouchers found  : {len(set((r["guid"],r["master_id"]) for r in rows if r["guid"] or r["master_id"]))}')
    print(f'Failed files    : {len(failed)}')
    print(f'JSON -> {OUT_JSON}')
    print(f'CSV -> {OUT_CSV}')
    print(f'Failed -> {FAILED_JSON}')

if __name__=='__main__': main()
