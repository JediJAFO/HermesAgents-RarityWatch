"""Additive shared NFT evidence and durable queue. Legacy tables are never changed."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from mtd_nft_history_provider import asset_key, identities, history_request, ownership_request

def encode(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def digest(value): return hashlib.sha256(encode(value).encode()).hexdigest()
def iso(epoch): return (datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(seconds=epoch)).isoformat().replace('+00:00','Z')

class HistoryStore:
    def __init__(self,path,*,allowlist=None):
        from mtd_nft_history_provider import collections
        if allowlist is None:
            from mtd_wallet_transfer_analyzer import load_catalog, DEFAULT_CATALOG
            allowlist=[row['id'] for row in load_catalog(DEFAULT_CATALOG).values()]
        self.allowlist=collections(allowlist)
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS nh_revisions(provider TEXT,id TEXT,digest TEXT,raw TEXT NOT NULL,PRIMARY KEY(provider,id,digest));
            CREATE TABLE IF NOT EXISTS nh_review(provider TEXT,id TEXT,digest TEXT,reason TEXT,PRIMARY KEY(provider,id,digest));
            CREATE TABLE IF NOT EXISTS nh_current(provider TEXT,id TEXT,digest TEXT,PRIMARY KEY(provider,id));
            CREATE TABLE IF NOT EXISTS nh_observations(provider TEXT,id TEXT,digest TEXT,target TEXT,retrieved_at TEXT,source TEXT,job INTEGER,provenance TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS nh_obs_unique ON nh_observations(provider,id,digest,ifnull(target,''),ifnull(retrieved_at,''),ifnull(source,''),ifnull(job,-1),ifnull(provenance,''));
            CREATE TABLE IF NOT EXISTS nh_assets(asset TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS nh_activity_assets(provider TEXT,id TEXT,asset TEXT,PRIMARY KEY(provider,id,asset));
            CREATE TABLE IF NOT EXISTS nh_target_assets(target TEXT,asset TEXT,reason TEXT,support TEXT,PRIMARY KEY(target,asset,reason,support));
            CREATE TABLE IF NOT EXISTS nh_orders(provider TEXT,id TEXT,digest TEXT,raw TEXT,retrieved_at TEXT,source TEXT,PRIMARY KEY(provider,id,digest));
            CREATE TABLE IF NOT EXISTS nh_order_observations(provider TEXT,id TEXT,digest TEXT,provenance TEXT,PRIMARY KEY(provider,id,digest,provenance));
            CREATE TABLE IF NOT EXISTS nh_migrations(name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS nh_runtime(name TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS nh_jobs(id INTEGER PRIMARY KEY,kind TEXT,subject TEXT,scope TEXT,request TEXT,state TEXT,mode TEXT,next_attempt_at REAL DEFAULT 0,retries INTEGER DEFAULT 0,calls INTEGER DEFAULT 0,pages INTEGER DEFAULT 0,rows INTEGER DEFAULT 0,error TEXT,needs_restart INTEGER DEFAULT 0,coverage TEXT,created REAL,UNIQUE(kind,subject,scope));
            CREATE TABLE IF NOT EXISTS nh_pages(id INTEGER PRIMARY KEY,job INTEGER,observed_at TEXT,request TEXT,response TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS nh_ownerships(job INTEGER,id TEXT,digest TEXT,raw TEXT,observed_at TEXT,PRIMARY KEY(job,id,digest));
            CREATE TABLE IF NOT EXISTS nh_lease(name TEXT PRIMARY KEY,pid INTEGER,token TEXT);
            CREATE TABLE IF NOT EXISTS nh_order_batches(id INTEGER PRIMARY KEY,target TEXT,scope TEXT,snapshot TEXT,coverage TEXT,created REAL,UNIQUE(target,scope,snapshot));
            CREATE TABLE IF NOT EXISTS nh_order_targets(batch INTEGER,job INTEGER,PRIMARY KEY(batch,job));
            ''')
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10); db.row_factory=sqlite3.Row
        try:
            with db: yield db
        finally: db.close()
    def link(self,db,target,asset,reason,support=''):
        db.execute('INSERT OR IGNORE INTO nh_assets VALUES(?)',(asset,))
        if target: db.execute('INSERT OR IGNORE INTO nh_target_assets VALUES(?,?,?,?)',(target,asset,reason,support))
    def ingest(self,rows,*,asset=None,target=None,retrieved_at=None,source=None,provider='rarible',job=None,provenance=None,_db=None):
        with (nullcontext(_db) if _db is not None else self.connect()) as db:
            for raw in rows:
                if not isinstance(raw,dict) or not isinstance(raw.get('id'),str) or not raw['id']: raise ValueError('Missing activity identity')
                ident=raw['id']; h=digest(raw)
                db.execute('INSERT OR IGNORE INTO nh_revisions VALUES(?,?,?,?)',(provider,ident,h,encode(raw)))
                previous=db.execute('SELECT r.raw,c.digest FROM nh_current c JOIN nh_revisions r USING(provider,id,digest) WHERE c.provider=? AND c.id=?',(provider,ident)).fetchone()
                if previous is None or self._newer(db,provider,ident,raw,retrieved_at,json.loads(previous[0]),previous[1]):
                    db.execute('INSERT OR REPLACE INTO nh_current VALUES(?,?,?)',(provider,ident,h))
                db.execute('INSERT OR IGNORE INTO nh_observations VALUES(?,?,?,?,?,?,?,?)',(provider,ident,h,target,retrieved_at,source,job,encode(provenance) if provenance else None))
                keys=identities(raw)
                for key in keys:
                    self.link(db,target,key,'activity',ident)
                    db.execute('INSERT OR IGNORE INTO nh_activity_assets VALUES(?,?,?)',(provider,ident,key))
    @staticmethod
    def _newer(db,provider,ident,new,observed,old,old_digest):
        """Provider version, then update time, then actual retrieval; never import order.

        Unknown/tied freshness is deterministic and conservatively prefers reverted.
        Update time orders revisions only; it is not a public price-change timestamp.
        """
        def version(raw):
            value=raw.get('version')
            return int(str(value)) if str(value).isdigit() else -1
        def epoch(value):
            try:
                parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
                return parsed.timestamp() if parsed.tzinfo else float('-inf')
            except (ValueError,TypeError,AttributeError): return float('-inf')
        old_seen=max((epoch(r[0]) for r in db.execute('SELECT retrieved_at FROM nh_observations WHERE provider=? AND id=? AND digest=?',(provider,ident,old_digest))),default=float('-inf'))
        rank=lambda raw,seen:(version(raw),epoch(raw.get('lastUpdatedAt')),seen,bool(raw.get('reverted')),digest(raw))
        return rank(new,epoch(observed))>rank(old,old_seen)

    def counts(self):
        with self.connect() as db:
            return {name:db.execute('SELECT count(*) FROM nh_'+table).fetchone()[0] for name,table in [('revisions','revisions'),('activities','current'),('jobs','jobs'),('observations','observations')]}
    def raw_for_target(self,target):
        with self.connect() as db:
            rows=db.execute('''SELECT DISTINCT r.raw FROM nh_current c JOIN nh_revisions r USING(provider,id,digest)
            WHERE EXISTS(SELECT 1 FROM nh_observations o WHERE o.provider=c.provider AND o.id=c.id AND o.target=?)
            OR EXISTS(SELECT 1 FROM nh_activity_assets a JOIN nh_target_assets t ON a.asset=t.asset WHERE a.provider=c.provider AND a.id=c.id AND t.target=?)''',(target,target)).fetchall()
        return [json.loads(r[0]) for r in rows]
    def timeline(self,asset):
        asset=asset_key(asset)
        with self.connect() as db:
            rows=db.execute('''SELECT r.* FROM nh_current c JOIN nh_revisions r USING(provider,id,digest) JOIN nh_activity_assets a USING(provider,id) WHERE a.asset=?''',(asset,)).fetchall()
        result=[{'asset':asset,'provider':r['provider'],'id':r['id'],'digest':r['digest'],'raw':json.loads(r['raw'])} for r in rows]
        return sorted(result,key=lambda r:(r['raw'].get('date',''),r['id']))
    def migrate_legacy(self):
        """Explicit one-time transaction. Preserve unknown retrieval source as SQL NULL."""
        with self.connect() as db:
            if db.execute("SELECT 1 FROM nh_migrations WHERE name='legacy-v1'").fetchone(): return
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('revisions','evidence'):
                if table not in tables: continue
                for row in db.execute('SELECT * FROM '+table).fetchall():
                    raw=json.loads(row['raw'])
                    self.ingest([raw],target=row['wallet'],retrieved_at=row['retrieved_at'],source=None,provenance={'legacy_table':table,'legacy_digest':row['digest'],'coverage':'bounded/partial'},_db=db)
            for table in ('order_revisions','orders'):
                if table not in tables: continue
                for row in db.execute('SELECT * FROM '+table).fetchall():
                    if not row['raw']: continue
                    raw=json.loads(row['raw']); fields=row.keys()
                    db.execute('INSERT OR IGNORE INTO nh_orders VALUES(?,?,?,?,?,?)',('rarible',row['order_id'],digest(raw),encode(raw),row['first_retrieved'] if 'first_retrieved' in fields else row['retrieved_at'],row['source']))
                    provenance={k:row[k] for k in row.keys() if k!='raw'}
                    db.execute('INSERT OR IGNORE INTO nh_order_observations VALUES(?,?,?,?)',('rarible',row['order_id'],digest(raw),encode(dict(provenance,legacy_table=table))))
            db.execute("INSERT INTO nh_migrations VALUES('legacy-v1')")
    def _enqueue(self,db,kind,subject,request,mode,now,scope=None):
        scope=scope or digest(request)
        db.execute('INSERT OR IGNORE INTO nh_jobs(kind,subject,scope,request,state,mode,created,coverage) VALUES(?,?,?,?,?,?,?,?)',(kind,subject,scope,encode(request),'queued',mode,now,encode({'query_exhausted':False,'blockchain_complete':False,'late_indexing_or_old_revision_gap_possible':True})))
        return db.execute('SELECT id FROM nh_jobs WHERE kind=? AND subject=? AND scope=?',(kind,subject,scope)).fetchone()[0]
    def enqueue_item(self,asset,*,target=None,now=None,allowlist=None,refresh_after=3600,_db=None):
        from mtd_nft_history_provider import collections
        now=time.time() if now is None else now; asset=asset_key(asset)
        if ':'.join(asset.split(':')[:2]) not in collections(self.allowlist if allowlist is None else allowlist): raise ValueError('Asset outside allowlist')
        with (nullcontext(_db) if _db is not None else self.connect()) as db:
            self.link(db,target,asset,'requested')
            last=db.execute("SELECT * FROM nh_jobs WHERE kind='item' AND subject=? ORDER BY id DESC LIMIT 1",(asset,)).fetchone()
            if last:
                if last['state']=='complete':
                    observed=json.loads(last['coverage']).get('last_observed')
                    if observed and now-datetime.fromisoformat(observed.replace('Z','+00:00')).timestamp()>=refresh_after:
                        return self.refresh_item(asset,now=now,_db=db)
                return last['id']
            return self._enqueue(db,'item',asset,history_request(asset,upper=iso(now)),'initial',now,scope=digest(history_request(asset)))
    def enqueue_discovery(self,wallet,allowlist,*,now=None,aliases=None):
        from mtd_wallet_transfer_analyzer import resolve_wallet
        wallet=resolve_wallet(wallet,aliases or {})
        with self.connect() as db: return self._enqueue(db,'discovery',wallet,ownership_request(wallet,allowlist),'current_holdings',time.time() if now is None else now)
    def reconcile_discovery(self,job,*,now=None):
        """Explicit new ownership observation scope; old rows/checkpoints remain intact."""
        prior=self.job(job)
        if prior['kind']!='discovery' or prior['state'] not in ('complete','failed','cancelled'): raise ValueError('Finish/pause-cancel prior discovery before reconciliation')
        now=time.time() if now is None else now
        request=prior['request']; request.pop('continuation',None)
        with self.connect() as db:
            return self._enqueue(db,'discovery',prior['subject'],request,'current_holdings',now,scope=digest({'request':request,'reconcile_after':job,'started':now}))
    def holdings(self,job):
        from mtd_nft_history_provider import current_ownership
        status=self.job(job)
        if status['kind']!='discovery': raise ValueError('Not ownership discovery')
        with self.connect() as db:
            rows=db.execute('SELECT raw FROM nh_ownerships WHERE job=? ORDER BY observed_at,rowid',(job,)).fetchall()
        latest={}
        for row in rows:
            raw=json.loads(row[0]); latest[raw['id']]=raw
        return {'job':job,'current':[r for r in latest.values() if current_ownership(r)],'observations':list(latest.values()),'query_exhausted':status['coverage']['query_exhausted'],'coverage':status['coverage'],'scope':status['scope'],'wallet_historical_universe_requested':False}
    def refresh_item(self,asset,*,now=None,overlap_seconds=86400,full_rescan=False,_db=None):
        """Explicit recent refresh/full rescan. Active frozen query is always reused."""
        now=time.time() if now is None else now; asset=asset_key(asset)
        if overlap_seconds<0: raise ValueError('Negative overlap')
        with (nullcontext(_db) if _db is not None else self.connect()) as db:
            active=db.execute("SELECT id FROM nh_jobs WHERE kind='item' AND subject=? AND state IN ('queued','running','deferred','paused') ORDER BY id LIMIT 1",(asset,)).fetchone()
            if active: return active[0]
            done=db.execute("SELECT * FROM nh_jobs WHERE kind='item' AND subject=? AND state='complete' ORDER BY id DESC LIMIT 1",(asset,)).fetchone()
            if not done and not full_rescan: return self.enqueue_item(asset,now=now,_db=db)
            cov=json.loads(done['coverage']) if done else {}
            watermark=cov.get('newest_event') or cov.get('query_upper_fence') or (json.loads(done['request'])['filter'].get('to') if done else None)
            # Never skip unqueried event time merely because an empty scan completed late.
            if not full_rescan and not watermark: raise ValueError('Missing frozen query fence: explicit full rescan required')
            lower=None if full_rescan else iso(datetime.fromisoformat(watermark.replace('Z','+00:00')).timestamp()-overlap_seconds)
            request=history_request(asset,lower=lower,upper=iso(now))
            return self._enqueue(db,'item',asset,request,'full_rescan' if full_rescan else 'incremental',now)
    def lifecycle(self,asset):
        """Saved-only deterministic timeline. Raw evidence and all revisions remain available."""
        rows=self.timeline(asset)
        def addr(value): return str(value or '').split(':')[-1].lower()
        def quantity(raw): return str((raw.get('nft') or {}).get('value') or raw.get('value') or '')
        def movement_supported(raw):
            import re
            if not raw.get('transactionHash') or not re.fullmatch(r'[0-9]+',quantity(raw)) or int(quantity(raw))<=0: return False
            kind=raw.get('@type')
            parties=[raw.get('seller'),raw.get('buyer')] if kind=='SELL' else [raw.get('owner')]+([raw.get('from')] if kind=='TRANSFER' else [])
            return all(isinstance(p,str) and re.fullmatch(r'(?:ETHEREUM:)?0x[0-9a-fA-F]{40}',p) for p in parties)
        sales={}; transfers={}
        for row in rows:
            with self.connect() as db:
                row['needs_review']=bool(db.execute('SELECT 1 FROM nh_review WHERE provider=? AND id=?',(row['provider'],row['id'])).fetchone())
            raw=row['raw']
            if raw.get('@type')=='SELL' and not row['needs_review'] and not raw.get('reverted') and movement_supported(raw):
                key=(raw['transactionHash'],addr(raw.get('seller')),addr(raw.get('buyer')),quantity(raw))
                sales.setdefault(key,[]).append(row['id'])
            if raw.get('@type')=='TRANSFER' and not row['needs_review'] and not raw.get('reverted') and movement_supported(raw):
                key=(raw['transactionHash'],addr(raw.get('from')),addr(raw.get('owner')),quantity(raw))
                transfers.setdefault(key,[]).append(row['id'])
        for row in rows:
            raw=row['raw']; kind=raw.get('@type'); linked=None; ambiguous=False
            if kind=='TRANSFER' and raw.get('transactionHash'):
                key=(raw['transactionHash'],addr(raw.get('from')),addr(raw.get('owner')),quantity(raw))
                candidates=sales.get(key,[])
                ambiguous=bool(candidates) and (len(candidates)!=1 or len(transfers.get(key,[]))!=1)
                if len(candidates)==1 and not ambiguous: linked=candidates[0]
            row.update(kind=kind,execution_type=raw.get('type') if kind=='SELL' else None,linked_sale_id=linked,representation_ambiguous=ambiguous,
                       counts_as_acquisition=not ambiguous and not row['needs_review'] and not raw.get('reverted',False) and kind in ('MINT','SELL','TRANSFER') and movement_supported(raw) and linked is None,
                       continuous_exposure_seconds=None,price_change_time=None)
            with self.connect() as db:
                row['observations']=[dict(r) for r in db.execute('SELECT retrieved_at,source,target,job,provenance FROM nh_observations WHERE provider=? AND id=? AND digest=?',(row['provider'],row['id'],row['digest']))]
                row['revisions']=[dict(r) for r in db.execute('SELECT digest,raw FROM nh_revisions WHERE provider=? AND id=? ORDER BY digest',(row['provider'],row['id']))]
        return rows
    def enqueue_orders(self,wallet,order_ids,*,scope,coverage,now=None):
        """Frozen saved-purchase IDs only. No discovery, refresh or implicit retry."""
        from mtd_wallet_transfer_analyzer import validate_wallet, valid_order_id
        wallet=validate_wallet(wallet)
        ids=sorted(set(order_ids))
        if not all(valid_order_id(value) for value in ids): raise ValueError('Malformed literal order identifier')
        now=time.time() if now is None else now
        snapshot=encode(ids)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO nh_order_batches(target,scope,snapshot,coverage,created) VALUES(?,?,?,?,?)',(wallet,scope,snapshot,encode(coverage),now))
            batch=db.execute('SELECT id FROM nh_order_batches WHERE target=? AND scope=? AND snapshot=?',(wallet,scope,snapshot)).fetchone()[0]
            for identity in ids:
                job=self._enqueue(db,'order',identity,{'order_id':identity},'saved_purchase',now,scope='purchase-order-v1')
                db.execute('INSERT OR IGNORE INTO nh_order_targets VALUES(?,?)',(batch,job))
                cached=db.execute('SELECT status FROM orders WHERE order_id=?',(identity,)).fetchone()
                current=db.execute('SELECT * FROM nh_jobs WHERE id=?',(job,)).fetchone()
                if cached and current['state']=='queued' and not current['calls'] and not json.loads(current['request']).get('retry_cache'):
                    db.execute('UPDATE nh_jobs SET state=?,error=? WHERE id=?',('complete' if cached[0] in ('saved','missing') else 'failed',None if cached[0] in ('saved','missing') else 'Cached lookup failed; explicit retry required',job))
        return self.order_queue_status(wallet,batch=batch)

    def retry_orders(self,wallet,*,batch=None):
        """Explicit retry of this frozen snapshot's missing/failed orders only."""
        status=self.order_queue_status(wallet,batch=batch)
        with self.connect() as db:
            jobs=db.execute('SELECT j.*,o.status AS cache_status FROM nh_order_targets t JOIN nh_jobs j ON j.id=t.job LEFT JOIN orders o ON o.order_id=j.subject WHERE t.batch=?',(status['batch'],)).fetchall()
            for row in jobs:
                if row['cache_status']=='saved' or row['state'] in ('running','queued','deferred','paused','cancelled'): continue
                if row['state']!='failed' and row['cache_status'] not in ('missing','error'): continue
                request=json.loads(row['request']); request['retry_cache']=True
                db.execute("UPDATE nh_jobs SET state='queued',request=?,next_attempt_at=0,retries=0,error=NULL WHERE id=?",(encode(request),row['id']))
                # Same explicit remediation policy as selected-job Resume.
                db.execute("DELETE FROM nh_runtime WHERE name='auth_blocked'")
        return self.order_queue_status(wallet,batch=status['batch'])

    def order_queue_status(self,wallet,*,batch=None):
        with self.connect() as db:
            row=db.execute('SELECT * FROM nh_order_batches WHERE target=?'+(' AND id=?' if batch is not None else '')+' ORDER BY id DESC LIMIT 1',(wallet,batch) if batch is not None else (wallet,)).fetchone()
            runtime=dict(db.execute('SELECT name,value FROM nh_runtime'))
            jobs=[dict(r) for r in db.execute('SELECT j.*,o.status AS cache_status,o.retrieved_at FROM nh_order_targets t JOIN nh_jobs j ON j.id=t.job LEFT JOIN orders o ON o.order_id=j.subject WHERE t.batch=?',(row['id'],))] if row else []
        counts={name:sum(j['state']==name for j in jobs) for name in ('queued','running','deferred','paused','failed','cancelled')}
        counts.update(saved=sum(j['cache_status']=='saved' for j in jobs),missing=sum(j['cache_status']=='missing' for j in jobs),total=len(jobs),
                      remaining=sum(j['state']!='complete' for j in jobs),batch=row['id'] if row else None,
                      coverage=json.loads(row['coverage']) if row else {},latest_progress=max((j['retrieved_at'] for j in jobs if j['retrieved_at']),default=None),
                      queue_paused=runtime.get('queue_paused')=='1',auth_blocked=runtime.get('auth_blocked')=='1',provider_not_before=runtime.get('provider_not_before'),
                      next_attempt_at=min((j['next_attempt_at'] for j in jobs if j['state'] in ('queued','deferred')),default=None),network_calls=0,notifications=0,llm_tokens=0)
        return counts

    def queue_control(self,action):
        if action not in ('pause','resume'): raise ValueError('Choose pause or resume')
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO nh_runtime VALUES('queue_paused',?)",('1' if action=='pause' else '0',))
        # Global resume does not remediate authentication or change manual job states.

    def queue_status(self, *, include_assets=True):
        with self.connect() as db:
            runtime=dict(db.execute('SELECT name,value FROM nh_runtime').fetchall())
            jobs=[self.job(r[0]) for r in db.execute('SELECT id FROM nh_jobs ORDER BY id')]
            asset_count=db.execute('SELECT count(*) FROM nh_assets').fetchone()[0]
            assets=[r[0] for r in db.execute('SELECT asset FROM nh_assets ORDER BY asset')] if include_assets else None
        states={state:sum(j['state']==state for j in jobs) for state in ('queued','running','deferred','paused','cancelled','complete','failed')}
        result={'paused':runtime.get('queue_paused')=='1','auth_blocked':runtime.get('auth_blocked')=='1',
                'provider_not_before':runtime.get('provider_not_before'),'states':states,'jobs':jobs,'asset_count':asset_count,
                'current_job':next((j['id'] for j in jobs if j['state']=='running'),None),
                'network_calls':0,'notifications':0,'llm_tokens':0,'wallet_historical_universe_requested':False}
        if include_assets: result['assets']=assets
        return result

    def control(self,job,action):
        with self.connect() as db:
            row=db.execute('SELECT * FROM nh_jobs WHERE id=?',(job,)).fetchone()
            if row is None: raise ValueError('Unknown job')
            if action=='resume':
                if row['needs_restart']: raise ValueError('Cursor invalid: explicit full rescan required')
                state='queued'
                db.execute("DELETE FROM nh_runtime WHERE name='auth_blocked'")
            elif action in ('pause','cancel'): state={'pause':'paused','cancel':'cancelled'}[action]
            else: raise ValueError('Unknown action')
            db.execute('UPDATE nh_jobs SET state=?,next_attempt_at=0,retries=0 WHERE id=?',(state,job))
    def job(self,job):
        with self.connect() as db: row=db.execute('SELECT * FROM nh_jobs WHERE id=?',(job,)).fetchone()
        if row is None: raise ValueError('Unknown job')
        result=dict(row); result['request']=json.loads(result['request']); result['coverage']=json.loads(result['coverage']); return result
