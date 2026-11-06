"""Conservative Rarible search contract; no network occurs on import/read.
Schema evidence: cache/scratch/nft-lifecycle-history-handoff.md.
"""
import re
from datetime import datetime

TYPES=('MINT','TRANSFER','BURN','LIST','BID','SELL','CANCEL_LIST','CANCEL_BID')
BASE='https://api.rarible.org/v0.1/'

def asset_key(value):
    if not isinstance(value,str) or not re.fullmatch(r'POLYGON:0x[0-9a-fA-F]{40}:[0-9]+',value):
        raise ValueError('Exact Polygon contract and decimal token ID required')
    chain,contract,token=value.split(':')
    return ':'.join((chain,contract.lower(),token))

def collections(values):
    result=[]
    for value in values:
        if not re.fullmatch(r'POLYGON:0x[0-9a-fA-F]{40}',value): raise ValueError('Invalid collection')
        result.append(value.lower().replace('polygon:','POLYGON:'))
    if not result: raise ValueError('Empty allowlist')
    return sorted(set(result))

def history_request(asset, *, cursor=None, lower=None, upper=None, size=100, sources=None):
    if not isinstance(size,int) or not 1<=size<=1000: raise ValueError('Invalid page size')
    f={'blockchains':['POLYGON'],'items':[asset_key(asset)],'types':list(TYPES)}
    for key,value in (('from',lower),('to',upper)):
        if value:
            datetime.fromisoformat(value.replace('Z','+00:00')); f[key]=value
    if sources is not None: raise ValueError('All-type history must not exclude source-less NFT movements')
    r={'size':size,'sort':'LATEST','filter':f}
    if cursor is not None: r['cursor']=cursor
    return r

def ownership_request(wallet, allowlist, *, continuation=None, size=100):
    from mtd_wallet_transfer_analyzer import validate_wallet
    if not isinstance(size,int) or not 1<=size<=1000: raise ValueError('Invalid page size')
    r={'size':size,'sort':'LATEST','filter':{'blockchains':['POLYGON'],'owners':['ETHEREUM:'+validate_wallet(wallet)],'collections':collections(allowlist)}}
    if continuation is not None: r['continuation']=continuation
    return r

def identities(raw):
    """Walk only authoritative NFT fields, cross-check redundant identity fields."""
    found=set()
    def node(value):
        if not isinstance(value,dict): return
        if (value.get('itemId') or value.get('tokenId') is not None) and value.get('blockchain','POLYGON')!='POLYGON': raise ValueError('Unexpected NFT blockchain')
        local=set()
        if value.get('itemId'): local.add(asset_key(value['itemId']))
        contract=value.get('contract') or value.get('collection')
        if contract and value.get('tokenId') is not None:
            if contract.startswith('POLYGON:'): contract=contract.split(':')[1]
            local.add(asset_key('POLYGON:'+contract+':'+str(value['tokenId'])))
        if len(local)>1: raise ValueError('Conflicting NFT identities')
        found.update(local)
        for key in ('type','nft','make','take'):
            node(value.get(key))
    node(raw)
    if len(found)>1: raise ValueError('Conflicting nested NFT identities')
    return found

def current_ownership(raw):
    value=raw.get('value')
    pending=raw.get('pending')
    return ((pending is False or (isinstance(pending,list) and not pending)) and isinstance(value,str) and bool(re.fullmatch(r'[0-9]+',value)) and int(value)>0)

class ProviderError(RuntimeError):
    def __init__(self, status=None, retry_after=0, invalid_cursor=False):
        self.status=status; self.retry_after=retry_after; self.invalid_cursor=invalid_cursor
        super().__init__('Provider request failed' if status is None else 'Provider HTTP '+str(status))

class ProviderDeferred(ProviderError):
    """Local lease/pacing wait, not an HTTP attempt or retry.

    Remains a ProviderError for existing adapter callers; the worker handles the
    more specific deferral before the ordinary failure/retry path.
    """
    def __init__(self,ready_at):
        self.ready_at=ready_at
        super().__init__(None)

class ProviderOutcome(RuntimeError):
    """Explicit outcome carrier; payload is NEVER written to guard metadata."""
    def __init__(self,*,payload=None,error=None,repair,diagnostic):
        self.payload=payload; self.error=error; self.repair=repair
        self.diagnostic=diagnostic
        super().__init__('Known provider outcome requires local filesystem repair')

class _LocalFailure(Exception):
    def __init__(self,operation,resource,error):
        self.operation=operation; self.resource=resource; self.error=error
        super().__init__('Managed local filesystem operation failed')

class RaribleAdapter:
    """Adapter-owned shared holder lease/pacing; NEVER retries transport."""
    def __init__(self,root,*,key=None,opener=None,clock=None,min_gap_seconds=None):
        from pathlib import Path
        import math
        from mtd_wallet_transfer_analyzer import MIN_CALL_GAP_SECONDS
        gap=MIN_CALL_GAP_SECONDS if min_gap_seconds is None else min_gap_seconds
        if type(gap) not in (int,float) or not math.isfinite(gap) or gap<MIN_CALL_GAP_SECONDS:
            raise ValueError('Minimum gap must be finite and at least the shared floor')
        self.min_gap_seconds=gap
        self.root=Path(root); self.key=key; self.opener=opener; self.clock=clock
        self.deadline=None
    def _now(self):
        import time
        return (self.clock or time.time)()
    def _local(self,operation,resource,fn):
        import errno,time
        for attempt in range(3):
            try: return fn()
            except OSError as exc:
                retry=exc.errno in (errno.EACCES,errno.EPERM,errno.EBUSY) or getattr(exc,'winerror',None) in (5,32,33)
                if not retry or attempt==2 or (self.deadline is not None and time.monotonic()>=self.deadline):
                    raise _LocalFailure(operation,resource,exc) from None
    def ready_at(self):
        """Read-only advisory; never repairs. Under-lease check is independent."""
        return self._pacing_ready()
    def _pacing_ready(self):
        import json,math
        from mtd_wallet_transfer_analyzer import MIN_CALL_GAP_SECONDS
        try:
            finished=json.loads((self.root/'holder-activity-pacing.json').read_text(encoding='utf-8'))['finished_epoch']
            if type(finished) not in (int,float) or not math.isfinite(finished) or finished<0:
                raise ValueError('Invalid pacing timestamp')
            return finished+self.min_gap_seconds
        except FileNotFoundError: return 0
        except (ValueError,KeyError,TypeError,OverflowError,RecursionError):
            raise OSError('Managed pacing record invalid; no provider request') from None
    def _pacing_write(self,finished):
        import json,os,tempfile
        from pathlib import Path
        pacing=self.root/'holder-activity-pacing.json'
        fd,temp=self._local('temp_create','pacing',lambda:tempfile.mkstemp(prefix=pacing.name+'.',suffix='.tmp',dir=self.root))
        error=None
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                stream.write(json.dumps({'finished_epoch':finished})); stream.flush()
                self._local('fsync','pacing_temp',lambda:os.fsync(stream.fileno()))
            self._local('replace','pacing',lambda:os.replace(temp,pacing))
        except (OSError,_LocalFailure) as exc: error=exc
        try:
            self._local('temp_remove','pacing_temp',lambda:Path(temp).unlink(missing_ok=True))
        except _LocalFailure as exc:
            if error is None: error=exc
            error.temp_name=Path(temp).name
        if error:
            if isinstance(error,_LocalFailure): raise error
            raise _LocalFailure('write','pacing_temp',error)
    def _release(self,plan,*,before_delete=None):
        from mtd_nft_history_local import delete_owned
        return self._local('release','holder_lease',lambda:delete_owned(self.root/'holder-activity-state.lock',plan['identity'],plan['token'],before_delete=before_delete))
    def repair_local(self,plan):
        """Explicit worker repair hook, never called by ready_at/status reads."""
        import math,json,stat
        from mtd_nft_history_local import identity
        if not isinstance(plan,dict) or set(plan)!={'version','token','identity','finished_epoch','pacing_pending','temp_name','manual'}: return False
        if plan['version']!=1 or type(plan['manual']) is not bool or plan['manual']: return False
        if not isinstance(plan['token'],str) or not re.fullmatch('[a-f0-9]{32}',plan['token']): return False
        if not isinstance(plan['identity'],list) or len(plan['identity'])!=2 or any(type(x) is not int or x<0 for x in plan['identity']): return False
        if type(plan['pacing_pending']) is not bool: return False
        finished=plan['finished_epoch']
        if finished is not None and (type(finished) not in (int,float) or not math.isfinite(finished) or finished<0): return False
        if plan['pacing_pending'] and finished is None: return False
        temp=plan['temp_name']
        if temp is not None and (not isinstance(temp,str) or not re.fullmatch(r'holder-activity-pacing\.json\.[a-zA-Z0-9_-]+\.tmp',temp)): return False
        try:
            if plan['identity']==[0,0] and finished is None:
                # Acquisition failed before owning any lease. Prove that exact
                # managed operation is repaired using a new exclusive local-only
                # probe, not merely absence (which can hide a persistent ACL).
                import os
                lease=self.root/'holder-activity-state.lock'
                fd=self._local('acquire','holder_lease',lambda:os.open(lease,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600))
                probe=dict(plan)
                with os.fdopen(fd,'w',encoding='utf-8') as stream:
                    probe['identity']=identity(os.fstat(stream.fileno()))
                    def initialize():
                        stream.seek(0); stream.truncate()
                        json.dump({'pid':os.getpid(),'started_at':self._now(),'recovery_token':plan['token']},stream)
                        stream.flush()
                    self._local('initialize','holder_lease',initialize)
                return bool(self._release(probe,before_delete=self._pacing_ready))
            def pacing_repair():
                # Validate under the SAME owned exclusive HANDLE even when only
                # release failed. A known completion is a durable lower bound.
                from mtd_wallet_transfer_analyzer import MIN_CALL_GAP_SECONDS
                saved=self._pacing_ready()-self.min_gap_seconds
                if finished is not None and (plan['pacing_pending'] or saved<finished):
                    self._pacing_write(max(finished,saved))
            removed=self._release(plan,before_delete=pacing_repair)
            if removed: return True
            # Crash after exact-handle deletion but before DB acknowledgement is
            # idempotent only when pacing was already installed (or no transport).
            if finished is None:
                self.ready_at() # Permission/read validation, not a repair side effect.
                return True
            from mtd_wallet_transfer_analyzer import MIN_CALL_GAP_SECONDS
            return self._pacing_ready()-self.min_gap_seconds>=finished
        except (OSError,ValueError,TypeError,_LocalFailure): return False
    def __call__(self,endpoint,request):
        import json,os,secrets,time,urllib.request,urllib.error
        from email.utils import parsedate_to_datetime
        from mtd_wallet_transfer_analyzer import api_key,MIN_CALL_GAP_SECONDS,valid_order_id
        from mtd_nft_history_local import identity
        if endpoint not in ('orders','activities/search','ownerships/search','items/byCollection','items/byIds'): raise ValueError('Unsupported endpoint')
        if endpoint.startswith('items/'):
            from mtd_rarity_queue import validate_request
            validate_request(endpoint,request)
        if endpoint=='orders' and not valid_order_id(request.get('order_id')): raise ValueError('Malformed Polygon order identifier; no request made')
        key=self.key or api_key(self.root)
        lease=self.root/'holder-activity-state.lock'; token=secrets.token_hex(16)
        phase='not_started'; payload=None; error=None; failure=None
        plan={'version':1,'token':token,'identity':[0,0],'finished_epoch':None,'pacing_pending':False,'temp_name':None,'manual':False}
        owned=False; initialized=False
        try:
            try: fd=self._local('acquire','holder_lease',lambda:os.open(lease,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600))
            except _LocalFailure as exc:
                if isinstance(exc.error,FileExistsError): raise ProviderDeferred(self._now()+self.min_gap_seconds)
                raise
            owned=True
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                plan['identity']=identity(os.fstat(stream.fileno()))
                def initialize():
                    stream.seek(0); stream.truncate()
                    json.dump({'pid':os.getpid(),'started_at':self._now(),'recovery_token':token},stream)
                    stream.flush()
                self._local('initialize','holder_lease',initialize)
            initialized=True
            ready=self._local('read','pacing',self._pacing_ready)
            if ready>self._now(): raise ProviderDeferred(ready)
            remaining=60 if self.deadline is None else min(60,self.deadline-time.monotonic())
            if remaining<=0: raise ProviderDeferred(self._now()+self.min_gap_seconds)
            from urllib.parse import urlencode
            is_get=endpoint in ('orders','items/byCollection')
            suffix=('orders/'+request['order_id'] if endpoint=='orders' else endpoint)
            if endpoint=='items/byCollection': suffix+='?'+urlencode(request)
            req=urllib.request.Request(BASE+suffix,data=None if is_get else json.dumps(request).encode(),method='GET' if is_get else 'POST',headers={'X-API-KEY':key,'Accept':'application/json','Content-Type':'application/json','User-Agent':'Hermes-MTD-NFT-History'})
            phase='entered'
            try:
                with (self.opener or urllib.request.urlopen)(req,timeout=remaining) as response:
                    payload=json.loads(response.read().decode('utf-8'))
                if not isinstance(payload,dict): raise ValueError('Malformed response')
                phase='response_received'
            except urllib.error.HTTPError as exc:
                phase='response_received'
                value=exc.headers.get('Retry-After','') if exc.headers else ''
                try: after=max(0,float(value))
                except ValueError:
                    try: after=max(0,parsedate_to_datetime(value).timestamp()-self._now())
                    except (ValueError,TypeError,OverflowError): after=0
                invalid=False
                if exc.code==400:
                    try: invalid=json.loads(exc.read(8192)).get('code') in ('INVALID_CURSOR','INVALID_CONTINUATION')
                    except (ValueError,TypeError,AttributeError): pass
                if not (endpoint=='orders' and exc.code==404): error=ProviderError(exc.code,retry_after=after,invalid_cursor=invalid)
            finally:
                plan['finished_epoch']=self._now(); plan['pacing_pending']=True
                try:
                    self._pacing_write(plan['finished_epoch']); plan['pacing_pending']=False
                except _LocalFailure as exc:
                    failure=exc; plan['temp_name']=getattr(exc,'temp_name',None)
        except _LocalFailure as exc:
            failure=exc; error=ProviderDeferred(self._now()+300)
            if owned and not initialized: plan['manual']=True
        except Exception as exc: error=exc
        finally:
            if owned and initialized and not plan['pacing_pending'] and not (failure is not None and failure.resource=='pacing' and failure.operation=='read'):
                try: self._release(plan)
                except (_LocalFailure,ValueError) as exc:
                    if failure is None: failure=exc if isinstance(exc,_LocalFailure) else _LocalFailure('release','holder_lease',exc)
            elif owned and not initialized: plan['manual']=True
        if failure is not None:
            cause=failure.error
            diagnostic={'operation':failure.operation,'resource':failure.resource,'transport_phase':phase,'class':type(cause).__name__,'errno':getattr(cause,'errno',None),'winerror':getattr(cause,'winerror',None),'provider_status':getattr(error,'status',None),'retry_after':getattr(error,'retry_after',0)}
            if phase=='entered':
                if error is not None: raise error
                raise RuntimeError('Unknown provider outcome; manual review required')
            raise ProviderOutcome(payload=payload,error=error,repair=plan,diagnostic=diagnostic) from None
        if error is not None: raise error
        return payload

