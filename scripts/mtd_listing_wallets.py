"""Saved-only MTD listing analytics; deterministic collection is explicit CLI only.

Official provider schema: rarible/protocol-mcp openapi.json, /orders/all.
Never touches saved sales. Counts identities, not orders or ERC1155 units.
"""
from __future__ import annotations
import json, re, os, time, hashlib, tempfile
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from collections import defaultdict
HOME=Path(__file__).resolve().parents[1]
ROOT=HOME/'price-watches'
STORE=ROOT/'mtd-listing-wallets.json'
ADDRESS=re.compile(r'0x[0-9a-f]{40}')
def utc(): return datetime.now(timezone.utc).isoformat()
def stamp(value):
    if not value: return None
    d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('Missing timezone')
    return d.timestamp()
def number(value):
    try:
        d=Decimal(str(value))
        return d if d.is_finite() else None
    except (InvalidOperation,TypeError,ValueError): return None
def wallet(value):
    s=str(value or '').split(':')[-1].lower()
    return s if ADDRESS.fullmatch(s) else None
def asset(value):
    pieces=str(value or '').split(':')
    if len(pieces)!=3 or pieces[0]!='POLYGON' or not ADDRESS.fullmatch(pieces[1].lower()) or not re.fullmatch('[0-9]+',pieces[2]): return None
    return pieces[1].lower(),pieces[2]
def parse_order(row,contracts,as_of):
    """Allow only plain, public Rarible fixed-price sell orders; fail closed."""
    if not isinstance(row,dict): raise ValueError('Malformed order row')
    make=row.get('make') or {}; take=row.get('take') or {}; mt=make.get('type') or {}; tt=take.get('type') or {}
    contract=str(mt.get('contract') or '').lower()
    if not contract.startswith('polygon:'): return None
    contract=contract.split(':')[-1]
    if contract not in contracts or mt.get('@type') not in ('ERC721','ERC1155'): return None
    if row.get('status')!='ACTIVE' or row.get('cancelled') is not False: return None
    # Other protocols/auction routes are deliberately unsupported, not mislabelled.
    data=row.get('data') or {}
    if row.get('platform','RARIBLE')!='RARIBLE' or data.get('@type') not in ('ETH_RARIBLE_V2','ETH_RARIBLE_V2_2','ETH_RARIBLE_V2_3','ETH_RARIBLE_V2_DATA_V3_SELL'): return None
    if data.get('auction') or data.get('isAuction') or data.get('startPrice') or data.get('endPrice'): return None
    taker=wallet(row.get('taker'))
    if row.get('taker') and taker!='0x'+'0'*40: return None
    try:
        now=stamp(as_of);start=stamp(row.get('startedAt'));end=stamp(row.get('endedAt'))
    except (ValueError,TypeError): return None
    if (start is not None and start>now) or (end is not None and end<=now): return None
    seller=wallet(row.get('maker')); token=str(mt.get('tokenId',''))
    quantity=number(make.get('value'));stock=number(row.get('makeStock'));price=number(take.get('value'))
    if not seller or not re.fullmatch('[0-9]+',token) or not row.get('id'): raise ValueError('Malformed in-scope order identity')
    if quantity is None or stock is None or price is None or quantity<=0 or stock<=0 or price<=0: return None
    if quantity!=quantity.to_integral_value() or stock!=stock.to_integral_value(): return None
    fill=number(row.get('fill'))
    if fill is None or fill<0:return None
    fill_limit=quantity if data.get('isMakeFill') is True or data.get('@type')=='ETH_RARIBLE_V2_DATA_V3_SELL' else price
    if fill>=fill_limit:return None
    currency=None
    if tt.get('@type') in ('ETH','CURRENCY_NATIVE') and tt.get('blockchain')=='POLYGON':currency='POL'
    if tt.get('@type')=='ERC20' and str(tt.get('contract','')).startswith('POLYGON:'):
        currency=tt['contract'] # Exact currency identity; do not guess ticker.
    if not currency:return None
    return dict({key:row[raw] for key,raw in [('created_at','createdAt'),('started_at','startedAt'),('last_updated_at','lastUpdatedAt')] if row.get(raw)},order_id=row['id'],wallet=seller,contract=contract,token_id=token,item_id='POLYGON:'+contract+':'+token,
        units=str(min(quantity,stock)),currency=currency,price=format(price/quantity,'f'),verified_at=as_of,ended_at=row.get('endedAt'))
def summarize(orders,inventories,as_of):
    bywallet=defaultdict(dict)
    for o in orders:
        if o.get('ended_at') and stamp(o['ended_at'])<=stamp(as_of):continue
        key=(o['contract'],o['token_id']);bywallet[o['wallet']].setdefault(key,[]).append(o)
    ranked=sorted(bywallet,key=lambda w:(-len(bywallet[w]),-len({k[0] for k in bywallet[w]}),w))
    top=[]
    for w in ranked[:5]:
        items=[]
        for key,asks in sorted(bywallet[w].items()):
            best={}
            for ask in asks:
                curr=ask['currency']
                if curr not in best or (Decimal(ask['price']),ask['order_id'])<(Decimal(best[curr]['price']),best[curr]['order_id']):best[curr]=ask
            items.append(dict(contract=key[0],token_id=key[1],item_id=asks[0]['item_id'],asks=[best[c] for c in sorted(best)],verified_at=max(a['verified_at'] for a in asks)))
        inv=inventories.get(w,{})
        owned=inv.get('count') if inv.get('complete') else None
        listed=len(items); percent=None
        if owned and listed<=owned and all(i['item_id'] in inv.get('items',[]) for i in items):percent=format(Decimal(listed)*100/owned,'.1f')
        top.append(dict(wallet=w,listed_nfts=listed,collection_count=len({k[0] for k in bywallet[w]}),owned_nfts=owned,listed_percent=percent,inventory_complete=bool(inv.get('complete')),inventory_verified_at=inv.get('as_of'),items=items))
    # Same expiry-filtered identities as the global total, across ALL makers.
    bycollection=defaultdict(set)
    for values in bywallet.values():
        for contract,token in values:bycollection[contract].add(token)
    collections=[dict(contract=c,listed_nfts=len(bycollection[c])) for c in sorted(bycollection,key=lambda c:(-len(bycollection[c]),c))[:5]]
    return dict(as_of=as_of,listed_nfts=sum(len(tokens) for tokens in bycollection.values()),wallet_count=len(bywallet),top_wallets=top,top_collections=collections)

def atomic(path,data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    encoded=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    if len(encoded)>40_000_000:raise ValueError('Bounded listing store limit reached; last good retained')
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(encoded);stream.flush();os.fsync(stream.fileno())
        for attempt in range(3):
            try:os.replace(tmp,path);break
            except OSError as exc:
                if attempt==2 or getattr(exc,'winerror',None) not in (5,32,33):raise
                time.sleep(.05*(attempt+1))
    finally:
        Path(tmp).unlink(missing_ok=True)
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def new_pending(now,previous_snapshot=None):
    cache={}
    for w in (previous_snapshot or {}).get('top_wallets',[]):
        for item in w.get('items',[]):
            if item.get('item_id') and item.get('item_name') and item.get('rarity') not in (None,'Unknown'):
                cache[item['item_id']]={'name':item['item_name'],'rarity':item['rarity']}
    return dict(stage='orders',started_at=now,updated_at=now,cursor=None,seen=[],pages=0,calls=0,scanned=0,orders={},inventories={},ownerships={},metadata=cache,retry_count=0,next_at=0)
def new_state(contracts,now):
    return dict(schema=1,contracts=contracts,scope_hash=hashlib.sha256(json.dumps(sorted(contracts)).encode()).hexdigest(),snapshot=None,pending=new_pending(now))
def inventory_rows(rows,contracts,wallets):
    found={w:set() for w in wallets}
    for row in rows:
        if not isinstance(row,dict):raise ValueError('Malformed ownership row')
        key=asset(row.get('itemId')); owner=wallet(row.get('owner')); value=number(row.get('value'))
        if key and key[0] in contracts and owner in found and value is not None and value>0 and value==value.to_integral_value() and (row.get('pending') is False or row.get('pending')==[]):
            found[owner].add('POLYGON:'+key[0]+':'+key[1])
    return found
def advance_page(p,response,key):
    rows=response.get(key)
    if not isinstance(rows,list):raise ValueError('Provider omitted '+key+' array')
    cursor=response.get('continuation')
    if cursor is not None and (not isinstance(cursor,str) or not cursor):raise ValueError('Malformed continuation')
    if cursor and cursor in p['seen']:raise ValueError('Repeated continuation; incomplete scan not published')
    if cursor:p['seen'].append(cursor)
    p['cursor']=cursor;p['pages']+=1
    if p['pages']>3000:raise ValueError('Daily page bound reached; incomplete scan not published')
    # A short page never terminates pagination. A contradictory empty cursor page
    # is not accepted as exhaustion: it must be followed until cursor absent.
    return rows,not cursor

def tick(path,fetch,*,budget=12,now=None):
    """One bounded pass. The --run loop consumes successive passes without LLMs."""
    supplied_now=now is not None
    now=now or utc();s=read(path);p=s.get('pending');contracts=set(s['contracts'])
    if not p or p['stage'] in ('blocked','complete') or p.get('next_at',0)>stamp(now):return s
    raw_fetch=fetch
    def fetch(endpoint,body):
        from mtd_nft_history_provider import ProviderDeferred
        try:response=raw_fetch(endpoint,body)
        except ProviderDeferred:raise
        except Exception:
            p['calls']+=1;raise
        p['calls']+=1
        return response
    for _ in range(budget):
        stage=p['stage']
        try:
            from mtd_listing_refresh import STAGES,step
            if p.get('optimization_version')==2 and stage in STAGES:
                import sys
                step(sys.modules[__name__],s,p,fetch,now)
            elif stage=='orders':
                request=dict(blockchains='POLYGON',status='ACTIVE',size=1000,sort='LAST_UPDATE_DESC')
                if p['cursor']:request['continuation']=p['cursor']
                response=fetch('orders/all',request);rows,done=advance_page(p,response,'orders')
                for row in rows:
                    parsed=parse_order(row,contracts,now)
                    if parsed:p['orders'][parsed['order_id']]=parsed
                p['scanned']+=len(rows)
                if len(p['orders'])>50000:raise ValueError('Scoped order bound reached')
                if done:
                    summary=summarize(list(p['orders'].values()),{},now)
                    p['top_wallets']=[w['wallet'] for w in summary['top_wallets']]
                    p['stage']='inventory';p['cursor']=None;p['seen']=[]
            elif stage=='inventory':
                inventory_wallets=p.get('inventory_wallets',p['top_wallets'])
                if not inventory_wallets:p['stage']='metadata';atomic(path,s);continue
                request={'size':1000,'filter':{'blockchains':['POLYGON'],'owners':['ETHEREUM:'+w for w in inventory_wallets],'collections':['POLYGON:'+c for c in sorted(contracts)]}}
                if p['cursor']:request['continuation']=p['cursor']
                response=fetch('ownerships/search',request);rows,done=advance_page(p,response,'ownerships')
                found=inventory_rows(rows,contracts,set(inventory_wallets))
                partial=set(p.get('inventory_partial',[]))
                for row in rows:
                    owner=wallet(row.get('owner'));key=asset(row.get('itemId'));value=number(row.get('value'))
                    if owner not in inventory_wallets or key is None:
                        partial.update(inventory_wallets)
                    elif key[0] in contracts and (value is None or value<0 or value!=value.to_integral_value() or not (row.get('pending') is False or row.get('pending')==[])):
                        partial.add(owner)
                p['inventory_partial']=sorted(partial)
                for w,items in found.items():p['ownerships'][w]=sorted(set(p['ownerships'].get(w,[]))|items)
                if done:
                    p['inventories'].update({w:dict(complete=w not in partial,count=len(p['ownerships'].get(w,[])),items=p['ownerships'].get(w,[]),as_of=now) for w in inventory_wallets})
                    p['stage']='metadata';p['cursor']=None;p['seen']=[]
            elif stage=='metadata':
                from mtd_listing_details import floors,missing,merge_items
                representatives=floors(p['orders'],now)
                needed={o['item_id'] for o in p['orders'].values() if o['wallet'] in p['top_wallets'] and o['item_id'] not in p['metadata']}
                needed.update(missing(representatives,p,now))
                ids=sorted(needed-set(p.get('details_attempted',[])))
                if ids:
                    ids=ids[:50];response=fetch('items/byIds',{'ids':ids})
                    merge_items(p,representatives,response,ids,now)
                    p['details_attempted']=sorted(set(p.get('details_attempted',[]))|set(ids))
                else:
                    previous=(s.get('snapshot') or {}).get('orders',{})
                    p['confirm']=[] if p.get('confirmations_done') else sorted(set(previous)-set(p['orders']));p['stage']='confirm';atomic(path,s);continue
            elif stage=='shared_items':
                from mtd_shared_listing_source import validate_items
                request={'size':1000,'filter':{'blockchains':['POLYGON'],'collections':['POLYGON:'+c for c in sorted(contracts)],'onSale':True}}
                if p['cursor']:request['continuation']=p['cursor']
                response=fetch('items/search',request);rows,done=advance_page(p,response,'items')
                p['shared_raw_items'].extend(rows)
                if done:
                    unknown=validate_items(p['shared_raw_items'],s['contracts'])
                    p['shared_coverage']={c:{'complete':True,'rarity_complete':c not in unknown} for c in s['contracts']}
                    p.update(shared_complete=True,stage='confirm',cursor=None,seen=[])
            elif stage=='confirm':
                from mtd_shared_listing_source import publication_enabled
                if publication_enabled() and not p.get('confirm') and not p.get('shared_complete'):
                    p.update(stage='shared_items',cursor=None,seen=[],shared_raw_items=[]);atomic(path,s);continue
                if p['confirm']:
                    id=p['confirm'][0];response=fetch('order',{'id':id})
                    if response and parse_order(response,contracts,now):raise ValueError('A removed listing remains active on independent recheck; retry full scan')
                    p['confirm'].pop(0)
                else:
                    if not supplied_now:now=utc()
                    p['orders']={id:o for id,o in p['orders'].items() if not o.get('ended_at') or stamp(o['ended_at'])>stamp(now)}
                    snapshot=summarize(list(p['orders'].values()),p['inventories'],now)
                    for w in snapshot['top_wallets']:
                        for item in w['items']:
                            meta=p['metadata'].get(item['item_id'],{})
                            item.update(item_name=meta.get('name') or s['contracts'][item['contract']],rarity=meta.get('rarity','Unknown'))
                    snapshot.update(snapshot_id=hashlib.sha256(json.dumps(p['orders'],sort_keys=True).encode()).hexdigest()[:20],started_at=p['started_at'],contracts=s['contracts'],orders=p['orders'],scanned_orders=p['scanned'],pages=p['pages'])
                    if p.get('optimization_version')==2:
                        if not p.get('replay_cursor'):raise ValueError('Optimized completion missing sync checkpoint')
                        snapshot['refresh']={'version':2,'sync_cursor':p['replay_cursor'],'last_reconciled_at':now if p['mode']!='incremental' else p['last_reconciled_at'],'mode':p['mode'],'sync_started_at':p['started_at'],'sync_caught_up_at':p.get('sync_caught_up_at'),'inventory':p['inventories'],'metadata':p['metadata'],'order_metadata':p.get('order_metadata',{})}
                    if p.get('shared_complete'):
                        snapshot['shared_listings']={'version':1,'started_at':p['started_at'],'observed_at':now,'complete':True,'contracts':dict(s['contracts']),'items':p['shared_raw_items'],'coverage':p['shared_coverage']}
                    s['snapshot']=snapshot;p['stage']='complete';p['updated_at']=now;atomic(path,s);return s
            else:raise ValueError('Unknown collection stage')
            p['retry_count']=0;p['next_at']=0;p.pop('error',None)
        except Exception as exc:
            from mtd_nft_history_provider import ProviderDeferred,ProviderError
            if isinstance(exc,ProviderDeferred):p['next_at']=max(stamp(now)+1,exc.ready_at if hasattr(exc,'ready_at') else stamp(now)+15)
            elif isinstance(exc,ProviderError) and exc.status==400 and p['retry_count']<3:
                p['retry_count']+=1;p['next_at']=stamp(now)+300;p['error']='HTTP 400; bounded retry '+str(p['retry_count'])+'/3 after five minutes'
            elif p.get('optimization_version')==2 and p.get('mode')=='incremental' and stage=='sync' and ((isinstance(exc,ProviderError) and exc.status==400 and exc.invalid_cursor) or (isinstance(exc,ValueError) and 'Repeated sync cursor' in str(exc))):
                from mtd_listing_refresh import new_pending as optimized_pending
                s['recovery_checkpoint']={'reason':'Sync cursor unavailable; scoped reconciliation required','pending':p}
                s['pending']=optimized_pending(__import__(__name__),s,now,reconcile=True);p=s['pending'];p['recovery_reason']='Sync cursor unavailable; scoped reconciliation (no Polygon full scan)'
            else:
                p['stage']='blocked';p['error']='Collection stopped: '+(f'HTTP {exc.status}' if isinstance(exc,ProviderError) else type(exc).__name__+': '+str(exc)[:180])
            p['updated_at']=now;atomic(path,s);return s
        p['updated_at']=now;atomic(path,s)
    return s

class Provider:
    """Reuse the established shared Rarible request lease/pacing and outcomes.

    Route bridge changes only the adapter's outgoing request before transport;
    all locking, secure credential lookup and completion bookkeeping stay owned
    by the existing tested adapter. No request goes to the placeholder route.
    """
    def __init__(self,root=ROOT,*,key=None,opener=None):
        from mtd_nft_history_provider import RaribleAdapter
        self.root=Path(root);self.key=key;self.opener=opener;self.adapter=RaribleAdapter(root,key=key,min_gap_seconds=15)
    def __call__(self,endpoint,body):
        import urllib.request,urllib.parse,urllib.error
        from mtd_nft_history_provider import BASE
        if endpoint not in ('orders/all','orders/sell','orders/sync','orders/sell/byItem','items/search','order','ownerships/search','items/byIds'):raise ValueError('Unsupported listing endpoint')
        if endpoint=='orders/sync':
            if set(body)-{'blockchain','sort','size','continuation'} or body.get('blockchain')!='POLYGON' or body.get('sort') not in ('DB_UPDATE_ASC','DB_UPDATE_DESC'):raise ValueError('Unsupported sync scope')
        if endpoint=='orders/sell/byItem':
            if set(body)-{'itemId','platform','status','size','continuation'} or asset(body.get('itemId')) is None or body.get('platform')!='RARIBLE' or body.get('status')!='ACTIVE':raise ValueError('Unsupported item sell scope')
        if endpoint=='items/search':
            f=body.get('filter') or {}
            if set(body)-{'size','filter','continuation'} or set(f)!={'blockchains','collections','onSale'} or f.get('blockchains')!=['POLYGON'] or f.get('onSale') is not True or not f.get('collections') or any(not re.fullmatch(r'POLYGON:0x[0-9a-f]{40}',c) for c in f['collections']):raise ValueError('Unsupported scoped item search')
        if endpoint=='order' and not re.fullmatch(r'POLYGON:0x[0-9a-fA-F]{64}',body.get('id','')):raise ValueError('Malformed order identifier')
        if endpoint=='orders/all':
            if set(body)-{'blockchains','status','size','sort','continuation'} or body.get('blockchains')!='POLYGON' or body.get('status')!='ACTIVE':raise ValueError('Unsupported order scan scope')
        if endpoint=='orders/sell':
            if set(body)-{'blockchains','platform','size','continuation'} or body.get('blockchains')!='POLYGON' or body.get('platform')!='RARIBLE':raise ValueError('Unsupported sell scan scope')
        def route(request,timeout):
            if request.full_url!=BASE+'ownerships/search':raise ValueError('Unexpected adapter route')
            if endpoint in ('orders/all','orders/sell','orders/sync','orders/sell/byItem'):url=BASE+endpoint+'?'+urllib.parse.urlencode(body);data=None;method='GET'
            elif endpoint=='order':url=BASE+'orders/'+urllib.parse.quote(body['id'],safe=':');data=None;method='GET'
            else:url=BASE+endpoint;data=json.dumps(body).encode();method='POST'
            actual=urllib.request.Request(url,data=data,method=method,headers=dict(request.header_items()))
            try:return (self.opener or urllib.request.urlopen)(actual,timeout=timeout)
            except urllib.error.HTTPError as exc:
                if endpoint=='order' and exc.code==404:
                    import io
                    return io.BytesIO(b'{"_not_found":true}')
                raise
        self.adapter.opener=route
        return self.adapter('ownerships/search',{})

def approved_scope(path=ROOT/'mcfarlane-dc-sales.json'):
    data=read(path);result={}
    for c in data.get('collections',[]):
        id=c.get('id','');contract=c.get('contract')
        if not isinstance(contract,str) or not ADDRESS.fullmatch(contract.lower()) or id.lower()!='polygon:'+contract.lower():raise ValueError('Malformed approved collection')
        result[contract.lower()]=str(c.get('display_name') or c.get('name') or 'MTD collection')
    if not result:raise ValueError('Empty approved scope')
    return result

def run(path=STORE,*,seconds=21600,budget=12,retry=False,optimize=False):
    """Finite automatic continuation; every request shares a durable >=15s gap.

    Daily launcher invokes --run. Interrupted passes resume their exact cursor.
    Exhausted daily jobs start fresh; blocked jobs require review/--retry.
    """
    import msvcrt
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with open(path.with_suffix('.worker.lock'),'a+b') as lock:
        if lock.tell()==0:lock.write(b'0');lock.flush()
        lock.seek(0)
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:return {'status':'already_running'}
        from mtd_listing_refresh import prepare,new_pending as optimized_pending
        import sys
        module=sys.modules[__name__]
        scope=approved_scope();now=utc()
        if not path.exists():
            s=new_state(scope,now);s['pending']=optimized_pending(module,s,now)
        else:
            s=read(path)
            if s['contracts']!=scope:
                s['recovery_checkpoint']={'reason':'Approved scope changed','pending':s.get('pending'),'contracts':s['contracts']}
                s['contracts']=scope;s['scope_hash']=hashlib.sha256(json.dumps(sorted(scope)).encode()).hexdigest();s['pending']=optimized_pending(module,s,now,reconcile=True)
        prepare(module,s,now,optimize=optimize,retry=retry);atomic(path,s)
        provider=Provider();deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            s=tick(path,provider,budget=budget)
            p=s['pending']
            if p['stage'] in ('complete','blocked'):return {'status':p['stage'],'pages':p['pages'],'calls':p['calls'],'error':p.get('error')}
            delay=max(1,min(30,p.get('next_at',0)-time.time()))
            time.sleep(min(delay,max(0,deadline-time.monotonic())))
        return {'status':'checkpointed','pages':p['pages'],'calls':p['calls']}

def saved_view(path=STORE,aliases=None,*,now=None):
    """Saved-only public projection; never exposes exact wallet/order identities."""
    import importlib.util
    try:s=read(path)
    except (OSError,ValueError):return {'status':'No completed listing snapshot yet. Initial collection has not saved readable progress.','top_wallets':[],'top_collections':[],'collections_complete':False}
    p=s.get('pending') or {};snap=s.get('snapshot')
    view={'status':p.get('stage','unknown'),'pages':p.get('pages',0),'calls':p.get('calls',0),'scanned':p.get('scanned',0),'scoped_orders_so_far':len(p.get('orders',{})), 'updated_at':p.get('updated_at'),'contract_count':len(s.get('contracts',{})),'top_wallets':[],'complete':False}
    view.update(mode=p.get('mode','legacy baseline (checkpoint retained)'),discovered_items=len(p.get('item_ids',[])),items_completed=p.get('item_index',0))
    if p.get('error'):view['collection_notice']='Collection blocked; last complete snapshot retained.' if p['stage']=='blocked' else 'Provider retry waiting; incomplete results not published.'
    view.update(top_collections=[],collections_complete=False)
    if not snap:return view
    if 'orders' in snap:
        # Local clock expiry is deterministic: no transport, no saved-store write.
        effective_now=now or utc();refresh=snap.get('refresh') or {}
        inventory=refresh.get('inventory') or {w['wallet']:{'complete':w.get('inventory_complete',False),'count':w.get('owned_nfts'),'items':[i['item_id'] for i in w.get('items',[])],'as_of':w.get('inventory_verified_at')} for w in snap.get('top_wallets',[])}
        metadata=dict(refresh.get('metadata') or {})
        for party in snap.get('top_wallets',[]):
            for item in party.get('items',[]):metadata.setdefault(item['item_id'],{'name':item.get('item_name'),'rarity':item.get('rarity','Unknown')})
        projection=summarize(list(snap['orders'].values()),inventory,effective_now)
        view['top_collections']=[dict(row,collection_name=(snap.get('contracts') or {}).get(row['contract']) or 'Unknown collection') for row in projection['top_collections']]
        view['collections_complete']=True
        for party in projection['top_wallets']:
            for item in party['items']:
                meta=metadata.get(item['item_id'],{});item.update(item_name=meta.get('name') or snap['contracts'].get(item['contract'],'MTD collection'),rarity=meta.get('rarity','Unknown'))
        snap=dict(snap,listed_nfts=projection['listed_nfts'],wallet_count=projection['wallet_count'],top_wallets=projection['top_wallets'])
        view['expiry_evaluated_at']=effective_now
    view.update({k:snap.get(k) for k in ('as_of','started_at','snapshot_id','listed_nfts','wallet_count')});view['complete']=True;view['contract_count']=len(snap.get('contracts',{}))
    view['stale']=(stamp(now or utc())-stamp(snap['as_of']))>36*3600
    checkpoint=snap.get('refresh') or {}
    view.update(sync_caught_up_at=checkpoint.get('sync_caught_up_at'),last_reconciled_at=checkpoint.get('last_reconciled_at'))
    spec=importlib.util.spec_from_file_location('_listing_shared_alias',HOME/'price-watches/mcfarlane_wallet_aliases.py');aliasmod=importlib.util.module_from_spec(spec);spec.loader.exec_module(aliasmod)
    for party in snap.get('top_wallets',[]):
        w=party['wallet'];public={k:party.get(k) for k in ('listed_nfts','collection_count','owned_nfts','listed_percent','inventory_complete','inventory_verified_at')}
        public['display']=aliasmod.wallet_label(w,aliases or {});public['items']=[]
        for item in party.get('items',[]):
            row={k:item.get(k) for k in ('item_name','token_id','rarity','verified_at')}
            row['asks']=[{k:a.get(k) for k in ('price','currency','units')} for a in item.get('asks',[])];public['items'].append(row)
        view['top_wallets'].append(public)
    return view

def render_section(view,time_label):
    import html
    view=view or {};esc=lambda v:html.escape(str(v if v is not None else 'Unknown'))
    fmt=lambda v:format(int(v),',') if isinstance(v,int) else 'Unknown'
    amount=lambda v:format(number(v),',f') if number(v) is not None else 'Unknown'
    intro=(f'<strong class="hero">{fmt(view.get("listed_nfts"))} distinct NFTs currently listed</strong><p class="note">Across {fmt(view.get("wallet_count"))} listing wallets · completed scanned scope. Scan interval (ET): {esc(time_label(view.get("started_at")))} to {esc(time_label(view.get("as_of")))}.</p>' if view.get('complete') else '<strong class="hero">Listed total unavailable — initial scan not complete</strong>')
    status=f'<p class="note" role="status">Collection: {esc(view.get("status","not started"))} · {fmt(view.get("pages",0))} pages · {fmt(view.get("scanned",0))} provider orders examined · {fmt(view.get("scoped_orders_so_far",0))} qualifying scoped orders saved so far (not an NFT total). Last progress (ET): {esc(time_label(view.get("updated_at")))}.</p>'
    status+=f'<p class="note">Collection mode: {esc(view.get("mode","unknown"))} · scoped items checked: {fmt(view.get("items_completed",0))} / {fmt(view.get("discovered_items",0))}. Sync coverage (ET): {esc(time_label(view.get("sync_caught_up_at")))}. Last scoped reconciliation (ET): {esc(time_label(view.get("last_reconciled_at")))}. Local expiry evaluated (ET): {esc(time_label(view.get("expiry_evaluated_at")))}.</p>'
    if view.get('collection_notice'):status+='<p role="alert">'+esc(view['collection_notice'])+'</p>'
    if view.get('stale'):status+='<p role="alert">Stale: the last complete snapshot is over 36 hours old; no current-market completeness is implied.</p>'
    cards=[]
    for rank,w in enumerate(view.get('top_wallets',[]),1):
        owned=fmt(w.get('owned_nfts'));percent=esc(w.get('listed_percent'))
        summary=f'{esc(w.get("display"))} — {fmt(w.get("listed_nfts"))} NFTs listed · {fmt(w.get("collection_count"))} collections · {owned} owned · '+(percent+'% listed' if w.get('listed_percent') is not None else 'Unknown % listed')
        items=[]
        for item in w.get('items',[]):
            asks='; '.join(amount(a.get('price'))+' '+esc(a.get('currency'))+' / unit (available units: '+amount(a.get('units'))+')' for a in item.get('asks',[])) or 'Unknown ask'
            items.append('<li><strong>'+esc(item.get('item_name') or 'Unknown item')+'</strong><span>Token ID: '+esc(item.get('token_id'))+' · Rarity: '+esc(item.get('rarity') or 'Unknown')+'</span><span>Ask: '+asks+'</span><span>Last verified (ET): '+esc(time_label(item.get('verified_at')))+'</span></li>')
        cards.append(f'<details class="rank-card"><summary class="rank-summary"><span class="rank">#{rank}</span><span>{summary}</span></summary><p class="rule">Current scoped inventory verified (ET): {esc(time_label(w.get("inventory_verified_at")))}. Percentage is withheld unless complete confirmed holdings contain every listed NFT.</p><ol class="nft-list">'+''.join(items)+'</ol></details>')
    collections=''.join('<li><strong>'+esc(c.get('collection_name') or 'Unknown collection')+'</strong><span>'+fmt(c.get('listed_nfts'))+' distinct NFTs listed</span></li>' for c in view.get('top_collections',[])[:5])
    collection_body=('<ol class="listed-collections">'+collections+'</ol>' if collections else '<p class="note">'+('No qualifying distinct NFTs in this saved snapshot.' if view.get('collections_complete') else 'Collection ranking unavailable — no complete saved order snapshot.')+'</p>')
    return ('<section id="listing-wallets" class="monthly-history" style="overflow-wrap:anywhere" aria-labelledby="listing-heading"><h2 id="listing-heading">MTD Listings — Fixed Approved Scope</h2>'
        +'<style>.listing-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin:16px 0}.listing-panel{min-width:0;border:1px solid #334155;border-radius:8px;padding:12px}.listing-panel h3{margin:0 0 12px;font-size:1.05rem}.listed-collections{margin:0;padding-left:24px}.listed-collections li{padding:9px 0;border-top:1px solid #334155}.listed-collections li:first-child{border-top:0}.listed-collections strong,.listed-collections span{display:block;overflow-wrap:anywhere}.listed-collections span{color:#cbd5e1;margin-top:4px}@media(max-width:800px){.listing-grid{grid-template-columns:minmax(0,1fr)}.listing-panel{padding:9px}}</style>'
        +'<p class="note">All '+fmt(view.get('contract_count'))+' approved MTD Polygon collections · All rarities · independent of Number of days. Saved-only dashboard; deterministic token-free worker, no notifications.</p>'+intro+status
        +'<div class="listing-grid"><section class="listing-panel" aria-labelledby="listing-wallet-ranking"><h3 id="listing-wallet-ranking">Top 5 Listing Wallets</h3>'+(''.join(cards) or '<p class="note">No completed ranking available yet.</p>')+'</section>'
        +'<section class="listing-panel" aria-labelledby="listing-collection-ranking"><h3 id="listing-collection-ranking">Top 5 Listed NFT Collections</h3><p class="note">All rarities · distinct NFTs across all listing wallets, not orders or units.</p>'+collection_body+'<p class="rule">Rank: distinct listed NFTs descending, exact collection contract ascending for ties. Collections are grouped by exact contract, never display name. Same saved snapshot, local expiry and coverage as the wallet ranking above.</p></section></div>'
        +'<p class="rule">Coverage: provider-enumerated ACTIVE, public, fixed-price Rarible V2 sell orders only, native POL or exact Polygon ERC-20 currency; positive provider makeStock required. Other marketplaces, auctions, private/restricted and unsupported order routes are excluded. This is the verified scanned scope, not every listing on the web. A changing marketplace is observed over the stated scan interval, not an atomic chain snapshot.</p>'
        +'<p class="rule">Counts: wallet listings deduplicate seller + contract + token ID; the market-wide scoped total deduplicates contract + token ID across ALL scanned wallets, not just Top 5. Duplicate orders and ERC-1155 units are not additional NFTs. Owned counts use the same approved scope and distinct contract/token identities with positive confirmed holdings. Ask units are provider available stock per representative order, not an aggregate unit total. Best ask is selected within each currency only; different currencies remain separate.</p>'
        +'<p class="rule">Rank: distinct listed NFTs descending, distinct collections descending, exact private wallet ascending. Live aliases resolve on each saved read; full wallet addresses remain private. Large listed fractions can indicate potential selling pressure, not proof of liquidation or a completed sale. Refresh is scheduled daily at 09:35 Eastern by a local no-agent launcher; its finite worker automatically consumes saved pagination checkpoints for up to six hours per launch. After a cursor-anchored scoped baseline, daily updates replay the last sync page and independently read changed orders across all statuses; weekly scoped reconciliation discovers listed items then exhausts every item’s sell orders without a maker filter. No repeated whole-Polygon ACTIVE daily scan is used. Existing unfinished legacy work is retained until reviewed migration. Scoped discovery relies on provider on-sale item indexing; delayed provider updates can appear on a later sync/reconciliation. Sync coverage marks the last caught-up response, not an atomic chain snapshot. Unchanged confirmed Top 5 inventory is reused for less than 24 hours; changed/new rankings or stale/partial inventory are refreshed. Expired asks are also invalidated locally on saved reads. Incomplete scans never replace the last completed snapshot, and removed orders are independently rechecked.</p></section>')

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',action='store_true');parser.add_argument('--seconds',type=int,default=21600);parser.add_argument('--retry',action='store_true');parser.add_argument('--optimize',action='store_true',help='Explicit reviewed migration; retains legacy raw checkpoint')
    args=parser.parse_args()
    if not args.run:parser.error('--run required; saved page never collects')
    if not 1<=args.seconds<=21600:parser.error('seconds must be 1..21600')
    print(json.dumps(run(seconds=args.seconds,retry=args.retry,optimize=args.optimize)),flush=True)
