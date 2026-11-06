"""Exact-asset rarity work ledger. Metadata only; no mint/burn collection."""
import json, re, time
from mtd_supply import collection, digest, encode, utc, stamp

SCHEMA='''
CREATE TABLE IF NOT EXISTS rarity_work(contract TEXT NOT NULL REFERENCES contracts(contract),token TEXT NOT NULL,asset TEXT NOT NULL UNIQUE,state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,due REAL NOT NULL DEFAULT 0,retrieved_at TEXT,provenance TEXT,raw_hash TEXT,reason TEXT,PRIMARY KEY(contract,token));
CREATE TABLE IF NOT EXISTS rarity_collections(contract TEXT PRIMARY KEY REFERENCES contracts(contract),cursor TEXT,state TEXT NOT NULL DEFAULT 'pending',pages INTEGER NOT NULL DEFAULT 0,serviced REAL NOT NULL DEFAULT 0,due REAL NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,reason TEXT);
CREATE TABLE IF NOT EXISTS rarity_receipts(id INTEGER PRIMARY KEY,endpoint TEXT NOT NULL,request TEXT NOT NULL,response TEXT NOT NULL,sha256 TEXT NOT NULL,retrieved_at TEXT NOT NULL,accepted INTEGER NOT NULL DEFAULT 0,error TEXT);
CREATE TABLE IF NOT EXISTS rarity_cursors(contract TEXT NOT NULL,cursor TEXT NOT NULL,PRIMARY KEY(contract,cursor));
CREATE TABLE IF NOT EXISTS rarity_runtime(name TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rarity_basis(generation INTEGER PRIMARY KEY REFERENCES generations(id),assets TEXT NOT NULL,sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rarity_publications(id INTEGER PRIMARY KEY,generation INTEGER NOT NULL REFERENCES generations(id),revision TEXT NOT NULL,result TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(generation,revision));
'''

class Queue:
    def __init__(self,store): self.store=store
    def initialize(self):
        with self.store.connect() as db: db.executescript(SCHEMA)
    def seed(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT OR IGNORE INTO rarity_collections(contract) SELECT contract FROM contracts")
            db.execute("""INSERT OR IGNORE INTO rarity_work(contract,token,asset,state)
                SELECT DISTINCT e.contract,substr(e.asset,length(e.contract)+2),e.asset,
                CASE WHEN m.rarity IS NOT NULL AND m.rarity!='Unknown' THEN 'resolved' ELSE 'pending' END
                FROM events e LEFT JOIN metadata_current c ON c.asset=e.asset
                LEFT JOIN metadata m ON m.asset=c.asset AND m.digest=c.digest WHERE e.reverted=0""")
            db.execute("""UPDATE rarity_work SET state='resolved',
                retrieved_at=(SELECT m.observed_at FROM metadata_current c JOIN metadata m USING(asset,digest) WHERE c.asset=rarity_work.asset),
                provenance=(SELECT m.source FROM metadata_current c JOIN metadata m USING(asset,digest) WHERE c.asset=rarity_work.asset),
                raw_hash=(SELECT digest FROM metadata_current c WHERE c.asset=rarity_work.asset),reason=NULL
                WHERE state!='resolved' AND EXISTS(SELECT 1 FROM metadata_current c JOIN metadata m USING(asset,digest) WHERE c.asset=rarity_work.asset AND m.rarity!='Unknown')""")
    def accept(self,endpoint,request,response,now=None,checkpoint=True):
        now=time.time() if now is None else now
        # Archive before parsing; rejected pages never advance the checkpoint.
        with self.store.connect() as db:
            receipt=db.execute('INSERT INTO rarity_receipts(endpoint,request,response,sha256,retrieved_at) VALUES(?,?,?,?,?)',(endpoint,encode(request),encode(response),digest(response),utc(now))).lastrowid
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                scope={r[0] for r in db.execute('SELECT contract FROM contracts')}
                rows=checked_items(endpoint,request,response,scope)
                if endpoint=='items/byCollection' and checkpoint:
                    c=request['collection'];saved=db.execute('SELECT * FROM rarity_collections WHERE contract=?',(c,)).fetchone()
                    if not saved or saved['cursor']!=request.get('continuation') or saved['state']=='exhausted': raise ValueError('Stale checkpoint')
                    cursor=response.get('continuation')
                    if cursor is not None:
                        if not isinstance(cursor,str) or not cursor or len(cursor)>8192 or cursor==request.get('continuation') or db.execute('SELECT 1 FROM rarity_cursors WHERE contract=? AND cursor=?',(c,cursor)).fetchone(): raise ValueError('Repeated/invalid continuation')
                        db.execute('INSERT INTO rarity_cursors VALUES(?,?)',(c,cursor))
                for raw in rows: save_item(db,raw,now)
                if endpoint=='items/byIds':
                    returned={r['id'] for r in rows}
                    for asset in set(request['ids'])-returned: mark_absent(db,asset,now,'Exact ID omitted by provider')
                if endpoint=='items/byCollection' and checkpoint:
                    db.execute('UPDATE rarity_collections SET cursor=?,state=?,pages=pages+1,serviced=?,failures=0,due=0,reason=NULL WHERE contract=?',(cursor,'pending' if cursor else 'exhausted',now,c))
                db.execute('UPDATE rarity_receipts SET accepted=1 WHERE id=?',(receipt,))
                db.execute("DELETE FROM rarity_runtime WHERE name='pending_request'")
        except Exception:
            with self.store.connect() as db: db.execute('UPDATE rarity_receipts SET error=? WHERE id=?',('Validation failed; checkpoint retained',receipt))
            raise
        return receipt
    def rebuild_bases(self):
        """Replay only archived accepted pages in scratch; never rewrite event history.

        Each frozen generation is tied to its final accepted receipt, not current
        working events. Aggregate equality is a required migration invariant.
        """
        import tempfile, pathlib
        from mtd_supply import Store, HOME
        with self.store.connect(readonly=True) as db:
            missing={r['id']:dict(r) for r in db.execute('SELECT g.* FROM generations g LEFT JOIN rarity_basis b ON b.generation=g.id WHERE b.generation IS NULL')}
            if not missing: return 0
            scope=[json.loads(r[0]) for r in db.execute('SELECT scope_raw FROM contracts')]
            jobs={r['id']:dict(r) for r in db.execute('SELECT * FROM jobs')}
            receipts=[dict(r) for r in db.execute('SELECT * FROM receipts WHERE accepted=1 ORDER BY id')]
        byjob={g['job']:g for g in missing.values()};saved=0
        with tempfile.TemporaryDirectory(dir=HOME/'cache/scratch',prefix='rarity-replay-') as temp:
            replay=Store(pathlib.Path(temp)/'replay.sqlite3');replay.initialize(scope,now=0)
            with replay.connect() as db: db.execute('DELETE FROM jobs')
            seen=set()
            for r in receipts:
                j=jobs[r['job']]
                if j['id'] not in seen:
                    with replay.connect() as db:
                        db.execute("UPDATE jobs SET state='exhausted' WHERE contract=?",(j['contract'],))
                        db.execute('INSERT INTO jobs(id,contract,mode,lower_at,upper_at) VALUES(?,?,?,?,?)',tuple(j[k] for k in ('id','contract','mode','lower_at','upper_at')))
                    seen.add(j['id'])
                with replay.connect(readonly=True) as db:
                    job=dict(db.execute('SELECT j.*,c.standard FROM jobs j JOIN contracts c USING(contract) WHERE j.id=?',(j['id'],)).fetchone())
                response=json.loads(r['response'])
                replay.commit_page(job,response,now=0)
                if response.get('cursor') is None and j['id'] in byjob:
                    g=byjob[j['id']]
                    with replay.connect(readonly=True) as db:
                        basis=event_basis(db,j['contract']); actual=replay.totals(db,j['contract'])
                    expected=json.loads(g['result'])
                    if any(actual[k]!=expected[k] for k in ('minted','burned','exists','distinct_token_ids','balance_valid','first_mint')): raise ValueError('Archived generation replay mismatch; no projection')
                    with self.store.connect() as db: db.execute('INSERT OR IGNORE INTO rarity_basis VALUES(?,?,?)',(g['id'],encode(basis),digest(basis)))
                    saved+=1
        if saved!=len(missing): raise ValueError('Missing final receipt; no inferred basis')
        return saved
    def publish(self,now=None):
        now=time.time() if now is None else now
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            latest=db.execute('SELECT g.* FROM generations g WHERE g.id=(SELECT MAX(x.id) FROM generations x WHERE x.contract=g.contract)').fetchall()
            for g in latest:
                b=db.execute('SELECT * FROM rarity_basis WHERE generation=?',(g['id'],)).fetchone()
                if not b: continue
                basis=json.loads(b['assets']);buckets={};revisions=[]
                for asset,units in basis.items():
                    m=db.execute('SELECT m.rarity,m.digest FROM metadata_current c JOIN metadata m USING(asset,digest) WHERE c.asset=?',(asset,)).fetchone()
                    rarity=m['rarity'] if m else 'Unknown';revisions.append([asset,m['digest'] if m else None])
                    bucket=buckets.setdefault(rarity,{'rarity':rarity,'minted':0,'burned':0,'exists':0})
                    bucket['minted']+=int(units[0]);bucket['burned']+=int(units[1])
                result=json.loads(g['result'])
                for row in buckets.values(): row['exists']=row['minted']-row['burned'] if result['exists'] is not None else None
                if sum(r['minted'] for r in buckets.values())!=(result['minted'] or 0) or sum(r['burned'] for r in buckets.values())!=(result['burned'] or 0): raise ValueError('Metadata projection changed supply')
                result['rarities']=sorted(buckets.values(),key=lambda r:r['rarity'])
                revision=digest({'basis':b['sha256'],'metadata':sorted(revisions)})
                db.execute('INSERT OR IGNORE INTO rarity_publications(generation,revision,result,created_at) VALUES(?,?,?,?)',(g['id'],revision,encode(result),utc(now)))

    def progress(self):
        with self.store.connect(readonly=True) as db:
            return progress(db)


def event_basis(db,contract):
    basis={}
    for e in db.execute('SELECT asset,kind,quantity FROM events WHERE contract=? AND reverted=0',(contract,)):
        v=basis.setdefault(e['asset'],[0,0]);v[0 if e['kind']=='MINT' else 1]+=int(e['quantity'])
    return {a:[str(v[0]),str(v[1])] for a,v in basis.items()}

def capture_basis(db,generation,contract):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='rarity_basis'").fetchone():
        basis=event_basis(db,contract)
        db.execute('INSERT OR IGNORE INTO rarity_basis VALUES(?,?,?)',(generation,encode(basis),digest(basis)))

PROVIDER='Rarible exact item metadata v1' 
def validate_request(endpoint,request):
    if not isinstance(request,dict): raise ValueError('Request required')
    if endpoint=='items/byCollection':
        if set(request)-{'collection','size','continuation'} or collection(request.get('collection'))!=request['collection'] or type(request.get('size')) is not int or not 1<=request['size']<=500: raise ValueError('Invalid collection request')
        if 'continuation' in request and (not isinstance(request['continuation'],str) or not request['continuation'] or len(request['continuation'])>8192): raise ValueError('Invalid continuation')
    elif endpoint=='items/byIds':
        ids=request.get('ids')
        if set(request)!={'ids'} or not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not isinstance(a,str) or not re.fullmatch(r'POLYGON:0x[0-9a-f]{40}:[0-9]+',a) for a in ids) or len(set(ids))!=len(ids): raise ValueError('Invalid exact IDs')
    else: raise ValueError('Unsupported metadata request')

def checked_items(endpoint,request,response,scope):
    validate_request(endpoint,request)
    if not isinstance(response,dict) or not isinstance(response.get('items'),list): raise ValueError('Items required')
    if endpoint=='items/byCollection':
        if request.get('collection') not in scope: raise ValueError('Unapproved contract')
    elif endpoint=='items/byIds':
        ids=request.get('ids')
        if not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)): raise ValueError('Exact distinct IDs required')
        for asset in ids:
            if not isinstance(asset,str) or not re.fullmatch(r'POLYGON:0x[0-9a-f]{40}:[0-9]+',asset) or asset.rsplit(':',1)[0] not in scope: raise ValueError('Unapproved asset')
    else: raise ValueError('Unsupported metadata endpoint')
    seen=set()
    for raw in response['items']:
        if not isinstance(raw,dict): raise ValueError('Malformed item')
        asset=raw.get('id')
        if not isinstance(asset,str) or not re.fullmatch(r'POLYGON:0x[0-9a-f]{40}:[0-9]+',asset): raise ValueError('Invalid exact item')
        c,t=asset.rsplit(':',1)
        if c not in scope or raw.get('blockchain')!='POLYGON' or raw.get('contract')!=c or raw.get('collection')!=c or raw.get('tokenId')!=t: raise ValueError('Conflicting exact identity')
        if asset in seen or (endpoint=='items/byCollection' and c!=request['collection']) or (endpoint=='items/byIds' and asset not in request['ids']): raise ValueError('Duplicate/unrequested item')
        seen.add(asset)
    return response['items']

def mark_absent(db,asset,now,reason):
    row=db.execute('SELECT state,attempts FROM rarity_work WHERE asset=?',(asset,)).fetchone()
    if not row: return
    attempts=row['attempts']+1
    state=row['state'] if row['state'] in ('resolved','conflict') else 'missing'
    db.execute('UPDATE rarity_work SET state=?,attempts=?,due=?,retrieved_at=?,reason=? WHERE asset=?',(state,attempts,now+(86400 if attempts==1 else 7*86400) if attempts<3 else 1e99,utc(now),reason,asset))

def metadata_order(raw):
    version=raw.get('version',0)
    if type(version) is not int or version<0: raise ValueError('Invalid metadata version')
    return (stamp(raw['lastUpdatedAt']) if raw.get('lastUpdatedAt') else '',version)

def save_item(db,raw,now):
    # Validate the first observation too; otherwise malformed ordering can
    # poison the authoritative current fact and block every later revision.
    new_order=metadata_order(raw)
    asset=raw['id'];attrs=raw.get('meta',{}).get('attributes',[]) if isinstance(raw.get('meta'),dict) else []
    values={a['value'] for a in attrs if isinstance(a,dict) and a.get('key')=='Rarity' and isinstance(a.get('value'),str) and a['value'].strip() and a['value']!='Unknown'}
    rarity=next(iter(values)) if len(values)==1 else 'Unknown'
    h=digest(raw)
    db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?,?,?,?,?)',(asset,h,rarity,PROVIDER,encode(raw),utc(now)))
    prior=db.execute('SELECT m.* FROM metadata_current c JOIN metadata m ON m.asset=c.asset AND m.digest=c.digest WHERE c.asset=?',(asset,)).fetchone()
    conflict=len(values)>1
    if prior and prior['source']==PROVIDER:
        old=json.loads(prior['raw']);old_item=old.get('item',old);old_order=metadata_order(old_item)
        if new_order<old_order or (old.get('conflict') and new_order<=old_order): return
        if new_order==old_order and prior['rarity']!='Unknown' and rarity!='Unknown' and prior['rarity']!=rarity: conflict=True
    if not values:
        mark_absent(db,asset,now,'No exact Rarity attribute; prior good retained')
        return
    if conflict:
        rarity='Unknown';fact={'conflict':True,'item':raw,'prior_digest':prior['digest'] if prior else None};h=digest(fact)
        db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?,?,?,?,?)',(asset,h,rarity,PROVIDER,encode(fact),utc(now)))
    db.execute('INSERT OR REPLACE INTO metadata_current VALUES(?,?)',(asset,h))
    db.execute('UPDATE rarity_work SET state=?,attempts=attempts+1,due=?,retrieved_at=?,provenance=?,raw_hash=?,reason=? WHERE asset=?',('conflict' if conflict else 'resolved',1e99 if conflict else 0,utc(now),PROVIDER,h,'Conflicting exact Rarity facts; review required' if conflict else None,asset))

def progress(db):
    states=('pending','resolved','missing','temporaryfailure','conflict')
    totals=dict.fromkeys(states,0); rows=[]
    for c in db.execute('SELECT contract FROM contracts ORDER BY contract'):
        counts=dict.fromkeys(states,0)
        for r in db.execute('SELECT state,count(*) n FROM rarity_work WHERE contract=? GROUP BY state',(c[0],)): counts[r[0]]=r[1]
        counts['total']=sum(counts.values());counts['outstanding']=counts['total']-counts['resolved']
        rows.append({'contract':c[0],**counts})
        for k in states: totals[k]+=counts[k]
    totals['total']=sum(totals.values());totals['outstanding']=totals['total']-totals['resolved']
    runtime=dict(db.execute('SELECT name,value FROM rarity_runtime'))
    last=db.execute('SELECT MAX(retrieved_at) FROM rarity_receipts WHERE accepted=1').fetchone()[0]
    enumeration=dict(db.execute('SELECT state,count(*) FROM rarity_collections GROUP BY state'))
    return {**totals,'known_percent':round(100*totals['resolved']/totals['total'],2) if totals['total'] else None,'contracts':rows,'collection_states':enumeration,'last_saved':last,'last_tick':json.loads(runtime.get('last_tick','{}')),'manual_review':any(k in runtime for k in ('pending_request','manual_block','auth_blocked')),'shared_gate':runtime.get('shared_gate'),'not_before':runtime.get('not_before')}
