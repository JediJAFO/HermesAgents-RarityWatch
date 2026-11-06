"""Scoped baseline and opaque-cursor order sync. No network on import.

Never infer a sync boundary from lastUpdatedAt: /orders/sync uses DB order
and live responses omit dbUpdatedAt. Replay the final page's INPUT cursor.
Every changed scoped order is independently read by ID before publication.
"""
from copy import deepcopy

STAGES={'anchor','scoped_items','scoped_orders','sync','removals'}
RECONCILE_SECONDS=7*86400
INVENTORY_SECONDS=24*3600
MAX_SCOPED_ORDERS=50000
MAX_PAGES=3000

def prepare(m,s,now,*,optimize=False,retry=False):
    """Called only under the existing exclusive worker lock.

    Legacy unfinished scans resume unchanged unless migration is explicit.
    Never reset an optimized in-flight pass on a daily overlapping launch.
    """
    p=s.get('pending') or {}
    if optimize and p.get('optimization_version')!=2 and p.get('stage') not in ('complete',None):
        s['legacy_checkpoint']={'migrated_at':now,'pending':deepcopy(p),'contracts':deepcopy(s['contracts'])}
        s['pending']=new_pending(m,s,now)
    elif not p or p.get('stage')=='complete' or (retry and p.get('stage')=='blocked'):
        s['pending']=new_pending(m,s,now,reconcile=retry)
    s['schema']=2
    return s

def new_pending(m,s,now,*,reconcile=False):
    previous=s.get('snapshot') or {};checkpoint=previous.get('refresh') or {}
    comparable=previous.get('contracts')==s['contracts']
    incremental=(comparable and checkpoint.get('version')==2 and checkpoint.get('sync_cursor') and checkpoint.get('last_reconciled_at') and m.stamp(now)-m.stamp(checkpoint['last_reconciled_at'])<RECONCILE_SECONDS and not reconcile)
    p=m.new_pending(now,previous_snapshot=previous)
    p['metadata'].update(deepcopy(checkpoint.get('metadata',{})))
    p['order_metadata']=deepcopy(checkpoint.get('order_metadata',{}))
    p.update(optimization_version=2,mode='incremental' if incremental else ('scoped_reconciliation' if previous else 'scoped_baseline'),stage='sync' if incremental else 'anchor',item_ids=[],item_index=0,dirty=[],sync_anchor=None,replay_cursor=None,last_reconciled_at=checkpoint.get('last_reconciled_at') if comparable else None)
    if incremental:
        p['orders']=deepcopy(previous.get('orders',{}));p['cursor']=checkpoint['sync_cursor'];p['replay_cursor']=p['cursor']
    else:
        # Retain old item identities as discovery seeds, never their stale state.
        candidates=list(previous.get('orders',{}).values())+list((s.get('pending') or {}).get('orders',{}).values())
        p['item_ids']=sorted({o['item_id'] for o in candidates if m.asset(o.get('item_id')) and m.asset(o['item_id'])[0] in s['contracts']})
    return p

def step(m,s,p,fetch,now):
    contracts=set(s['contracts']);stage=p['stage']
    if stage=='anchor':
        response=fetch('orders/sync',{'blockchain':'POLYGON','sort':'DB_UPDATE_DESC','size':2})
        rows=response.get('orders');cursor=response.get('continuation')
        if not isinstance(rows,list) or not rows or not isinstance(cursor,str) or not cursor:raise ValueError('Cannot establish overlap sync anchor; last good retained')
        p.update(sync_anchor=cursor,replay_cursor=cursor,stage='scoped_items',cursor=None,seen=[])
    elif stage=='scoped_items':
        request={'size':1000,'filter':{'blockchains':['POLYGON'],'collections':['POLYGON:'+c for c in sorted(contracts)],'onSale':True}}
        if p['cursor']:request['continuation']=p['cursor']
        from mtd_shared_listing_source import publication_enabled
        if not p['cursor'] and publication_enabled():
            p['shared_capture_started']=True;p['shared_raw_items']=[]
        response=fetch('items/search',request);rows,done=m.advance_page(p,response,'items')
        ids=set(p['item_ids'])
        for row in rows:
            key=m.asset(row.get('id'))
            if key is None or key[0] not in contracts:raise ValueError('Scoped item search returned an invalid/out-of-scope item')
            ids.add('POLYGON:'+key[0]+':'+key[1])
        p['item_ids']=sorted(ids)
        from mtd_shared_listing_source import publication_enabled,validate_items
        if p.get('shared_capture_started'):
            p.setdefault('shared_raw_items',[]).extend(deepcopy(rows))
            if done:
                unknown=validate_items(p['shared_raw_items'],s['contracts'])
                p['shared_coverage']={c:{'complete':True,'rarity_complete':c not in unknown} for c in s['contracts']}
                p['shared_complete']=True
        if len(ids)>10000:raise ValueError('Scoped item bound reached')
        if done:p.update(stage='scoped_orders',cursor=None,seen=[],item_index=0)
    elif stage=='scoped_orders':
        if p['item_index']>=len(p['item_ids']):
            p.update(stage='sync',cursor=p['sync_anchor'],replay_cursor=p['sync_anchor'],seen=[]);return
        item=p['item_ids'][p['item_index']]
        request={'itemId':item,'platform':'RARIBLE','status':'ACTIVE','size':1000}
        if p['cursor']:request['continuation']=p['cursor']
        response=fetch('orders/sell/byItem',request);rows,done=m.advance_page(p,response,'orders')
        for row in rows:
            mt=(row.get('make') or {}).get('type') or {}
            if mt.get('@type') in ('ERC721','ERC1155') and (str(mt.get('contract','')).lower()+':'+str(mt.get('tokenId','')))!=item.lower():raise ValueError('Sell-by-item returned a different item')
            parsed=m.parse_order(row,contracts,now)
            if parsed:
                if parsed['item_id']!=item:raise ValueError('Sell-by-item returned a different item')
                p['orders'][parsed['order_id']]=parsed
        p['scanned']+=len(rows)
        if done:p.update(item_index=p['item_index']+1,cursor=None,seen=[])
    elif stage=='sync':
        cursor=p['cursor']
        if not cursor:raise ValueError('No verified opaque sync cursor; scoped reconciliation required')
        request={'blockchain':'POLYGON','sort':'DB_UPDATE_ASC','size':1000,'continuation':cursor}
        response=fetch('orders/sync',request)
        rows=response.get('orders');next_cursor=response.get('continuation')
        if not isinstance(rows,list):raise ValueError('Sync omitted orders')
        if next_cursor is not None and (not isinstance(next_cursor,str) or not next_cursor):raise ValueError('Malformed sync cursor')
        if next_cursor and (next_cursor==cursor or next_cursor in p['seen']):raise ValueError('Repeated sync cursor; last good retained')
        dirty=set(p.get('dirty',[]))
        for row in rows:
            if not isinstance(row,dict) or not row.get('id'):raise ValueError('Malformed sync order')
            mt=(row.get('make') or {}).get('type') or {}
            if row['id'] in p['orders'] or str(mt.get('contract','')).lower() in {'polygon:'+c for c in contracts}:
                dirty.add(row['id'])
        p['dirty']=sorted(dirty);p['scanned']+=len(rows);p['pages']+=1
        if rows:p['replay_cursor']=cursor
        if next_cursor:
            p['seen'].append(next_cursor);p['cursor']=next_cursor
        if not rows or not next_cursor:
            p.update(stage='removals',cursor=None,seen=[],confirm=list(p['dirty']),sync_caught_up_at=now)
            # Reconciliation omissions need an independent current-state read too.
            p['confirm']=sorted(set(p['confirm']) | (set((s.get('snapshot') or {}).get('orders',{}))-set(p['orders'])))
    elif stage=='removals':
        if p['confirm']:
            id=p['confirm'][0];row=fetch('order',{'id':id})
            if row=={'_not_found':True}:parsed=None
            else:
                if not isinstance(row,dict) or row.get('id')!=id:raise ValueError('Order confirmation identity missing/mismatched')
                if row.get('status') not in ('ACTIVE','INACTIVE','FILLED','CANCELLED') or type(row.get('cancelled')) is not bool or m.number(row.get('fill')) is None or m.number(row.get('makeStock')) is None or not isinstance(row.get('make'),dict) or not isinstance(row.get('take'),dict):raise ValueError('Malformed independent order state')
                parsed=m.parse_order(row,contracts,now)
            if parsed:p['orders'][id]=parsed
            else:p['orders'].pop(id,None)
            p['confirm'].pop(0)
        else:
            p['orders']={id:o for id,o in p['orders'].items() if not o.get('ended_at') or m.stamp(o['ended_at'])>m.stamp(now)}
            summary=m.summarize(list(p['orders'].values()),{},now)
            p['top_wallets']=[w['wallet'] for w in summary['top_wallets']]
            old=s.get('snapshot') or {};cache=(old.get('refresh') or {}).get('inventory',{})
            def comparable(o):return {k:v for k,v in o.items() if k!='verified_at'}
            changed={o['wallet'] for id,o in p['orders'].items() if comparable(o)!=comparable(old.get('orders',{}).get(id,{}))}
            changed.update(o['wallet'] for id,o in old.get('orders',{}).items() if id not in p['orders'])
            needed=[]
            for w in p['top_wallets']:
                inv=cache.get(w,{})
                fresh=(inv.get('complete') and inv.get('as_of') and 0<=m.stamp(now)-m.stamp(inv['as_of'])<INVENTORY_SECONDS)
                if p['mode']=='incremental' and w not in changed and fresh:p['inventories'][w]=deepcopy(inv)
                else:needed.append(w)
            p.update(stage='inventory',cursor=None,seen=[],confirmations_done=True,inventory_wallets=needed)
    else:raise ValueError('Unknown optimized stage')
    if len(p['orders'])>MAX_SCOPED_ORDERS or len(p.get('dirty',[]))>MAX_SCOPED_ORDERS:raise ValueError('Scoped order bound reached; last good retained')
    if p['pages']>MAX_PAGES:raise ValueError('Refresh page bound reached; last good retained')
