"""Exact-item display enrichment. Never scans listings or confirms a generation."""
from copy import deepcopy
import mtd_listing_wallets as m

def floors(orders, now):
    best={}
    for row in orders.values():
        price=m.number(row.get('price'))
        if row.get('currency')!='POL' or price is None or not 0<price<100: continue
        if row.get('ended_at') and m.stamp(row['ended_at'])<=m.stamp(now): continue
        key=(price,int(row['token_id']),row['order_id'])
        if row['contract'] not in best or key<best[row['contract']][0]: best[row['contract']]=(key,row)
    return [v[1] for v in best.values()]

def merge_items(p, rows, response, ids, now):
    """Optional metadata only; dates require exact item/order/unit-price match."""
    items=response.get('items')
    if not isinstance(items,list): raise ValueError('Malformed metadata')
    selected={r['item_id']:r for r in rows}; seen=set()
    metadata=p.setdefault('metadata',{}); dates=p.setdefault('order_metadata',{})
    for id in ids: metadata.setdefault(id,{'name':None,'rarity':'Unknown'})
    for item in items:
        id=item.get('id')
        if id not in ids: continue
        if id in seen: raise ValueError('Duplicate metadata identity')
        seen.add(id)
        meta=item.get('meta') or {}
        rarities={str(a['value']) for a in meta.get('attributes',[]) if isinstance(a,dict) and str(a.get('key','')).lower()=='rarity' and a.get('value')}
        metadata[id]={'name':meta.get('name'),'rarity':next(iter(rarities)) if len(rarities)==1 else 'Unknown'}
        row=selected.get(id); raw=item.get('bestSellOrder')
        if row and isinstance(raw,dict): merge_order(dates,row,raw,now,'items/byIds.bestSellOrder')

def merge_order(dates,row,raw,now,provenance):
    if raw.get('id')!=row['order_id']: return False
    parsed=m.parse_order(raw,{row['contract']},now)
    if not parsed or any(parsed[k]!=row[k] for k in ('item_id','order_id','wallet','currency')) or m.number(parsed['price'])!=m.number(row['price']): return False
    evidence={k:raw.get(v) for k,v in [('created_at','createdAt'),('started_at','startedAt'),('last_updated_at','lastUpdatedAt')]}
    dates[row['order_id']]=dict(evidence,item_id=row['item_id'],price=row['price'],provenance=provenance,captured_at=now)
    return True

def missing(rows,p,now):
    from mtd_under100_sniper import listing_date
    result=[]
    for r in rows:
        meta=p.get('metadata',{}).get(r['item_id'],{})
        evidence=p.get('order_metadata',{}).get(r['order_id'],{})
        exact=evidence.get('item_id')==r['item_id'] and m.number(evidence.get('price'))==m.number(r['price'])
        if not meta.get('rarity') or meta['rarity']=='Unknown' or not listing_date(evidence if exact else r,now): result.append(r['item_id'])
    return sorted(set(result))

def enrich(path=m.STORE, *, catalog_path=m.ROOT/'mcfarlane-dc-sales.json', capture=None, fetch=None, now=None):
    """One bounded metadata pass under the exact producer worker byte lock.

    Captures can satisfy current IDs without another request; changed IDs get
    one items/byIds attempt, then only unresolved exact orders get one lookup.
    Existing snapshot timestamps, order rows and fingerprints remain untouched.
    """
    from pathlib import Path
    from mtd_under100_sniper import lease, Busy, inventory, listing_date
    path=Path(path); now=now or m.utc()
    try:
        with lease(path.with_suffix('.worker.lock')):
            s=m.read(path)
            observed=inventory(s,m.approved_scope(catalog_path),now)
            rows=observed['rows']; snap=s['snapshot']; refresh=snap['refresh']
            p={'metadata':deepcopy(refresh.get('metadata',{})),'order_metadata':deepcopy(refresh.get('order_metadata',{}))}
            if capture is not None:
                if not 0<=m.stamp(capture['captured_at'])<=m.stamp(now): raise ValueError('Invalid capture clock')
                requested=capture['requested_ids']
                if not isinstance(requested,list) or len(set(requested))!=len(requested): raise ValueError('Invalid capture IDs')
                ids=set(requested)&{r['item_id'] for r in rows}
                merge_items(p,rows,capture['response'],ids,capture['captured_at'])
            needed=missing(rows,p,now)
            calls=0
            if needed:
                fetch=fetch or m.Provider()
                for offset in range(0,len(needed),50):
                    ids=needed[offset:offset+50]
                    merge_items(p,rows,fetch('items/byIds',{'ids':ids}),ids,now); calls+=1
                for row in rows:
                    evidence=p['order_metadata'].get(row['order_id'],{})
                    if not listing_date(evidence,now):
                        raw=fetch('order',{'id':row['order_id']}); calls+=1
                        merge_order(p['order_metadata'],row,raw,now,'order')
            refresh.update(p)
            # Completed pending is a cache, not a new observation or confirmation.
            s['pending']['metadata']=deepcopy(p['metadata'])
            s['pending']['order_metadata']=deepcopy(p['order_metadata'])
            m.atomic(path,s)
            return {'status':'enriched','count':len(rows),'calls':calls,'unresolved':len(missing(rows,p,now))}
    except Busy: return {'status':'busy'}

if __name__=='__main__':
    import argparse,json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fill',action='store_true',help='Explicit metadata-only fill; never listing scan')
    parser.add_argument('--capture',type=m.Path)
    args=parser.parse_args()
    if not args.fill: parser.error('--fill required')
    print(json.dumps(enrich(capture=m.read(args.capture) if args.capture else None)))
