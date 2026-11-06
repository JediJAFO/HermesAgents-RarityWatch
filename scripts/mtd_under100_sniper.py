"""Saved-only all-approved-contract native POL <100 sniper.

Import is inert. No token, rarity or marketplace requests are implemented here.
The producer remains mtd_listing_wallets.py; this consumer owns separate state.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

HOME = Path(__file__).resolve().parents[1]
ROOT = HOME / 'price-watches'
SOURCE = ROOT / 'mtd-listing-wallets.json'
CATALOG = ROOT / 'mcfarlane-dc-sales.json'
STATE = ROOT / 'mtd-under100-sniper.json'
LOCK = ROOT / '.mtd-under100-sniper-lock'
LEDGER = ROOT / 'mtd-under100-sniper-discord.json'
CHANNEL_ID = '__PRIVATE_DESTINATION_ID__'
CHANNEL_NAME = 'mtd-lowestprices-sniper'
GUILD_ID = '__PRIVATE_DESTINATION_ID__'
MAX_AGE_SECONDS = 36*3600
ADDRESS = re.compile(r'0x[0-9a-f]{40}')
ORDER = re.compile(r'POLYGON:0x[0-9a-fA-F]{64}')

def stamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None: raise ValueError('Timezone required')
    return dt.timestamp()

def decimal(value):
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except InvalidOperation:
        return None

def inventory(source, scope, now):
    """Validate the complete producer generation before considering any offers.

    Pending/incomplete refreshes are deliberately not consumed, even if a last
    good snapshot exists: retry the saved consumer after the producer completes.
    """
    try:
        if not scope or any(not ADDRESS.fullmatch(c) or not isinstance(n,str) or not n for c,n in scope.items()):
            raise ValueError('Invalid canonical scope')
        snap = source['snapshot']
        pending = source['pending']
        refresh = snap['refresh']
        current = stamp(now); end = stamp(snap['as_of']); start = stamp(snap['started_at'])
        caught = stamp(refresh['sync_caught_up_at'])
        reconciled = stamp(refresh['last_reconciled_at'])
        if (source['contracts'] != scope or snap['contracts'] != scope
                or pending.get('stage') != 'complete' or pending.get('error')
                or refresh['version'] != 2 or not isinstance(refresh['sync_cursor'],str) or not refresh['sync_cursor']
                or stamp(refresh['sync_started_at']) != start
                or not start <= caught <= end <= current
                or not 0 <= current-end <= MAX_AGE_SECONDS
                or not 0 <= end-reconciled < 7*86400
                or not isinstance(snap['snapshot_id'],str) or not snap['snapshot_id']
                or not isinstance(snap['orders'],dict)):
            raise ValueError('Incomplete, stale or scope-mismatched producer generation')
        for key,row in snap['orders'].items():
            price=decimal(row.get('price')); units=decimal(row.get('units'))
            token=row.get('token_id'); contract=row.get('contract')
            if (not ORDER.fullmatch(key) or row.get('order_id') != key or contract not in scope
                    or not isinstance(token,str) or not re.fullmatch(r'[0-9]+',token)
                    or row.get('item_id') != 'POLYGON:'+contract+':'+token
                    or not ADDRESS.fullmatch(row.get('wallet',''))
                    or price is None or price < 0 or units is None or units <= 0 or units != units.to_integral_value()
                    or not isinstance(row.get('currency'),str) or not row['currency']
                    or stamp(row['verified_at']) > end):
                raise ValueError('Malformed normalized producer order')
            if row.get('ended_at'): stamp(row['ended_at'])
    except (KeyError,TypeError,AttributeError,OverflowError) as exc:
        raise ValueError('Invalid producer schema') from exc
    best = {}
    for row in snap['orders'].values():
        if row.get('ended_at') and stamp(row['ended_at']) <= current: continue
        price = decimal(row.get('price'))
        if row.get('contract') not in scope or row.get('currency') != 'POL' or price is None or not 0 < price < 100:
            continue
        key = (price, int(row['token_id']), row['order_id'])
        old = best.get(row['contract'])
        if old is None or key < old[0]:
            result = deepcopy(row)
            result['collection'] = scope[row['contract']]
            result['price'] = format(price, 'f')
            meta=(refresh.get('metadata') or {}).get(row['item_id'],{})
            result['rarity']=meta.get('rarity') or 'Unknown'
            result['item_name']=meta.get('name')
            evidence=(refresh.get('order_metadata') or {}).get(row['order_id'],{})
            if evidence.get('item_id')==row['item_id'] and decimal(evidence.get('price'))==price:
                result.update({k:evidence.get(k) for k in ('created_at','started_at','last_updated_at')})
            result['url'] = 'https://mcfarlanetoys.digital/explore/POLYGON:'+row['contract']+'/?sort=cheapest&inStockOnly=true'
            best[row['contract']] = (key, result)
    return {'rows': sorted([v[1] for v in best.values()], key=lambda r:(Decimal(r['price']),r['contract'],int(r['token_id']),r['order_id'])),
            'source_as_of':snap['as_of'], 'source_started_at':snap['started_at'],
            'generation':snap['started_at']+'|'+snap['as_of']+'|'+snap['snapshot_id']}

def advance(state, source, scope, now):
    """Pure transition: qualification changes need two different complete passes.

    The content hash alone is NOT a generation ID (unchanged sync passes reuse
    it). Both producer start and publication timestamps must advance.
    Newly added scope contracts establish a silent baseline; retained contracts
    keep confirmation state, delivery debt and daily idempotency acknowledgements.
    """
    observed = inventory(source, scope, now)
    rows = {r['contract']:r for r in observed['rows']}
    if not state:
        return {'schema':1, 'scope':deepcopy(scope), 'observed':observed,
                'confirmed':sorted(rows), 'candidates':{}, 'alerts':{},
                'batches':{}, 'heartbeat_days':{}}
    previous = state['observed']
    if observed['generation'] == previous['generation'] and state.get('scope') == scope:
        result=deepcopy(state)
        result['observed']=observed
        return result
    if (stamp(observed['source_started_at']) <= stamp(previous['source_started_at'])
            or stamp(observed['source_as_of']) <= stamp(previous['source_as_of'])):
        raise ValueError('Out-of-order producer generation')
    result = deepcopy(state)
    result['observed'] = observed
    if state.get('scope') != scope:
        added = set(scope) - set(state['scope'])
        result['scope'] = deepcopy(scope)
        result['confirmed'] = sorted((set(state['confirmed']) & set(scope)) | (added & set(rows)))
        result['candidates'] = {c:v for c,v in result['candidates'].items() if c in scope}
        # Explicit removal cancels only out-of-scope queued events. Preserve
        # immutable batches/ledger references and daily acknowledgements: run()
        # retires only unattempted stale bodies; attempted ones fail closed.
        result['alerts'] = {e:r for e,r in result['alerts'].items() if r['contract'] in scope}
    confirmed = set(result['confirmed'])
    candidates = result['candidates']
    for contract in sorted(scope):
        present = contract in rows
        if present == (contract in confirmed):
            candidates.pop(contract,None)
            continue
        candidate = candidates.get(contract)
        if candidate and candidate['present'] == present:
            candidates.pop(contract,None)
            if present:
                confirmed.add(contract)
                event = hashlib.sha256((contract+'|'+observed['generation']).encode()).hexdigest()[:24]
                result['alerts'][event] = deepcopy(rows[contract])
            else:
                confirmed.discard(contract)
        else:
            candidates[contract] = {'present':present, 'generation':observed['generation']}
    result['confirmed'] = sorted(confirmed)
    return result


def listing_date(row, now):
    """Exact order provenance only; an invalid/future clock never invents age."""
    from zoneinfo import ZoneInfo
    try:
        dates=[(stamp(row[k]),k) for k in ('created_at','started_at') if row.get(k)]
        if not dates or any(t<0 or t>stamp(now) for t,k in dates): return None
        value,field=max(dates)
        return {'value':row[field],'field':field,'label':'Listed '+datetime.fromtimestamp(value,ZoneInfo('America/New_York')).strftime('%b %d, %Y %I:%M %p %Z')}
    except (ValueError,TypeError,OverflowError): return None

def render(observed, day, now, *, aliases=None, rate=None, kind='daily'):
    """Full saved listing list; shared unique-alias policy, never raw sellers."""
    import importlib.util
    spec=importlib.util.spec_from_file_location('_under100_shared_aliases',ROOT/'mcfarlane_wallet_aliases.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    rows=observed['rows']
    rate=rate or {}
    usd=decimal(rate.get('rate'))
    if usd is not None and usd <= 0: usd=None
    heading='MTD under 100 POL — '+('Daily heartbeat '+day if kind=='daily' else 'Newly qualifying contracts')
    age=max(0,int(stamp(now)-stamp(observed['source_as_of'])))
    lines=[heading, f'{len(rows)} qualifying contracts · native POL strictly <100 · one cheapest listing per contract',
           'Saved source: '+observed['source_started_at']+' → '+observed['source_as_of']+f' · age {age//60} min',
           'Generated: '+now+' · scanned public fixed-price Rarible scope (not an atomic live market)']
    if usd is not None:
        lines.append('Saved POL/USD: '+format(usd,'f')+' · '+str(rate.get('as_of') or rate.get('date') or 'timestamp unavailable')+' (display estimate)')
    # Daily display is deliberately compact; saved observations/batches retain provenance.
    if kind=='daily': lines=[heading]
    for row in sorted(rows,key=lambda r:(Decimal(r['price']),r['contract'],int(r['token_id']),r['order_id'])):
        price=Decimal(row['price'])
        pol=format(price,'f').rstrip('0').rstrip('.') if '.' in format(price,'f') else format(price,'f')
        dollars=f'${price*usd:,.2f}' if usd is not None else 'USD unavailable'
        seller=module.wallet_label(row['wallet'],aliases or {},'Seller not recorded')
        # Alias data is local display text, never a reason to expose a full wallet.
        seller=re.sub(r'0x[0-9a-fA-F]{40}',lambda m:'...'+m.group()[-9:],seller)
        seller=' '.join(seller.split())
        name=' '.join(row['collection'].splitlines())
        rarity=' '.join(str(row.get('rarity') or 'Unknown').split())
        date=listing_date(row,now)
        date_label=date['label'] if date else 'Listing date not recorded'
        url='https://mcfarlanetoys.digital/explore/POLYGON:'+row['contract']+'/?sort=cheapest&inStockOnly=true'
        if kind=='daily' and date: date_label=date_label.removeprefix('Listed ')
        link=f'[Link](<{url}>)' if kind=='daily' else f'<{url}>'
        lines.append(f"{pol} POL ({dollars}) — [{rarity}] {name} — {seller} — Token #{row['token_id']} — {date_label} — {link}")
    if not rows: lines.append('No qualifying listings in this complete saved generation.')
    return '\n'.join(lines)


def atomic_json(path, value):
    import os
    import tempfile
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,sort_keys=True,indent=2)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if Path(temporary).exists(): Path(temporary).unlink()

def chunks(text):
    # Discord counts UTF-16 code units. Preserve every character, including
    # long catalog names; prefer whole lines without silently dropping rows.
    bodies=[]; remaining=text
    while remaining:
        used=0; end=0
        for char in remaining:
            size=2 if ord(char)>0xffff else 1
            if used+size>1900: break
            used+=size; end+=1
        if end<len(remaining):
            split=remaining.rfind('\n',0,end)
            if split>0: end=split+1
        bodies.append(remaining[:end]); remaining=remaining[end:]
    return bodies

def deliver(text, batch_id, ledger_path=LEDGER, request=None):
    """Durable direct delivery; caller must hold this sniper's own lease.

    Unknown POST outcomes fail closed for operator reconciliation rather than
    trusting Discord's finite nonce retention and risking a delayed duplicate.
    Readback failures with known message IDs retry GET only.
    """
    request=request or discord_request
    ledger_path=Path(ledger_path)
    ledger=json.loads(ledger_path.read_text(encoding='utf-8')) if ledger_path.exists() else {'batches':{}}
    batches=ledger['batches']; bodies=chunks(text)
    digest=hashlib.sha256(text.encode()).hexdigest()
    record=batches.get(batch_id)
    if record is None:
        record={'sha256':digest,'bodies':bodies,'messages':{},'complete':False,'suppress_embeds':True}
        batches[batch_id]=record; atomic_json(ledger_path,ledger)
    elif record['sha256']!=digest or record['bodies']!=bodies:
        raise ValueError('Discord batch content changed')
    channel=request('channels/'+CHANNEL_ID)
    if (channel.get('id'),channel.get('name'),channel.get('guild_id')) != (CHANNEL_ID,CHANNEL_NAME,GUILD_ID):
        raise RuntimeError('Discord channel identity mismatch')
    ids=[]
    for index,body in enumerate(bodies):
        key=str(index); message=record['messages'].get(key)
        if message is None:
            nonce=str(int(hashlib.sha256((batch_id+':'+key).encode()).hexdigest()[:15],16))
            message={'nonce':nonce,'status':'sending','suppress_embeds':True}
            record['messages'][key]=message; atomic_json(ledger_path,ledger)
            response=request('channels/'+CHANNEL_ID+'/messages',{'content':body,'allowed_mentions':{'parse':[]},'nonce':nonce,'enforce_nonce':True,'tts':False,'flags':4})
            mid=response.get('id')
            if not isinstance(mid,str) or not mid.isdigit(): raise RuntimeError('Ambiguous Discord POST identity')
            message.update(id=mid,status='posted'); atomic_json(ledger_path,ledger)
        elif not message.get('id'):
            raise RuntimeError('Ambiguous Discord POST; reconcile saved nonce before retry')
        response=request('channels/'+CHANNEL_ID+'/messages/'+message['id'])
        # Discord may remove trailing LF characters. Keep persisted bodies and
        # their digest immutable; do not normalize any other content or identity.
        if ((response.get('id'),response.get('channel_id')) != (message['id'],CHANNEL_ID)
                or response.get('content') not in (body, body.rstrip('\n'))
                or ((record.get('suppress_embeds') or message.get('suppress_embeds'))
                    and (response.get('flags',0)&4 != 4 or response.get('embeds') != []))):
            raise RuntimeError('Discord read-back mismatch')
        message['status']='verified'; atomic_json(ledger_path,ledger); ids.append(message['id'])
    record['complete']=True; atomic_json(ledger_path,ledger)
    return ids

def discord_request(path, payload=None):
    """Discord-only transport. Never called by import, preview or baseline."""
    import os
    import urllib.request
    token=os.environ.get('DISCORD_BOT_TOKEN')
    if not token:
        for line in (HOME/'.env').read_text(encoding='utf-8').splitlines():
            if line.startswith('DISCORD_BOT_TOKEN='):
                token=line.split('=',1)[1].strip().strip('"').strip("'"); break
    if not token: raise RuntimeError('DISCORD_BOT_TOKEN unavailable')
    req=urllib.request.Request('https://discord.com/api/v10/'+path,
            headers={'Authorization':'Bot '+token,'User-Agent':'HermesMTDUnder100/1.0','Content-Type':'application/json'},
            data=json.dumps(payload).encode() if payload is not None else None)
    with urllib.request.urlopen(req,timeout=40) as response: return json.load(response)


class Busy(RuntimeError):
    pass

from contextlib import contextmanager

@contextmanager
def lease(path=LOCK):
    """OS-owned file lock, automatically released on death; never unlink it.

    This is NOT the shared producer/provider lock. No stale-age reclamation,
    directory race or inherited detached child is involved.
    """
    import os
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with open(path,'a+b') as stream:
        if stream.tell()==0: stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            try: msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            except OSError as exc: raise Busy('Sniper lease busy') from exc
        else:
            import fcntl
            try: fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as exc: raise Busy('Sniper lease busy') from exc
        try: yield
        finally:
            if os.name=='nt':
                stream.seek(0); msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(stream,fcntl.LOCK_UN)

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def optional_json(path):
    try: return read_json(path)
    except (OSError,ValueError): return {}

def saved_rate(path):
    data=optional_json(path)
    current=data.get('current_pol_usd') or {}
    value=decimal(current.get('rate'))
    if value is not None and value>0:
        return {'rate':current['rate'],'as_of':current.get('retrieved_at'),'source':current.get('source')}
    # Missing saved spot is unavailable, never an unrelated historic rate.
    return {}

def morning_ready(observed, day, now):
    from zoneinfo import ZoneInfo
    eastern=ZoneInfo('America/New_York')
    threshold=datetime.fromisoformat(day+'T09:35:00').replace(tzinfo=eastern).timestamp()
    return stamp(now)>=threshold and stamp(observed['source_started_at'])>=threshold

def run(mode, *, source_path=SOURCE, catalog_path=CATALOG, state_path=STATE,
        lock_path=LOCK, ledger_path=LEDGER, aliases_path=ROOT/'mcfarlane-wallet-aliases.json',
        rate_path=ROOT/'mcfarlane-exotics.json', now=None, day=None,
        send=False, manual=False, revision=None, request=None):
    """One bounded saved-only pass. Scheduler delivery must be suppressed.

    Reinvoke daily every five minutes: the consumer waits for a completed
    generation begun after 09:35 Eastern that day, not a fragile fixed offset.
    Manual is only for the explicitly authorized initial daily-format heartbeat.
    """
    from mtd_listing_wallets import approved_scope
    from zoneinfo import ZoneInfo
    if mode not in ('preview','baseline','interval','daily'): raise ValueError('Unknown mode')
    if revision is not None and (mode!='daily' or not manual or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}',revision)):
        raise ValueError('Revision requires explicit manual daily heartbeat and a safe revision label')
    now=now or datetime.now(timezone.utc).isoformat()
    day=day or datetime.fromtimestamp(stamp(now),ZoneInfo('America/New_York')).date().isoformat()
    if datetime.fromisoformat(day).date().isoformat()!=day: raise ValueError('Invalid heartbeat day')
    state_path=Path(state_path)
    revision_key='daily:'+day+(':'+revision if revision else '')
    def daily_done(state):
        return revision_key in state.get('heartbeat_revisions',{}) if revision else day in state.get('heartbeat_days',{})
    def load():
        scope=approved_scope(catalog_path)
        source=read_json(source_path)
        observed=inventory(source,scope,now)
        return scope,source,observed
    try:
        scope,source,observed=load()
    except (OSError,ValueError) as exc:
        return {'status':'waiting_source','reason':str(exc)}
    if mode=='preview':
        return {'status':'preview','count':len(observed['rows']),
                'text':render(observed,day,now,aliases=optional_json(aliases_path),rate=saved_rate(rate_path))}
    if mode=='daily' and not manual and not morning_ready(observed,day,now):
        return {'status':'waiting_fresh_daily_generation'}
    # Frequent unchanged no-op ticks should not create even our private lease.
    previous=read_json(state_path) if state_path.exists() else {}
    if (mode=='interval' and previous and previous.get('scope')==scope
            and previous['observed']==observed
            and not previous.get('alerts') and not any(not b.get('delivered') and not b.get('retired') for b in previous.get('batches',{}).values())):
        return {'status':'unchanged','count':len(observed['rows'])}
    if mode=='daily' and daily_done(previous): return {'status':'already_delivered'}
    try:
        with lease(lock_path):
            scope,source,observed=load()  # Revalidate source and reread state under our own lock.
            previous=read_json(state_path) if state_path.exists() else {}
            if mode=='daily' and daily_done(previous): return {'status':'already_delivered'}
            if mode=='daily' and not manual and not morning_ready(observed,day,now):
                return {'status':'waiting_fresh_daily_generation'}
            state=advance(previous,source,scope,now)
            if mode=='baseline':
                # Baseline is first-run only. Never silently clear existing queues.
                atomic_json(state_path,state)
                return {'status':'baseline' if not previous else 'observed','count':len(observed['rows'])}
            aliases=optional_json(aliases_path); rate=saved_rate(rate_path)
            batch_id=None
            current={r['contract']:r for r in observed['rows']}
            def identities(rows):
                return sorted((r['contract'],r['order_id'],r['price'],r['collection'],r['wallet'],r.get('rarity'),r.get('created_at'),r.get('started_at')) for r in rows)
            # Never mutate an attempted message body. Unattempted superseded
            # batches can be retired and rebuilt from current complete data.
            delivery_records=optional_json(ledger_path).get('batches',{})
            for key,batch in state['batches'].items():
                if batch.get('delivered') or batch.get('retired'): continue
                target=observed['rows'] if batch['kind']=='daily' else [current[r['contract']] for r in batch['rows'] if r['contract'] in current]
                if identities(batch['rows']) != identities(target):
                    if delivery_records.get(key,{}).get('messages'):
                        atomic_json(state_path,state)
                        return {'status':'pending_batch_inventory_changed','batch_id':key}
                    batch['retired']=True
            if mode=='daily':
                pending=[key for key,b in state['batches'].items() if b['kind']=='daily' and b['day']==day and b.get('revision')==revision and not b.get('delivered') and not b.get('retired')]
                batch_id=pending[0] if pending else revision_key
                if batch_id in state['batches'] and state['batches'][batch_id].get('retired'):
                    batch_id+=':'+hashlib.sha256(json.dumps(identities(observed['rows'])).encode()).hexdigest()[:16]
                if batch_id not in state['batches']:
                    state['batches'][batch_id]={'kind':'daily','day':day,'revision':revision,'events':[],
                        'text':render(observed,day,now,aliases=aliases,rate=rate),'rows':deepcopy(observed['rows'])}
            else:
                # Current saved inventory is authoritative at delivery time.
                current={r['contract']:r for r in observed['rows']}
                # An invalid current listing suspends delivery, not its debt.
                # Only a confirmed exit (or explicit scope removal) cancels it.
                state['alerts']={event:row for event,row in state['alerts'].items() if row['contract'] in state['confirmed']}
                valid={event:current[row['contract']] for event,row in state['alerts'].items() if row['contract'] in current}
                pending=[key for key,b in state['batches'].items() if b['kind']=='interval' and not b.get('delivered') and not b.get('retired')]
                if pending: batch_id=pending[0]
                elif valid:
                    batch_id='new:'+hashlib.sha256(('|'.join(sorted(valid))+json.dumps(identities(valid.values()))).encode()).hexdigest()[:24]
                    alert_observed=dict(observed,rows=list({r['contract']:r for r in valid.values()}.values()))
                    state['batches'][batch_id]={'kind':'interval','events':sorted(valid),
                        'text':render(alert_observed,day,now,aliases=aliases,rate=rate,kind='interval'),'rows':deepcopy(alert_observed['rows'])}
            atomic_json(state_path,state)
            if batch_id is None: return {'status':'observed','count':len(observed['rows'])}
            if not send: return {'status':'queued','batch_id':batch_id}
            batch=state['batches'][batch_id]
            current={r['contract']:r for r in observed['rows']}
            def identity(row): return (row['order_id'],row['price'])
            if any(row['contract'] not in current or identity(row)!=identity(current[row['contract']]) for row in batch['rows']):
                return {'status':'pending_batch_inventory_changed','batch_id':batch_id}
            ids=deliver(batch['text'],batch_id,ledger_path,request)
            batch.update(delivered=True,message_ids=ids)
            for event in batch['events']: state['alerts'].pop(event,None)
            if batch['kind']=='daily':
                if batch.get('revision'): state.setdefault('heartbeat_revisions',{})[revision_key]=batch_id
                else: state['heartbeat_days'][day]=batch_id
            atomic_json(state_path,state)
            return {'status':'delivered','batch_id':batch_id,'message_ids':ids,'count':len(batch['rows'])}
    except Busy:
        return {'status':'busy'}
    except (OSError,ValueError) as exc:
        return {'status':'waiting_source','reason':str(exc)}

