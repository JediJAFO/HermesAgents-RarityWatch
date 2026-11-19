"""Independent provider-indexed MTD supply ledger. No transport on import/read.
Units are arbitrary-precision integers, not distinct token IDs. Never chain complete.
Raw pages, revisions, metadata evidence and completed generations are append-only.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib, json, pathlib, re, sqlite3, time

HOME=pathlib.Path(__file__).resolve().parents[1]
ROOT=HOME/'price-watches'
DATABASE=ROOT/'mtd-supply.sqlite3'
QUALIFIER='provider-query-exhausted; blockchain incomplete'

def encode(x): return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=True)
def digest(x): return hashlib.sha256(encode(x).encode()).hexdigest()
def utc(epoch): return datetime.fromtimestamp(epoch,timezone.utc).isoformat().replace('+00:00','Z')
def stamp(value):
    if not isinstance(value,str): raise ValueError('Timestamp required')
    d=datetime.fromisoformat(value.replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('Timezone required')
    return d.astimezone(timezone.utc).isoformat(timespec='microseconds')
def collection(value):
    if not isinstance(value,str) or not re.fullmatch('POLYGON:0x[0-9a-fA-F]{40}',value): raise ValueError('Exact Polygon collection required')
    return 'POLYGON:'+value.split(':')[1].lower()

def normalize(raw,expected,standard='Unknown'):
    kind=raw.get('@type')
    if kind not in ('MINT','BURN'): raise ValueError('Unexpected activity type')
    c=collection(raw.get('contract') or raw.get('collection'))
    token=raw.get('tokenId'); quantity=raw.get('value'); identity=raw.get('id')
    if c!=expected or not isinstance(token,str) or not re.fullmatch('[0-9]+',token): raise ValueError('Out-of-scope or invalid token')
    if raw.get('itemId')!=c+':'+token or (raw.get('collection') and collection(raw['collection'])!=c): raise ValueError('Conflicting exact identity')
    if not isinstance(quantity,str) or not re.fullmatch('[0-9]+',quantity): raise ValueError('Invalid integer quantity')
    quantity=int(quantity)
    if standard=='ERC721' and quantity!=1: raise ValueError('ERC721 quantity conflict')
    if not isinstance(identity,str) or not identity.startswith('POLYGON:') or len(identity)>256: raise ValueError('Invalid provider identity')
    if type(raw.get('reverted')) is not bool: raise ValueError('Reversion evidence required')
    # Explicit contradictory zero-address fields fail closed. Rarible MINT/BURN
    # normally use owner, not transfer from/to. Never infer a standard from that.
    zero='0x'+'0'*40
    for field in (('from',) if kind=='MINT' else ('to',)):
        if raw.get(field) is not None and str(raw[field]).split(':')[-1].lower()!=zero: raise ValueError('Contradictory zero-address evidence')
    b=raw.get('blockchainInfo',{}); tx=b.get('transactionHash') or raw.get('transactionHash'); idx=b.get('logIndex')
    if not isinstance(tx,str) or not re.fullmatch('0x[0-9a-fA-F]{64}',tx) or type(idx) is not int or idx<0: raise ValueError('Exact log identity required')
    if raw.get('transactionHash') and raw['transactionHash'].lower()!=tx.lower(): raise ValueError('Transaction identity conflict')
    version=raw.get('version',0)
    if type(version) is not int or version<0: raise ValueError('Invalid revision version')
    return {'id':identity,'event':c+':'+tx.lower()+':'+str(idx)+':'+token+':'+kind,'asset':c+':'+token,'kind':kind,'quantity':str(quantity),'date':stamp(raw['date']),'updated':stamp(raw['lastUpdatedAt']),'version':version,'reverted':int(raw['reverted'])}

SCHEMA='''
CREATE TABLE IF NOT EXISTS contracts(contract TEXT PRIMARY KEY,name TEXT NOT NULL,standard TEXT NOT NULL,scope_raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,contract TEXT NOT NULL REFERENCES contracts(contract),mode TEXT NOT NULL,lower_at TEXT,upper_at TEXT NOT NULL,cursor TEXT,state TEXT NOT NULL DEFAULT 'queued',pages INTEGER NOT NULL DEFAULT 0,due REAL NOT NULL DEFAULT 0,serviced REAL NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,error TEXT,published INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_contract ON jobs(contract) WHERE state IN ('queued','running','deferred','blocked');
CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY,job INTEGER NOT NULL REFERENCES jobs(id),request TEXT NOT NULL,response TEXT NOT NULL,sha256 TEXT NOT NULL,retrieved_at TEXT NOT NULL,accepted INTEGER NOT NULL DEFAULT 0,error TEXT);
CREATE TABLE IF NOT EXISTS revisions(provider_id TEXT NOT NULL,digest TEXT NOT NULL,raw TEXT NOT NULL,PRIMARY KEY(provider_id,digest));
CREATE TABLE IF NOT EXISTS observations(receipt INTEGER NOT NULL REFERENCES receipts(id),provider_id TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(receipt,provider_id,digest),FOREIGN KEY(provider_id,digest) REFERENCES revisions(provider_id,digest));
CREATE TABLE IF NOT EXISTS events(event TEXT PRIMARY KEY,contract TEXT NOT NULL REFERENCES contracts(contract),asset TEXT NOT NULL,provider_id TEXT NOT NULL,digest TEXT NOT NULL,kind TEXT NOT NULL,quantity TEXT NOT NULL,date TEXT NOT NULL,updated TEXT NOT NULL,version INTEGER NOT NULL,reverted INTEGER NOT NULL,FOREIGN KEY(provider_id,digest) REFERENCES revisions(provider_id,digest));
CREATE INDEX IF NOT EXISTS contract_event ON events(contract,kind,reverted);
CREATE TABLE IF NOT EXISTS event_aliases(provider_id TEXT PRIMARY KEY,event TEXT NOT NULL,FOREIGN KEY(event) REFERENCES events(event) DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE IF NOT EXISTS metadata(asset TEXT NOT NULL,digest TEXT NOT NULL,rarity TEXT NOT NULL,source TEXT NOT NULL,raw TEXT NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(asset,digest));
CREATE TABLE IF NOT EXISTS metadata_current(asset TEXT PRIMARY KEY,digest TEXT NOT NULL,FOREIGN KEY(asset,digest) REFERENCES metadata(asset,digest));
CREATE TABLE IF NOT EXISTS generations(id INTEGER PRIMARY KEY,job INTEGER NOT NULL UNIQUE REFERENCES jobs(id),contract TEXT NOT NULL REFERENCES contracts(contract),result TEXT NOT NULL,created_at TEXT NOT NULL,blockchain_complete INTEGER NOT NULL DEFAULT 0 CHECK(blockchain_complete=0));
CREATE TABLE IF NOT EXISTS runtime(name TEXT PRIMARY KEY,value TEXT NOT NULL);
'''

class Store:
    def __init__(self,path=DATABASE): self.path=pathlib.Path(path)
    @contextmanager
    def connect(self,readonly=False):
        db=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10) if readonly else sqlite3.connect(self.path,timeout=10)
        db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON')
        try:
            with db: yield db
        finally: db.close()
    def initialize(self,scope,now=None):
        now=time.time() if now is None else now
        validated=[(collection(r['id']),r.get('display_name') or r['id'],r.get('metadata',{}).get('type','Unknown'),encode(r)) for r in scope]
        if len({r[0] for r in validated})!=len(validated): raise ValueError('Duplicate scope')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO runtime VALUES('refresh_cadence',?)",(encode({'activated':now,'completed':None,'pending':None}),))
            saved={r[0] for r in db.execute('SELECT contract FROM contracts')}
            if saved and saved!={r[0] for r in validated}: raise ValueError('Scope change requires explicit migration')
            for row in validated:
                db.execute('INSERT OR IGNORE INTO contracts VALUES(?,?,?,?)',row)
                if not db.execute('SELECT 1 FROM jobs WHERE contract=?',(row[0],)).fetchone():
                    db.execute('INSERT INTO jobs(contract,mode,upper_at) VALUES(?,?,?)',(row[0],'backfill',utc(now)))
    def quarantine_request(self,job,pending_hash,*,manual_hash=None,stage,exception_code,proof,db=None):
        """Internal acknowledgement; caller MUST own worker mutex and protected provider boundary.

        No timeout/age authorizes this operation. The manual CLI provides the
        protected boundary; the worker permits only trusted returned payloads.
        Runtime namespace avoids a destructive schema migration on existing DBs.
        """
        if stage not in ('historical_unknown','page_validation') or exception_code not in ('unknown','ValueError'):
            raise ValueError('Unsupported incident classification')
        if proof not in ('operator_protected','adapter_closed_success'): raise ValueError('Protected outcome proof required')
        if (stage=='historical_unknown') != (proof=='operator_protected'): raise ValueError('Inconsistent proof')
        if db is None:
            with self.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                return self.quarantine_request(job,pending_hash,manual_hash=manual_hash,stage=stage,exception_code=exception_code,proof=proof,db=conn)
        live=db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
        snapshot={k:v for k,v in job.items() if k!='standard'}
        if not live or dict(live)!=snapshot or live['state'] not in ('blocked','queued','running','deferred') or live['published'] is not None:
            raise ValueError('Job fence changed')
        if collection(live['contract'])!=live['contract'] or live['mode'] not in ('backfill','incremental','reconcile'):
            raise ValueError('Unsupported read-only job')
        if not db.execute('SELECT 1 FROM contracts WHERE contract=?',(live['contract'],)).fetchone(): raise ValueError('Out of scope')
        runtime=dict(db.execute('SELECT name,value FROM runtime'))
        if any(k in runtime for k in ('auth_blocked','not_before','shared_gate')): raise ValueError('Provider-wide restriction retained')
        pending=json.loads(runtime.get('pending_request','null'))
        if not isinstance(pending,dict) or set(pending)!={'job','token','started'} or pending['job']!=live['id'] or not isinstance(pending['token'],str) or not re.fullmatch('[a-f0-9]{32}',pending['token']) or digest(pending)!=pending_hash:
            raise ValueError('Pending fence changed')
        stamp(pending['started'])
        if manual_hash is None:
            if 'manual_block' in runtime: raise ValueError('Preexisting manual restriction')
        elif runtime.get('manual_block')!='1' or digest(runtime['manual_block'])!=manual_hash or stage!='historical_unknown':
            raise ValueError('Manual fence changed')
        if stage=='page_validation':
            receipt=db.execute('SELECT * FROM receipts WHERE job=? ORDER BY id DESC LIMIT 1',(live['id'],)).fetchone()
            if not receipt or receipt['accepted'] or not receipt['error'] or receipt['request']!=encode(self.request(snapshot)):
                raise ValueError('Rejected raw evidence required')
        body={'version':1,'status':'unresolved/operator-review','job':snapshot,'request':self.request(snapshot),'operation':'activities/search:MINT,BURN:read-only','intent':pending,'pending_hash':pending_hash,'stage':stage,'exception_code':exception_code,'proof':proof,'manual_hash':manual_hash}
        identity=digest(body)
        # Immutable append-only incident evidence, including across restarts.
        db.execute("CREATE TRIGGER IF NOT EXISTS supply_incident_no_update BEFORE UPDATE ON runtime WHEN OLD.name LIKE 'supply_incident:%' BEGIN SELECT RAISE(ABORT,'Immutable supply incident'); END")
        db.execute("CREATE TRIGGER IF NOT EXISTS supply_incident_no_delete BEFORE DELETE ON runtime WHEN OLD.name LIKE 'supply_incident:%' BEGIN SELECT RAISE(ABORT,'Immutable supply incident'); END")
        db.execute("CREATE TRIGGER IF NOT EXISTS supply_incident_no_replace BEFORE INSERT ON runtime WHEN NEW.name LIKE 'supply_incident:%' AND EXISTS(SELECT 1 FROM runtime WHERE name=NEW.name) BEGIN SELECT RAISE(ABORT,'Immutable supply incident'); END")
        db.execute('INSERT INTO runtime VALUES(?,?)',('supply_incident:'+identity,encode(body)))
        db.execute("UPDATE jobs SET state='quarantined',error='Unresolved collection incident; last good retained' WHERE id=?",(live['id'],))
        db.execute("DELETE FROM runtime WHERE name='pending_request' AND value=?",(runtime['pending_request'],))
        if manual_hash is not None: db.execute("DELETE FROM runtime WHERE name='manual_block' AND value='1'")
        return identity
    def quarantine_registry(self,db):
        """Validate exact immutable job/source identity; corruption fails closed."""
        found={}
        try:
            for row in db.execute("SELECT name,value FROM runtime WHERE name LIKE 'supply_incident:%'"):
                b=json.loads(row['value']);identity=row['name'].split(':',1)[1]
                if not isinstance(b,dict) or set(b)!={'version','status','job','request','operation','intent','pending_hash','stage','exception_code','proof','manual_hash'} or b['version']!=1: return None
                if (b['stage'],b['exception_code'],b['proof']) not in (('historical_unknown','unknown','operator_protected'),('page_validation','ValueError','adapter_closed_success')): return None
                if (b['stage']=='historical_unknown' and b['manual_hash'] not in (None,digest('1'))) or (b['stage']=='page_validation' and b['manual_hash'] is not None): return None
                if not isinstance(b['intent'],dict) or set(b['intent'])!={'job','token','started'} or not isinstance(b['intent']['token'],str) or not re.fullmatch('[a-f0-9]{32}',b['intent']['token']): return None
                stamp(b['intent']['started'])
                if digest(b)!=identity or b['status']!='unresolved/operator-review' or b['operation']!='activities/search:MINT,BURN:read-only': return None
                j=b['job'];live=db.execute('SELECT * FROM jobs WHERE id=?',(j['id'],)).fetchone()
                if not live or j['id'] in found or b['request']!=self.request(j) or b['intent']['job']!=j['id'] or digest(b['intent'])!=b['pending_hash']: return None
                expected={**j,'state':'quarantined','error':'Unresolved collection incident; last good retained'}
                if dict(live)!=expected or collection(j['contract'])!=j['contract'] or not db.execute('SELECT 1 FROM contracts WHERE contract=?',(j['contract'],)).fetchone(): return None
                if j['mode'] not in ('backfill','incremental','reconcile') or b['proof'] not in ('operator_protected','adapter_closed_success'): return None
                found[j['id']]=b
            if {x[0] for x in db.execute("SELECT id FROM jobs WHERE state='quarantined'")}!=set(found): return None
        except (KeyError,ValueError,TypeError,AttributeError): return None
        return found
    def next_job(self,now):
        with self.connect(readonly=True) as db:
            r=db.execute("SELECT j.*,c.standard FROM jobs j JOIN contracts c USING(contract) WHERE state IN ('queued','running','deferred') AND due<=? ORDER BY serviced,id LIMIT 1",(now,)).fetchone()
            return dict(r) if r else None
    def request(self,job):
        f={'blockchains':['POLYGON'],'collections':[job['contract']],'types':['MINT','BURN'],'to':job['upper_at']}
        if job['lower_at']: f['from']=job['lower_at']
        r={'size':1000,'sort':'EARLIEST','filter':f}
        if job['cursor']: r['cursor']=job['cursor']
        return r
    def totals(self,db,c):
        buckets={}; first=None; tokens={}
        rows=db.execute('SELECT e.*,m.rarity FROM events e LEFT JOIN metadata_current mc ON mc.asset=e.asset LEFT JOIN metadata m ON m.asset=mc.asset AND m.digest=mc.digest WHERE e.contract=? AND e.reverted=0',(c,))
        for e in rows:
            rarity=e['rarity'] or 'Unknown'; b=buckets.setdefault(rarity,{'rarity':rarity,'minted':0,'burned':0,'exists':0})
            n=int(e['quantity']); b['minted' if e['kind']=='MINT' else 'burned']+=n
            tokens[e['asset']]=tokens.get(e['asset'],0)+(n if e['kind']=='MINT' else -n)
            if e['kind']=='MINT' and n and (first is None or e['date']<first): first=e['date']
        for b in buckets.values(): b['exists']=b['minted']-b['burned']
        standard=db.execute('SELECT standard FROM contracts WHERE contract=?',(c,)).fetchone()[0]
        invalid=any(x<0 or (standard=='ERC721' and x>1) for x in tokens.values())
        if invalid:
            for b in buckets.values(): b['exists']=None
        minted=sum(b['minted'] for b in buckets.values());burned=sum(b['burned'] for b in buckets.values())
        return {'minted':minted if buckets else None,'burned':burned if buckets else None,'exists':None if invalid or not buckets else minted-burned,'rarities':sorted(buckets.values(),key=lambda b:b['rarity']),'first_mint':first,'distinct_token_ids':len(tokens),'balance_valid':not invalid,'blockchain_complete':False}
    def commit_page(self,job,response,now=None):
        now=time.time() if now is None else now
        # Archive transport result BEFORE interpretation; rejected raw pages are
        # retained without advancing checkpoints or replacing completed results.
        with self.connect() as db:
            receipt=db.execute('INSERT INTO receipts(job,request,response,sha256,retrieved_at) VALUES(?,?,?,?,?)',(job['id'],encode(self.request(job)),encode(response),digest(response),utc(now))).lastrowid
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                self._apply_page(db,job,response,receipt,now)
        except Exception:
            with self.connect() as db: db.execute('UPDATE receipts SET error=? WHERE id=?',('Rejected page; inspect private evidence',receipt))
            raise
        return receipt
    def _apply_page(self,db,job,response,receipt,now):
        if not isinstance(response,dict) or not isinstance(response.get('activities'),list): raise ValueError('Malformed page')
        cursor=response.get('cursor')
        if cursor is not None and (not isinstance(cursor,str) or not cursor or cursor==job['cursor'] or len(cursor)>8192): raise ValueError('Invalid/repeated cursor')
        normalized=[(raw,normalize(raw,job['contract'],job['standard'])) for raw in response['activities']]
        live=db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
        if live['cursor']!=job['cursor'] or live['state'] not in ('queued','running','deferred'): raise ValueError('Stale page checkpoint')
        for raw,n in normalized:
            if not (stamp(job['upper_at'])>=n['date'] and (not job['lower_at'] or n['date']>=stamp(job['lower_at']))): raise ValueError('Activity outside frozen range')
            h=digest(raw)
            db.execute('INSERT OR IGNORE INTO revisions VALUES(?,?,?)',(n['id'],h,encode(raw)))
            db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?)',(receipt,n['id'],h))
            order=(n['updated'],n['version'])
            prior=db.execute('SELECT e.* FROM events e JOIN event_aliases a ON a.event=e.event WHERE a.provider_id=?',(n['id'],)).fetchone()
            if prior and prior['event']!=n['event']:
                priororder=(prior['updated'],prior['version'])
                if order<priororder: continue
                if order==priororder: raise ValueError('Same-version log identity conflict')
                # Move every known duplicate representation with the revised
                # provider identity; stale alias rows cannot resurrect it.
                db.execute('UPDATE event_aliases SET event=? WHERE event=?',(n['event'],prior['event']))
                db.execute('DELETE FROM events WHERE event=?',(prior['event'],))
            old=db.execute('SELECT * FROM events WHERE event=?',(n['event'],)).fetchone()
            if old:
                oldorder=(old['updated'],old['version'])
                if order<oldorder:
                    # Even stale duplicate representations establish an
                    # alias: later log-identity revisions must move the
                    # existing contribution, never append a second one.
                    db.execute('INSERT OR REPLACE INTO event_aliases VALUES(?,?)',(n['id'],n['event']))
                    continue
                # Duplicate provider representation is okay only when it
                # agrees on the supply contribution at the same version.
                if order==oldorder and any(str(n[k])!=str(old[k]) for k in ('quantity','reverted','date')): raise ValueError('Conflicting event revision')
            db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)',(n['event'],job['contract'],n['asset'],n['id'],h,n['kind'],n['quantity'],n['date'],n['updated'],n['version'],n['reverted']))
            db.execute('INSERT OR REPLACE INTO event_aliases VALUES(?,?)',(n['id'],n['event']))
        db.execute('UPDATE receipts SET accepted=1 WHERE id=?',(receipt,))
        db.execute('UPDATE jobs SET cursor=?,state=?,pages=pages+1,serviced=?,failures=0,error=NULL WHERE id=?',(cursor,'running' if cursor else 'exhausted',now,job['id']))
        if cursor is None:
            result=self.totals(db,job['contract'])
            if result['balance_valid']:
                g=db.execute('INSERT INTO generations(job,contract,result,created_at) VALUES(?,?,?,?)',(job['id'],job['contract'],encode(result),utc(now))).lastrowid
                from mtd_rarity_queue import capture_basis
                capture_basis(db,g,job['contract'])
                db.execute('UPDATE jobs SET published=? WHERE id=?',(g,job['id']))
            else: db.execute('UPDATE jobs SET error=? WHERE id=?',('Invalid per-token balance: supply withheld; provider coverage incomplete',job['id']))
        db.execute("DELETE FROM runtime WHERE name='pending_request'")

    def replay_cached_page(self,receipt_id,receipt_hash,pending_hash,job_hash,manual_hash,*,now=None,db=None):
        """Internal cached-only apply; operator caller must own protected provider boundary.

        No transport, no new receipt, no retry/cause inference. Fences and application
        share one IMMEDIATE transaction; all original failure evidence is immutable.
        """
        now=time.time() if now is None else now
        if db is None:
            with self.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                return self.replay_cached_page(receipt_id,receipt_hash,pending_hash,job_hash,manual_hash,now=now,db=conn)
        runtime=dict(db.execute('SELECT name,value FROM runtime'))
        if any(k in runtime for k in ('auth_blocked','not_before','shared_gate')): raise ValueError('Provider-wide restrictions retained')
        intent=json.loads(runtime.get('pending_request','null'))
        if not isinstance(intent,dict) or set(intent)!={'job','token','started'} or not isinstance(intent['token'],str) or not re.fullmatch('[a-f0-9]{32}',intent['token']) or digest(intent)!=pending_hash: raise ValueError('Pending intent fence changed')
        stamp(intent['started'])
        manual=runtime.get('manual_block')
        if not isinstance(manual,str) or digest(manual)!=manual_hash: raise ValueError('Manual fence changed')
        diagnostic=json.loads(manual)
        if not isinstance(diagnostic,dict) or diagnostic.get('classification')!='global-required-review' or diagnostic.get('stage')!='page_commit' or diagnostic.get('exception_code')!='OperationalError': raise ValueError('Not a reviewed local SQL commit failure')
        if set(diagnostic)-{'classification','stage','exception_code','sqlite_errorcode','sqlite_errorname'}: raise ValueError('Unrecognized manual restriction')
        j=db.execute('SELECT * FROM jobs WHERE id=?',(intent['job'],)).fetchone()
        if not j or digest(dict(j))!=job_hash or j['state']!='blocked' or j['published'] is not None or j['mode'] not in ('backfill','incremental','reconcile'): raise ValueError('Job fence changed')
        job=dict(j)
        contract=db.execute('SELECT standard FROM contracts WHERE contract=?',(job['contract'],)).fetchone()
        if not contract or collection(job['contract'])!=job['contract']: raise ValueError('Scope changed')
        job['standard']=contract['standard']
        r=db.execute('SELECT * FROM receipts WHERE id=?',(receipt_id,)).fetchone()
        if not r or digest(dict(r))!=receipt_hash or r['job']!=job['id'] or r['accepted']!=0 or r['error']!='Rejected page; inspect private evidence' or r['request']!=encode(self.request(job)): raise ValueError('Cached receipt fence changed')
        response=json.loads(r['response'])
        if digest(response)!=r['sha256']: raise ValueError('Cached payload hash changed')
        body={'version':1,'operation':'cached-local-apply:no-HTTP','intent':intent,'pending_hash':pending_hash,'job_before':dict(j),'receipt_before':dict(r),'manual_before':manual,'at':utc(now)}
        identity=digest(body)
        for suffix,action,condition in (('update','UPDATE',"OLD.name LIKE 'supply_cached_replay:%'"),('delete','DELETE',"OLD.name LIKE 'supply_cached_replay:%'"),('replace','INSERT',"NEW.name LIKE 'supply_cached_replay:%' AND EXISTS(SELECT 1 FROM runtime WHERE name=NEW.name)")):
            db.execute("CREATE TRIGGER IF NOT EXISTS supply_cached_no_"+suffix+" BEFORE "+action+" ON runtime WHEN "+condition+" BEGIN SELECT RAISE(ABORT,'Immutable cached replay evidence'); END")
        db.execute('INSERT INTO runtime VALUES(?,?)',('supply_cached_replay:'+identity,encode(body)))
        db.execute("UPDATE jobs SET state='queued' WHERE id=?",(job['id'],))
        job['state']='queued'
        self._apply_page(db,job,response,receipt_id,now)
        db.execute('UPDATE receipts SET error=NULL WHERE id=?',(receipt_id,))
        if db.execute("DELETE FROM runtime WHERE name='manual_block' AND value=?",(manual,)).rowcount!=1: raise ValueError('Manual acknowledgement changed')
        return identity
    def schedule_updates(self,now):
        """Saved-only eligibility; one healthy batch plus verified recovery jobs.

        observed acknowledges exhausted queries, not valid supply. completed only
        advances for fully published scope. Invalid members retain evidence in a
        separate full-history recovery lane, with ordinary bounded incrementals
        between monthly full recoveries. Neither lane fabricates valid supply.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try: zone=ZoneInfo('America/New_York')
        except ZoneInfoNotFoundError:
            with (HOME/'hermes-agent/venv/Lib/site-packages/tzdata/zoneinfo/America/New_York').open('rb') as f:
                zone=ZoneInfo.from_file(f,key='America/New_York')
        local=datetime.fromtimestamp(now,zone)
        hour=max(h for h in (0,8,12,16,20) if h<=local.hour)
        slot=local.replace(hour=hour,minute=0,second=0,microsecond=0).timestamp()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved=db.execute("SELECT value FROM runtime WHERE name='refresh_cadence'").fetchone()
            cadence=json.loads(saved[0]) if saved else {'activated':now,'completed':None,'pending':None}
            def persist():
                db.execute("INSERT OR REPLACE INTO runtime VALUES('refresh_cadence',?)",(encode(cadence),))
            if not saved: persist()
            quarantines=self.quarantine_registry(db)
            if quarantines is None: return 'blocked'
            quarantine_ids=set(quarantines)
            quarantine_contracts={b['job']['contract'] for b in quarantines.values()}
            # Every approved contract needs its initial query exhausted, not just
            # a page, a published first contract, or a misleading chain-complete flag.
            if db.execute("SELECT 1 FROM contracts c WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.contract=c.contract AND j.mode='backfill' AND j.state IN ('exhausted','quarantined')) LIMIT 1").fetchone(): return
            cadence.setdefault('observed',cadence['completed'])
            cadence.setdefault('recoveries',[])
            # Durable identities authorize this narrow lane, never mode alone.
            # Retain exhausted entries too: a missing member/source fails closed.
            if not isinstance(cadence['recoveries'],list): return 'blocked'
            recovery_ids=set()
            for member in cadence['recoveries']:
                if not isinstance(member,dict) or set(member)!= {'job','source'}: return 'blocked'
                identity=member['job']; source=member['source']
                if type(identity) is not int or type(source) is not int or identity in recovery_ids: return 'blocked'
                j=db.execute('SELECT * FROM jobs WHERE id=?',(identity,)).fetchone()
                origin=db.execute('SELECT * FROM jobs WHERE id=?',(source,)).fetchone()
                if (not j or not origin or source>=identity or j['contract']!=origin['contract'] or
                    j['mode']!='recovery' or j['lower_at'] is not None or origin['state']!='exhausted' or
                    origin['published'] is not None or origin['error']!='Invalid per-token balance: supply withheld; provider coverage incomplete'):
                    return 'blocked'
                if j['state'] not in ('queued','running','deferred','exhausted'): return 'blocked'
                if j['state']=='exhausted' and j['published'] is None and j['error']!='Invalid per-token balance: supply withheld; provider coverage incomplete': return 'blocked'
                recovery_ids.add(identity)
            if {j[0] for j in db.execute("SELECT id FROM jobs WHERE mode='recovery'")}!=recovery_ids: return 'blocked'
            pending=cadence['pending']
            if pending:
                if not pending['jobs'] or len(set(pending['jobs']))!=len(pending['jobs']) or recovery_ids.intersection(pending['jobs']): return 'blocked'
                members=[db.execute('SELECT * FROM jobs WHERE id=?',(identity,)).fetchone() for identity in pending['jobs']]
                if any(j is None for j in members): return 'blocked'
                for job in members:
                    if job['id'] in quarantine_ids: continue
                    if job['state'] not in ('queued','running','deferred','exhausted'): return 'blocked'
                    # Validate every member before allowing any nonterminal return:
                    # list order must not hide a blocked or unknown terminal result.
                    if job['state']=='exhausted' and job['published'] is None and job['error']!='Invalid per-token balance: supply withheld; provider coverage incomplete': return 'blocked'
                if any(job['state']!='exhausted' and job['id'] not in quarantine_ids for job in members): return
                cadence['observed']=pending['slot']
                cadence['observed_excluded_contracts']=sorted(set(pending.get('excluded_contracts',[])+[j['contract'] for j in members if j['published'] is None]))
                unresolved=db.execute("SELECT 1 FROM jobs j WHERE j.id=(SELECT MAX(x.id) FROM jobs x WHERE x.contract=j.contract) AND j.published IS NULL LIMIT 1").fetchone()
                if not unresolved and not pending.get('excluded_contracts'): cadence['completed']=pending['slot']
                cadence['pending']=None
                persist()
            if slot<=cadence['activated'] or (cadence['observed'] is not None and slot<=cadence['observed']): return
            # Preserve any pre-policy in-flight/blocked job; never reset its guards.
            active=db.execute("SELECT id FROM jobs WHERE state IN ('queued','running','deferred','blocked')").fetchall()
            if any(j['id'] not in recovery_ids for j in active): return
            rows=db.execute("SELECT c.contract,MAX(j.id) latest FROM contracts c JOIN jobs j USING(contract) GROUP BY c.contract").fetchall()
            if not rows: return
            identities=[]; excluded=[]
            for row in rows:
                j=db.execute('SELECT * FROM jobs WHERE id=?',(row['latest'],)).fetchone()
                if j['contract'] in quarantine_contracts:
                    excluded.append(j['contract']);continue
                if j['id'] in recovery_ids and j['state']!='exhausted':
                    excluded.append(j['contract']);continue
                last_full=db.execute("SELECT MAX(upper_at) FROM jobs WHERE contract=? AND mode IN ('backfill','reconcile','recovery') AND state='exhausted'",(j['contract'],)).fetchone()[0]
                full=not last_full or slot-datetime.fromisoformat(stamp(last_full)).timestamp()>=30*86400
                invalid=j['state']=='exhausted' and j['published'] is None and j['error']=='Invalid per-token balance: supply withheld; provider coverage incomplete'
                if invalid:
                    recovery=db.execute("SELECT MAX(upper_at) FROM jobs WHERE contract=? AND mode IN ('reconcile','recovery') AND state='exhausted'",(j['contract'],)).fetchone()[0]
                    # Keep observing recent evidence during the full-history cooldown.
                    # Ordinary incremental membership drains withheld, never published;
                    # only full recovery gets the registered independent-lane exemption.
                    full=not recovery or slot-datetime.fromisoformat(stamp(recovery)).timestamp()>=30*86400
                recovering=invalid and full
                if recovering: excluded.append(j['contract'])
                lower=None if full else utc(datetime.fromisoformat(stamp(j['upper_at'])).timestamp()-7*86400)
                # New recurring work joins at enqueue age, not artificial age zero.
                # Preserve least-serviced fairness across batch rollovers; initial
                # historical jobs and every existing checkpoint remain untouched.
                identity=db.execute('INSERT INTO jobs(contract,mode,lower_at,upper_at,serviced) VALUES(?,?,?,?,?)',(j['contract'],'recovery' if recovering else ('reconcile' if full else 'incremental'),lower,utc(slot),now)).lastrowid
                if recovering: cadence['recoveries'].append({'job':identity,'source':j['id']})
                else: identities.append(identity)
            if identities: cadence['pending']={'slot':slot,'jobs':identities,'excluded_contracts':excluded}
            else:
                cadence['observed']=slot
                cadence['observed_excluded_contracts']=excluded
            persist()
    def save_metadata(self,asset,rarity,source,raw,now=None):
        parts=asset.split(':')
        if len(parts)!=3 or collection(':'.join(parts[:2]))!= ':'.join(parts[:2]) or not re.fullmatch('[0-9]+',parts[2]): raise ValueError('Exact asset required')
        if not isinstance(rarity,str) or not rarity.strip(): rarity='Unknown'
        h=digest({'source':source,'raw':raw,'rarity':rarity})
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM contracts WHERE contract=?',(':'.join(parts[:2]),)).fetchone(): return
            db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?,?,?,?,?)',(asset,h,rarity,source,encode(raw),utc(time.time() if now is None else now)))
            db.execute('INSERT OR REPLACE INTO metadata_current VALUES(?,?)',(asset,h))
    def report(self,sort='supply'):
        if sort not in ('supply','oldest','newest'): raise ValueError('Invalid sort')
        with self.connect(readonly=True) as db:
            contracts=[];incidents=[]
            registry=self.quarantine_registry(db)
            quarantined={b['job']['contract']:(digest(b),b) for b in (registry or {}).values()}
            for c in db.execute('SELECT * FROM contracts'):
                j=dict(db.execute('SELECT * FROM jobs WHERE contract=? ORDER BY id DESC LIMIT 1',(c['contract'],)).fetchone())
                g=db.execute('SELECT * FROM generations WHERE contract=? ORDER BY id DESC LIMIT 1',(c['contract'],)).fetchone()
                progress=self.totals(db,c['contract'])
                # Keep last completed provider generation stable on pending/failure.
                result=json.loads(g['result']) if g else progress
                if g and db.execute("SELECT 1 FROM sqlite_master WHERE name='rarity_publications'").fetchone():
                    publication=db.execute('SELECT result FROM rarity_publications WHERE generation=? ORDER BY id DESC LIMIT 1',(g['id'],)).fetchone()
                    if publication: result=json.loads(publication[0])
                freshness=dict(db.execute("SELECT MAX(r.retrieved_at) last_attempted_at,MAX(CASE WHEN r.accepted=1 THEN r.retrieved_at END) last_accepted_at FROM receipts r JOIN jobs x ON x.id=r.job WHERE x.contract=?",(c['contract'],)).fetchone())
                # serviced also seeds recurring queue age. A queued job is not
                # an attempted request unless durable request intent exists.
                intent=db.execute("SELECT value FROM runtime WHERE name='pending_request'").fetchone()
                try: pending_job=json.loads(intent[0]).get('job') if intent else None
                except (ValueError,TypeError,AttributeError): pending_job=None
                attempted=utc(j['serviced']) if j['serviced'] and (j['state']!='queued' or pending_job==j['id']) else None
                freshness['last_attempted_at']=max(filter(None,(freshness['last_attempted_at'],attempted)),default=None)
                freshness['query_exhausted_through']=db.execute("SELECT MAX(upper_at) FROM jobs WHERE contract=? AND state='exhausted'",(c['contract'],)).fetchone()[0]
                freshness['published_through']=db.execute('SELECT upper_at FROM jobs WHERE id=?',(g['job'],)).fetchone()[0] if g else None
                invalid=not progress['balance_valid']
                last_invalid=db.execute("SELECT id,error FROM jobs WHERE contract=? AND state='exhausted' AND published IS NULL AND error=? ORDER BY id DESC LIMIT 1",(c['contract'],'Invalid per-token balance: supply withheld; provider coverage incomplete')).fetchone() if invalid else None
                recovery=db.execute("SELECT MAX(upper_at) FROM jobs WHERE contract=? AND mode IN ('reconcile','recovery') AND state='exhausted'",(c['contract'],)).fetchone()[0] if invalid else None
                validation={'validation_state':'withheld' if invalid else ('published' if g and j['published'] else 'pending'),'validation_error':last_invalid['error'] if last_invalid else None,'unresolved_job':last_invalid['id'] if last_invalid else None,'recovery_not_before':utc(datetime.fromisoformat(stamp(recovery)).timestamp()+30*86400) if recovery else None,'source_freshness':freshness}
                incident=None
                if c['contract'] in quarantined:
                    identity,body=quarantined[c['contract']]
                    incident={'incident':identity,'job':body['job']['id'],'kind':body['stage']}
                elif last_invalid:
                    incident={'incident':'balance:'+str(last_invalid['id']),'job':last_invalid['id'],'kind':'invalid-token-balance'}
                elif pending_job==j['id'] and j['state']=='blocked':
                    incident={'incident':'unknown:'+str(j['id']),'job':j['id'],'kind':'global-required-review'}
                if incident:
                    incident.update(contract=c['contract'],name=c['name'],status='unresolved/operator-review')
                    incidents.append(incident)
                validation['incident_status']='unresolved/operator-review' if incident else None
                contracts.append({**validation,**{k:c[k] for k in ('contract','name','standard')},**result,'progress':progress,'coverage':QUALIFIER if g else 'partial / unknown; blockchain incomplete','state':j['state'],'pages':j['pages'],'error':j['error'],'next_retry':utc(j['due']) if j['due'] else None,'mode':j['mode'],'as_of':g['created_at'] if g else None,'upper_at':j['upper_at']})
            rarity_metadata=None
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='rarity_work'").fetchone():
                from mtd_rarity_queue import progress as metadata_progress
                rarity_metadata=metadata_progress(db)
                metadata_contracts={r['contract']:r for r in rarity_metadata['contracts']}
                for c in contracts:c['rarity_metadata']=metadata_contracts.get(c['contract'])
            runtime=dict(db.execute('SELECT name,value FROM runtime'))
            counts={t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('receipts','revisions','events','generations')}
            counts['unresolved_incidents']=len(incidents)
            counts['incident_registry_valid']=registry is not None
            counts['accepted_pages']=db.execute('SELECT COUNT(*) FROM receipts WHERE accepted=1').fetchone()[0]
            counts['query_exhausted_contracts']=db.execute('SELECT COUNT(DISTINCT contract) FROM generations').fetchone()[0]
        def key(c):
            if sort=='supply': return (c['exists'] is None,-(c['exists'] or 0),c['name'],c['contract'])
            d=datetime.fromisoformat(c['first_mint']).timestamp() if c['first_mint'] else 0
            return (c['first_mint'] is None,d if sort=='oldest' else -d,c['name'],c['contract'])
        return {'incidents':incidents,'rarity_metadata':rarity_metadata,'contracts':sorted(contracts,key=key),'counts':counts,'runtime':runtime,'llm_tokens':0,'notifications':0,'blockchain_complete':False,'source':'Rarible indexed MINT/BURN; no configured Polygon RPC','generated_at':utc(time.time())}
