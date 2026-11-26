"""Normalize authenticated director snapshots; compare evidence without edits.

This module performs no network requests and never changes source workbooks,
classifies statuses, publishes records, or replaces accounting balances.
Inputs and outputs may contain confidential data: keep them outside the repo.
"""
from pathlib import Path
import argparse, hashlib, json, math, re, unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from collections import defaultdict

ACTIVE_TABS = {
    'ami': ['на 01.10.2026'],
    'krd': ['1.09 (КРД и МСК)', 'КРД 01.09.26 (+одинаковые с МСК)',
            '30.09. КРД МСК ', 'Необходимо подписать в эдо КРД'],
    'msk': ['Лист1', 'Лист2'],
    'loft': ['Лист1', 'Лист2'],
    'vas': ['Лист2'],
    'lig': ['Поставщики Ноябрь 2026'],
}



class SourceValidationError(ValueError):
    """Evidence is incomplete or ambiguous; do not publish a partial update."""

def text(v):
    return '' if v is None else str(v).strip()

def key(v):
    return re.sub(r'[^\w]+', '', unicodedata.normalize('NFKC', text(v)).upper().replace('Ё','Е'))

def money(v):
    if v in ('', None) or isinstance(v, bool): return 0.0
    if isinstance(v, (int,float)): return round(float(v),2) if math.isfinite(v) else None
    s=re.sub(r'\s+', '', text(v)).replace('р.','').replace('₽','').replace(',','.')
    try:
        n = Decimal(s)
        return float(n.quantize(Decimal('.01'))) if n.is_finite() else None
    except InvalidOperation: return None

def val(c):
    e=c.get('effectiveValue',{})
    for f in ('stringValue','numberValue','boolValue'):
        if f in e: return e[f]
    return c.get('formattedValue','')

def native(source_dir, k, source_id):
    d=json.loads((source_dir/f'{k}_cells.json').read_text(encoding='utf-8'))
    if d.get('spreadsheetId') != source_id:
        raise SourceValidationError(f'Unexpected spreadsheet identity: {k}')
    out=[]
    for s in d['sheets']:
        p=s['properties']; rr=[]
        for g in s.get('data',[]):
            for ix,row in enumerate(g.get('rowData',[])):
                cells=row.get('values',[])
                if not any(val(c) not in ('',None,False) for c in cells): continue
                rr.append({'row':g.get('startRow',0)+ix+1,'values':[val(c) for c in cells],
                    'display_values':[c.get('formattedValue',val(c)) for c in cells],
                    'colors':[c.get('effectiveFormat',{}).get('backgroundColor') for c in cells],
                    'bold':[c.get('effectiveFormat',{}).get('textFormat',{}).get('bold',False) for c in cells],
                    'notes':[c.get('note','') for c in cells],
                    'checkbox_columns':[i+1 for i,c in enumerate(cells) if c.get('dataValidation',{}).get('condition',{}).get('type')=='BOOLEAN']})
        out.append({'title':p['title'],'id':p['sheetId'],'url':d['spreadsheetUrl']+'#gid='+str(p['sheetId']),'rows':rr})
    return out

def office(source_dir, source_id):
    import openpyxl
    w=openpyxl.load_workbook(source_dir/'vas.xlsx',data_only=True)
    out=[]
    for s in w:
        rows=[]
        for r in s:
            if not any(c.value is not None for c in r):continue
            last=max(i for i,c in enumerate(r) if c.value is not None)+1
            colors=[]
            for c in r[:last]:
                fc=c.fill.fgColor
                colors.append({'type':fc.type,'value':fc.rgb if fc.type=='rgb' else fc.indexed if fc.type=='indexed' else fc.theme,'tint':fc.tint} if c.fill.patternType else None)
            values=[c.value.strftime('%d.%m.%Y') if isinstance(c.value,(date,datetime)) else c.value for c in r[:last]]
            rows.append({'row':r[0].row,'values':values,'display_values':values,'colors':colors,'bold':[bool(c.font.bold) for c in r[:last]],'notes':[c.comment.text if c.comment else '' for c in r[:last]],'checkbox_columns':[]})
        out.append({'title':s.title,'id':None,'url':f'https://docs.google.com/spreadsheets/d/{source_id}/edit','rows':rows})
    return out

def v(r,i):
    return r['values'][i] if i<len(r['values']) else ''

def comment(r,i):
    raw=v(r,i)
    if raw is None or isinstance(raw,bool):return ''
    if isinstance(raw,(int,float)):
        shown=text(r.get('display_values',[])[i]) if i<len(r.get('display_values',[])) else ''
        return shown if re.fullmatch(r'\d{1,2}\.\d{1,2}\.\d{4}',shown) else ''
    return text(raw)

def record(k,t,r,name_col,p_col=None,d_col=None,comment_cols=(),dep_col=None,edo_col=None):
    n=text(v(r,name_col))
    z={'name':n,'key':key(n),'source':k,'tab':t['title'],'row':r['row'],'url':t['url'],
       'p':money(v(r,p_col)) if p_col is not None else None,'d':money(v(r,d_col)) if d_col is not None else None,
       'comments':[comment(r,c) for c in comment_cols if comment(r,c)],
       'department':text(v(r,dep_col)) if dep_col is not None else '',
       'edo':v(r,edo_col) if edo_col is not None else None,
       'name_color':r['colors'][name_col] if name_col<len(r['colors']) else None,
       'colors':r['colors'],'notes':r['notes'],'original_values':r['values']}
    for field, col in [('p', p_col), ('d', d_col)]:
        if col is not None and z[field] is None:
            raise SourceValidationError(f'Unparseable amount: {k}/{t["title"]}/row {r["row"]}/{field}')
    return z

def parse(k,t):
    rows=[]; groups=[]; current_group=''
    for r in t['rows']:
        i=r['row']; title=t['title']
        if k=='ami':
            if i==1 or not text(v(r,0)):continue
            old=title=='на 01.06.2026'
            z=record(k,t,r,0,1,None,(5,6,8) if old else (4,5,7),2,4 if old else 3)
            z['explicit_status']=text(v(r,5 if old else 4));z['overpayment_flag']=None if old else v(r,8)
        elif k=='krd':
            if title=='Необходимо подписать в эдо КРД':
                n=text(v(r,0))
                if '·' in n and 'ИНН' in n: current_group=n.split('·')[0].strip()
                if not text(v(r,4)) or i==1:continue
                z=record(k,t,r,0,None,None,())
                z['name']=current_group;z['key']=key(current_group);z['upd_number']=text(v(r,4));z['upd_amount']=money(v(r,6));z['signed']=v(r,9);z['type']='document'
            elif title=='30.09. КРД МСК ':
                n=text(v(r,0))
                if i==1 or not n or n.startswith(('Филиал:','▸','Итого','ИТОГО','ВСЕГО')):
                    groups.append(r);continue
                z=record(k,t,r,0,1,None,(5,),2,4);z['unsigned_upd_count']=v(r,6);z['unsigned_upd_sum']=money(v(r,7));z['review_type']=text(v(r,3))
            elif title=='1.10':
                if i==1 or not text(v(r,0)):continue
                z=record(k,t,r,0,1,2,(3,));z['ambiguous_period']='Tab 1.10 but source header explicitly 01.10.2025; excluded from current selection to preserve deployed behavior.'
            else:
                if i==1 or not text(v(r,0)):continue
                old=title=='1.06'
                z=record(k,t,r,0,1 if old else 2,2 if old else 3,(6,),4 if old else 1,3 if old else 4)
        elif k=='loft':
            if i==1 or not text(v(r,0)):continue
            z=record(k,t,r,0,1,2,(3,6) if title=='Лист2' else (3,4),4 if title=='Лист2' else None)
            z['uploaded_to_accounting']=v(r,5) if title=='Лист2' else None
        elif k=='lig':
            if i==1 or not text(v(r,0)):continue
            n=text(v(r,0))
            if n in ('Поставщики','Прочее','Покупатели','Итого'):
                current_group=n;groups.append(r);continue
            is_balance=title in ('Поставщики Октябрь','Поставщики Ноябрь 2026')
            z=record(k,t,r,0,2 if is_balance else 1,1 if is_balance else 2,(4,),None,3)
            z['group']=current_group or 'Поставщики';z['include_supplier_scope']=z['group']=='Поставщики'
        elif k=='msk':
            if title=='Лист1':
                if i==1 or not text(v(r,1)):continue
                z=record(k,t,r,1,2,3,(8,),6,5);z['previous_p']=money(v(r,4));z['responsible']=text(v(r,7))
            else:
                if not text(v(r,0)):continue
                z=record(k,t,r,0,None,None,(1,2));z['side_list_text']=text(v(r,5));z['no_header']=True
        elif k=='vas':
            n=text(v(r,0))
            if title=='Лист2':
                if i<9 or n=='Итого':groups.append(r);continue
                z=record(k,t,r,0,1,None,(2,3));z['remaining_balance']=money(v(r,2)) if isinstance(v(r,2),(int,float)) else None
            else:
                if i==1 or not n:continue
                z=record(k,t,r,0,1,None,(2,3))
        rows.append(z)
    return {'title':t['title'],'url':t['url'],'records':rows,'excluded_headers_groups':groups}

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def group_id(source, tab, item):
    """A supplier evidence group; an EDO group refers to one UPD."""
    parts = [source, tab, item['key']]
    if item.get('type') == 'document':
        raw = item.get('original_values', [])
        parts.extend(['document', text(item.get('upd_number')),
                      text(raw[5]) if len(raw) > 5 else ''])
    return canonical(parts)


def record_id(source, tab, item):
    """Preserve repeated supplier evidence; disambiguate document payment legs."""
    if item.get('type') != 'document':
        return canonical([group_id(source, tab, item), item['row']])
    raw = item.get('original_values', [])
    get = lambda i: raw[i] if len(raw) > i else ''
    return canonical([group_id(source, tab, item), text(get(1)), money(get(2)),
                      text(get(3)), item.get('upd_amount'), text(get(7))])


def validate_ids(state):
    """Never collapse duplicate physical rows or duplicate document identities."""
    seen = set()
    for source, src in state['sources'].items():
        for tab, data in src['tabs'].items():
            for item in data['records']:
                if not item.get('key') or not isinstance(item.get('row'), int):
                    raise SourceValidationError(f'Missing supplier identity or row: {source}/{tab}')
                rid = record_id(source, tab, item)
                if rid in seen:
                    raise SourceValidationError(f'Duplicate record identity at {source}/{tab}/row {item["row"]}')
                seen.add(rid)


def source_structure(source, meta, raw_tabs, active_tabs):
    """Sheet structure excludes grid row capacity and changing data rows."""
    grids = []
    for s in (meta.get('sheets') or {}).get('sheets', []):
        p = s['properties']
        grids.append({'title': p['title'], 'id': p['sheetId'],
                      'hidden': p.get('hidden', False),
                      'columns': p.get('gridProperties', {}).get('columnCount')})
    if not grids:
        grids = [{'title': t['title'], 'id': t.get('id')} for t in raw_tabs]
    headers = {}
    for t in raw_tabs:
        if t['title'] not in active_tabs:
            continue
        # The second Moscow tab is a free-form supplier list without a header.
        if source == 'msk' and t['title'] == 'Лист2':
            continue
        header_rows = [r for r in t['rows'] if r['row'] == 1]
        if meta['file']['mime_type'] != 'application/vnd.google-apps.spreadsheet':
            header_rows = [r for r in t['rows'] if r['row'] in (1, 2, 6)]
        headers[t['title']] = [{'row': r['row'], 'values': r['values']} for r in header_rows]
    return {'file_id': meta['file']['id'], 'mime_type': meta['file']['mime_type'],
            'tabs': grids, 'headers': headers}


def normalize(source_dir, metadata_filename='metadata.json', captured_at=None):
    """Return the encrypted-state input schema from complete local snapshots.

    ``metadata_filename`` may be absolute or relative to ``source_dir``.
    All six configured sources and all active tabs are required. New tabs are
    retained in metadata and detected by comparison; this module does not
    silently promote a new month or infer its accounting scope.
    """
    source_dir = Path(source_dir)
    metadata_path = Path(metadata_filename)
    if not metadata_path.is_absolute():
        metadata_path = source_dir / metadata_path
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    sources = {}
    for k, titles in ACTIVE_TABS.items():
        if k not in metadata:
            raise SourceValidationError(f'Missing metadata for {k}')
        source_id = metadata[k]['file'].get('id')
        if not source_id:
            raise SourceValidationError(f'Missing metadata file identity: {k}')
        raw_tabs = office(source_dir, source_id) if k == 'vas' else native(source_dir, k, source_id)
        by_title = {t['title']: t for t in raw_tabs}
        if len(by_title) != len(raw_tabs):
            raise SourceValidationError(f'Duplicate sheet titles for {k}')
        missing = set(titles) - set(by_title)
        if missing:
            raise SourceValidationError(f'Missing active tabs for {k}: {sorted(missing)}')
        tabs = {title: parse(k, by_title[title]) for title in titles}
        structure = source_structure(k, metadata[k], raw_tabs, titles)
        sources[k] = {'file': metadata[k]['file'], 'active_tabs': list(titles), 'tabs': tabs,
                      'metadata': metadata[k], 'structure': structure,
                      # Preserve the initial handover state's canonical hash format.
                      'content_sha256': hashlib.sha256(json.dumps(tabs, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                      'structure_sha256': hashlib.sha256(canonical(structure).encode()).hexdigest()}
    state = {'schema_version': 1,
             'captured_at': captured_at or datetime.now(timezone.utc).isoformat(),
             'sources': sources}
    validate_ids(state)
    return state


def grouped(state):
    out = defaultdict(list)
    for source, src in state['sources'].items():
        for tab, data in src['tabs'].items():
            for item in data['records']:
                out[group_id(source, tab, item)].append(item)
    return out


def semantic_record(item):
    # A moved row changes provenance but not the underlying supplier evidence.
    return {k: v for k, v in item.items() if k not in ('row', 'url')}


def compare_states(previous, current):
    """Return changed/new/deleted evidence groups; never mutate either input.

    Added/deleted supplier groups, changed structure and changed groups with
    repeated supplier rows require review. The caller still validates proposed
    field/status changes and accounting balances before publishing.
    """
    validate_ids(previous)
    validate_ids(current)
    review = []
    old_sources, new_sources = previous['sources'], current['sources']
    for source in sorted(set(old_sources) | set(new_sources)):
        old, new = old_sources.get(source), new_sources.get(source)
        if old is None or new is None:
            review.append({'kind': 'source_added' if old is None else 'source_removed', 'source': source})
            continue
        if old.get('active_tabs') != new.get('active_tabs'):
            review.append({'kind': 'active_tabs_changed', 'source': source})
        if old.get('structure') != new.get('structure'):
            review.append({'kind': 'structure_changed' if old.get('structure') else 'structure_baseline_missing',
                           'source': source, 'before': old.get('structure'), 'after': new.get('structure')})
    before, after = grouped(previous), grouped(current)
    changes = []
    for gid in sorted(set(before) | set(after)):
        a, b = before.get(gid, []), after.get(gid, [])
        if sorted(canonical(semantic_record(r)) for r in a) == sorted(canonical(semantic_record(r)) for r in b):
            continue
        kind = 'added' if not a else 'deleted' if not b else 'changed'
        parts = json.loads(gid)
        fields = sorted({f for r in a+b for f in semantic_record(r)} - {'original_values'})
        changed_fields = [f for f in fields if sorted(canonical(r.get(f)) for r in a) != sorted(canonical(r.get(f)) for r in b)]
        reason = []
        if kind != 'changed':
            reason.append('new_or_deleted_supplier_or_document')
        if max(len(a), len(b)) > 1 and not any(r.get('type') == 'document' for r in a+b):
            reason.append('multiple_supplier_rows')
        if any(f in changed_fields for f in ('p', 'd', 'department', 'group', 'include_supplier_scope')):
            reason.append('amount_or_scope_changed')
        changes.append({'id': gid, 'source': parts[0], 'tab': parts[1],
                        'supplier_key': parts[2], 'kind': kind, 'before': a, 'after': b,
                        'changed_fields': changed_fields, 'requires_review': bool(reason),
                        'review_reasons': reason})
    return {'schema_version': 1, 'baseline_captured_at': previous.get('captured_at'),
            'current_captured_at': current.get('captured_at'), 'changes': changes,
            'structure_changes': review,
            'requires_review': bool(review) or any(c['requires_review'] for c in changes),
            'summary': {kind: sum(c['kind'] == kind for c in changes) for kind in ('added', 'changed', 'deleted')}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input-dir', required=True, type=Path)
    p.add_argument('--metadata', default='metadata.json')
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--previous', type=Path, help='Optional previous decrypted source state')
    p.add_argument('--changes-output', type=Path)
    args = p.parse_args()
    if bool(args.previous) != bool(args.changes_output):
        p.error('--previous and --changes-output must be supplied together')
    state = normalize(args.input_dir, args.metadata)
    changes = None
    if args.previous:
        changes = compare_states(json.loads(args.previous.read_text(encoding='utf-8')), state)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    if changes is not None:
        args.changes_output.parent.mkdir(parents=True, exist_ok=True)
        args.changes_output.write_text(json.dumps(changes, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'sources': len(state['sources']),
                      'records': sum(len(t['records']) for s in state['sources'].values() for t in s['tabs'].values()),
                      'comparison': changes['summary'] if changes is not None else None,
                      'requires_review': changes['requires_review'] if changes is not None else None}))


if __name__ == '__main__':
    main()
