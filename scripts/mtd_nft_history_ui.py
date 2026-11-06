"""Saved queue/timeline presentation and local-only intake. Never calls a provider."""
import html
import json
import importlib.util
import re
from pathlib import Path
from datetime import datetime, timezone
from mtd_nft_history_provider import asset_key, identities

_alias_spec=importlib.util.spec_from_file_location('mtd_history_shared_aliases',Path(__file__).resolve().parents[1]/'price-watches'/'mcfarlane_wallet_aliases.py')
_shared_aliases=importlib.util.module_from_spec(_alias_spec)
_alias_spec.loader.exec_module(_shared_aliases)


def lifecycle_parties(raw, aliases):
    """Label only recorded event roles; no ownership or execution inference."""
    def label(value):
        if not isinstance(value,str) or not re.fullmatch(r'(?:(?:ETHEREUM|POLYGON):)?0x[0-9a-fA-F]{40}',value):
            return 'Not recorded'
        return _shared_aliases.alias_for_wallet(value,aliases) or '0x...'+value[-4:].lower()
    kind=raw.get('@type')
    if kind=='MINT': return '—',label(raw.get('owner'))
    if kind=='TRANSFER': return label(raw.get('from')),label(raw.get('owner'))
    if kind=='SELL': return label(raw.get('seller')),label(raw.get('buyer'))
    if kind=='BURN': return label(raw.get('owner')),'Burned'
    if kind in ('LIST','CANCEL_LIST','BID','CANCEL_BID'):
        value=raw.get('taker')
        taker=label(value) if not isinstance(value,str) or value.split(':')[-1].lower()!='0x'+'0'*40 else 'Not recorded'
        return label(raw.get('maker'))+' (maker)',taker+' (taker)' if taker!='Not recorded' else '—'
    return '—','—'


def lifecycle_date(value):
    """Display recorded zoned ISO dates in UTC; never assume local time."""
    if value is None or value=='': return '—'
    if not isinstance(value,str): return str(value)
    try:
        dt=datetime.fromisoformat(value.replace('Z','+00:00'))
        if dt.tzinfo is None: return value
        fraction=re.search(r'[T ]\d{2}:\d{2}:\d{2}([.,]\d+)',value)
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')+(fraction.group(1).replace(',','.') if fraction else '')+' UTC'
    except ValueError:
        return value


def wallet_label(wallet,aliases):
    names=[str(r.get('alias')) for r in aliases.get('aliases',[]) if str(r.get('wallet','')).lower()==wallet.lower() and r.get('alias')]
    names.extend(str(name) for key,name in aliases.get('exact_aliases',{}).items() if key.lower()==wallet.lower() and name)
    return (names[0]+' — ' if names else '')+'…'+wallet[-9:]


def exact_item(fields, allowlist):
    """NFT-only controls deliberately ignore any leftover wallet field."""
    item=fields.get('item','')
    contract=fields.get('contract','')
    token=fields.get('token_id','')
    if item and (contract or token): raise ValueError('Choose item string OR contract and literal token ID')
    if not item:
        if not contract or not token: raise ValueError('For wallet discovery use Queue Wallet’s NFTs; this button requires an exact item string or contract and literal token ID')
        item=(contract if contract.startswith('POLYGON:') else 'POLYGON:'+contract)+':'+token
    selected=asset_key(item)
    if selected.rsplit(':',1)[0].lower() not in {value.lower() for value in allowlist}:
        raise ValueError('Asset outside current allowlist')
    return selected


def action(store, fields, catalog, aliases):
    """Called under the analyzer archive lease; all mutations are local SQLite only."""
    verb=fields.get('action','saved')
    wallet_intake=verb=='enqueue_wallet'
    if wallet_intake:
        wallet=fields.get('wallet','').strip()
        if not wallet: raise ValueError('Enter a wallet or exact alias to Queue Wallet’s NFTs')
        # Wallet intake must not inherit a stale NFT or another discovery selection.
        fields={'action':'enqueue','wallet':wallet}
        verb='enqueue'
    selected=fields.get('asset','')
    job=None
    message=''
    allowlist=[r['id'] for r in catalog.values()]
    if selected: selected=exact_item({'item':selected},allowlist)
    if verb=='enqueue':
        wallet=fields.get('wallet','').strip()
        item=fields.get('item','')
        contract=fields.get('contract','')
        token=fields.get('token_id','')
        if bool(wallet)+bool(item or contract or token)!=1: raise ValueError('Choose wallet/alias OR exact item/contract+token, not both')
        if wallet:
            job=store.enqueue_discovery(wallet,allowlist,aliases=aliases)
            original=store.job(job)
            template=dict(original['request']); template.pop('continuation',None)
            # Reuse an active compatible observation, not the first completed scope forever.
            with store.connect() as db:
                ids=[r[0] for r in db.execute("SELECT id FROM nh_jobs WHERE kind='discovery' AND subject=? ORDER BY id DESC",(original['subject'],))]
            for identity in ids:
                candidate=store.job(identity)
                request=dict(candidate['request']); request.pop('continuation',None)
                if request==template:
                    job=identity
                    break
            if store.job(job)['state']=='complete': job=store.reconcile_discovery(job)
            if wallet_intake:
                message='Wallet queued; waiting for scheduled worker · Discovery run '+str(job)+'. Queue and job pause settings are unchanged.'
        else:
            if item and (contract or token): raise ValueError('Choose item string OR contract and literal token ID')
            if not item:
                if not contract or not token: raise ValueError('Contract and literal token ID required')
                item=(contract if contract.startswith('POLYGON:') else 'POLYGON:'+contract)+':'+token
            selected=asset_key(item)
            job=store.enqueue_item(selected,allowlist=allowlist)
    elif verb in ('saved_item','refresh_item'):
        selected=exact_item(fields,allowlist)
        if verb=='refresh_item':
            with store.connect() as db:
                prior=db.execute("SELECT id,state FROM nh_jobs WHERE kind='item' AND subject=? ORDER BY id DESC LIMIT 1",(selected,)).fetchone()
            job=store.refresh_item(selected) if prior and prior['state']=='complete' else store.enqueue_item(selected,allowlist=allowlist,refresh_after=0)
            current=store.job(job)
            reused=bool(prior and job<=prior['id'])
            message=('Reused existing NFT job' if reused else 'Recent-overlap NFT refresh queued' if current['mode']=='incremental' else 'Initial NFT history queued')+' · Job '+str(job)+' · '+current['state']+'.'
            if current['state']=='failed':
                message+=' Use Explicit Full NFT Rescan for an invalid cursor; otherwise Retry Failed Checkpoint.'
            elif current['state'] in ('paused','cancelled'):
                message+=' No automatic resume; use the explicit job controls.'
    elif verb in ('pause_all','resume_all'):
        store.queue_control('pause' if verb=='pause_all' else 'resume')
    elif verb in ('pause_job','resume_job','cancel_job','retry_failed','full_rescan','reconcile_holdings'):
        job=int(fields.get('job','0'))
        prior=store.job(job)
        if verb=='retry_failed':
            if prior['state']!='failed': raise ValueError('Select a failed job')
            store.control(job,'resume')  # Invalid cursor deliberately refused here.
        elif verb=='full_rescan':
            if prior['kind']!='item': raise ValueError('Select an NFT item job for full rescan')
            if ':'.join(prior['subject'].split(':')[:2]).lower() not in catalog: raise ValueError('Asset outside current allowlist')
            # Never restart another active good scan implicitly. Pause/cancel it explicitly first.
            if prior['state'] in ('queued','running','deferred','paused'): raise ValueError('Cancel the active item job explicitly before full rescan')
            selected=prior['subject']
            job=store.refresh_item(selected,full_rescan=True)
        elif verb=='reconcile_holdings':
            job=store.reconcile_discovery(job)
        else:
            if prior['state']=='complete': raise ValueError('Completed scans are reused; enqueue again for a due recent refresh or choose explicit full rescan')
            store.control(job,{'pause_job':'pause','resume_job':'resume','cancel_job':'cancel'}[verb])
    elif verb!='saved': raise ValueError('Unknown NFT queue action')
    status=store.queue_status(include_assets=False)
    status['job_id']=job
    status['job_state']=store.job(job)['state'] if job is not None else None
    status['message']=message
    status['selected_asset']=selected
    discoveries=[j for j in status['jobs'] if j['kind']=='discovery']
    status['discoveries']=[{'job_id':j['id'],'label':wallet_label(j['subject'],aliases)+' · run '+str(j['id'])} for j in discoveries]
    status['wallet_labels']={j['subject']:wallet_label(j['subject'],aliases) for j in discoveries}
    selection=fields.get('discovery_job','')
    if job is not None and store.job(job)['kind']=='discovery': selection=str(job)
    current=next((j for j in discoveries if str(j['id'])==selection),None) if selection else (discoveries[-1] if discoveries else None)
    if selection and current is None: raise ValueError('Unknown discovery job')
    status['selected_discovery_job']=current['id'] if current else None
    status['discovery']=None
    if current:
        holdings=store.holdings(current['id'])
        assets=set()
        for raw in holdings['current']:
            if str(raw.get('owner','')).lower()!=('ETHEREUM:'+current['subject']).lower(): continue
            try: keys=identities(raw)
            except ValueError: continue  # Raw failed/review observations stay available; never count unresolved identity.
            assets.update(a for a in keys if a.rsplit(':',1)[0] in current['request']['filter']['collections'])
        assets=sorted(assets)
        status['discovery']={'job_id':current['id'],'wallet_label':wallet_label(current['subject'],aliases),
            'state':('finished' if holdings['query_exhausted'] else 'failed' if current['state']=='failed' else 'scanning' if current['pages'] or current['state']=='running' else 'waiting'),
            'found':len(assets),'query_exhausted':holdings['query_exhausted'],
            'histories':[]}
        discovery=status['discovery']
        counts=dict.fromkeys(('waiting','in_progress','finished','deferred','failed','paused','cancelled'),0)
        for asset in assets:
            jobs=[j for j in status['jobs'] if j['kind']=='item' and j['subject']==asset]
            latest=jobs[-1] if jobs else None
            state=latest['state'] if latest else 'queued'
            progress=('finished' if state=='complete' else 'in_progress' if state=='running' or (state=='queued' and latest and latest['pages']) else 'waiting' if state=='queued' else state)
            counts[progress]+=1
            discovery['histories'].append({'asset':asset,'job_id':latest['id'] if latest else None,'state':progress,
                'mode':latest['mode'] if latest else 'initial',
                'reused_completed':bool(latest and state=='complete' and latest['coverage'].get('last_observed') and datetime.fromisoformat(latest['coverage']['last_observed'].replace('Z','+00:00')).timestamp()<=current['created']),
                'refresh_pending':bool(latest and latest['mode']=='incremental' and state!='complete'),
                'pages':latest['pages'] if latest else 0,'records_saved':len(store.timeline(asset)),
                'last_progress':max((j['coverage']['last_observed'] for j in jobs if j['coverage'].get('last_observed')),default=None),
                'next_retry':max(float(status['provider_not_before'] or 0),latest['next_attempt_at']) if latest and state in ('queued','running','deferred') else None})
        discovery['history_counts']=counts
        discovery['reused_completed']=sum(r['reused_completed'] for r in discovery['histories'])
        discovery['refresh_pending']=sum(r['refresh_pending'] for r in discovery['histories'])
        discovery['pages']=current['pages']
        discovery['last_progress']=current['coverage'].get('last_observed')
        discovery['next_retry']=max(float(status['provider_not_before'] or 0),current['next_attempt_at']) if current['state'] in ('queued','running','deferred') else None
    status['result_html']=render(store,status,catalog,selected,aliases)
    return status


def render(store,status,catalog,selected='',aliases=None):
    e=lambda value:html.escape(str(value if value is not None else '—'),quote=True)
    def when(value):
        if not value: return '—'
        dt=datetime.fromtimestamp(value,timezone.utc) if isinstance(value,(int,float)) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    out=[]
    if status.get('message'): out.append('<p role="status">'+e(status['message'])+'</p>')
    if status['paused']:
        out.append('<aside role="note" style="border:2px solid #db9c36;padding:12px"><strong>Queue PAUSED</strong> — No new discovery or NFT history calls will start. Queue Wallet / NFT adds work; does not resume paused queue. <button type="submit" form="nft-form" name="action" value="resume_all">Resume Queue</button><p>Resume Queue leaves manual job pauses and authentication blocks unchanged.</p></aside>')
    discovery=status.get('discovery')
    if discovery:
        out.append('<section aria-label="Wallet discovery"><h3>Wallet '+e(discovery['wallet_label'])+' · Discovery '+e(discovery['state'])+'</h3>')
        label='NFTs found: ' if discovery['query_exhausted'] else 'NFTs found so far: '
        out.append('<p><strong>'+label+e(discovery['found'])+'</strong> · '+('Ownership query scan finished; not full lifetime completeness.' if discovery['query_exhausted'] else 'Ownership query not finished — count is not final.')+'</p>')
        out.append('<p>Discovery run '+e(discovery['job_id'])+' · Pages: '+e(discovery['pages'])+' · Last actual progress: '+e(when(discovery['last_progress']))+' · Next retry: '+e(when(discovery['next_retry']))+'</p>')
        out.append('<h4>Histories for these '+e(discovery['found'])+' NFTs'+(' found so far' if not discovery['query_exhausted'] else '')+'</h4><p>'+ ' · '.join(e(k.replace('_',' '))+': '+e(v) for k,v in discovery['history_counts'].items())+'</p>')
        out.append('<p>Reused completed queries: '+e(discovery['reused_completed'])+' · Incremental refresh pending: '+e(discovery['refresh_pending'])+'. Finished means query scan finished, not lifetime complete. Counts are distinct NFTs, not jobs; saved histories are shared across wallets. Pages refer to the current query; records are distinct saved activities. Retry times are earliest eligibility (UTC), subject to pause/auth blocks.</p>')
        running=next((r['asset'] for r in discovery['histories'] if any(j['id']==r['job_id'] and j['state']=='running' for j in status['jobs'])),None)
        out.append('<p>Current NFT: '+e(running or 'none running for this discovery')+'</p>')
        out.append('<div style="overflow-x:auto"><table><thead><tr>'+''.join('<th>'+h+'</th>' for h in ('NFT / true token ID','History progress','Pages','Records saved','Last actual progress','Next retry'))+'</tr></thead><tbody>')
        for row in discovery['histories']:
            collection,token=row['asset'].rsplit(':',1)
            name=catalog.get(collection.lower(),{}).get('display_name') or collection
            progress=row['state'].replace('_',' ')
            if row['state']=='finished': progress+=' — query scan finished'
            if row['reused_completed']: progress+=' · reused completed query'
            if row['refresh_pending']: progress+=' · incremental refresh pending (saved query retained)'
            cells=[name+' · #'+token,progress,row['pages'],row['records_saved'],when(row['last_progress']),when(row['next_retry'])]
            out.append('<tr>'+''.join('<td>'+e(c)+'</td>' for c in cells)+'</tr>')
        out.append('</tbody></table></div></section>')
    out.append('<p><strong>Queue '+('PAUSED' if status['paused'] else 'enabled')+'</strong> · Auth blocked: '+e(status['auth_blocked'])+' · Current job: '+e(status['current_job'])+' · Saved-only request: network calls 0, notifications 0, model tokens 0.</p>')
    out.append('<details><summary>All queue jobs (including other wallets)</summary><p>'+e(json.dumps(status['states'],sort_keys=True))+'</p>')
    out.append('<p>Provider gate (epoch): '+e(status['provider_not_before'])+'. Manual per-job pauses remain unchanged by Resume All. Authentication needs explicit job resume after credentials are remediated.</p>')
    out.append('<div style="overflow-x:auto"><table><thead><tr>'+''.join('<th>'+h+'</th>' for h in ('Job','Kind / mode','Wallet / exact asset','State','Calls / pages / rows','Checkpoint','Next retry (epoch)','Error / coverage'))+'</tr></thead><tbody>')
    for j in status['jobs']:
        state='initial_scan_exhausted' if j['state']=='complete' and j['kind']=='item' and j['mode']=='initial' else j['state']
        cursor=j['request'].get('cursor') or j['request'].get('continuation')
        detail='<details><summary>'+e(j['error'] or 'Saved coverage')+'</summary><pre>'+e(json.dumps(j['coverage'],indent=2))+'</pre></details>'
        subject=status['wallet_labels'].get(j['subject'],j['subject'])
        cells=[e(j['id']),e(j['kind']+' / '+j['mode']),e(subject),e(state),e(f"{j['calls']} / {j['pages']} / {j['rows']}"),e(cursor),e(j['next_attempt_at']),detail]
        out.append('<tr>'+''.join('<td>'+v+'</td>' for v in cells)+'</tr>')
    out.append('</tbody></table></div></details>')
    out.append('<details><summary>Shared archive · '+e(status['asset_count'])+' saved assets (all wallets; not this discovery count)</summary><p>Use the exact NFT fields to view one saved history. Archive records are retained.</p></details>')
    if not selected:
        out.append('<p>Coverage is query exhaustion only, not lifetime completeness. Current holdings reflect the selected ownership observation.</p>')
    if selected:
        out.append('<style>body{max-width:none;width:100%;box-sizing:border-box}.analysis-report{max-width:none;width:100%}.nft-lifecycle-scroll{width:100%;overflow-x:auto}.nft-lifecycle{width:100%;border-collapse:collapse}.nft-lifecycle th,.nft-lifecycle td{padding:6px 11px;border:1px solid #31415f;text-align:left;vertical-align:top;white-space:nowrap}.nft-lifecycle th{background:#13233b}.nft-lifecycle th:last-child,.nft-lifecycle td:last-child{text-align:right;font-variant-numeric:tabular-nums}.nft-lifecycle td[colspan]{text-align:left;white-space:normal}</style>')
        selected=asset_key(selected)
        chain,contract,token=selected.split(':')
        collection=catalog.get((chain+':'+contract).lower(),{}).get('display_name') or chain+':'+contract
        timeline=store.lifecycle(selected)
        name=next((name for row in timeline for name in ((row['raw'].get('meta') or {}).get('name'),(row['raw'].get('nft') or {}).get('name')) if isinstance(name,str) and name.strip()),collection)
        out.append('<h3>Saved lifecycle · '+e(name)+' · True token ID: '+e(token)+'</h3>')
        jobs=[j for j in status['jobs'] if j['kind']=='item' and j['subject']==selected]
        latest=jobs[-1] if jobs else None
        observations=[o for row in timeline for o in row['observations']]
        retrieved=max([o['retrieved_at'] for o in observations if o.get('retrieved_at')]+[j['coverage']['last_observed'] for j in jobs if j['coverage'].get('last_observed')],default=None)
        sources=sorted({row['provider']+' / '+str(o.get('source') or 'unknown source') for row in timeline for o in row['observations']})
        out.append('<p>Source / coverage: '+e(', '.join(sources) or 'No saved activity source')+' · Last retrieval: '+e(when(retrieved))+' · Saved records: '+e(len(timeline))+'. Coverage means query exhaustion only, not lifetime completeness.</p>')
        if latest:
            out.append('<p>Current query: Job '+e(latest['id'])+' · '+e(latest['mode'])+' · '+e(latest['state'])+' · Query exhausted: '+e(latest['coverage'].get('query_exhausted',False))+'.</p>')
        else:
            out.append('<p>Current query: none saved; saved history may be partial.</p>')
        out.append('<div class="nft-lifecycle-scroll"><table class="nft-lifecycle"><thead><tr><th>Date (UTC)</th><th>Type / execution</th><th>From</th><th>To</th><th>Price</th></tr></thead><tbody>')
        if not timeline:
            out.append('<tr><td colspan="5">No saved history for this exact NFT. <button type="submit" form="nft-form" name="action" value="refresh_item">Gather / Refresh NFT History</button></td></tr>')
        for row in timeline:
            raw=row['raw']
            sender,recipient=lifecycle_parties(raw,aliases)
            cells=[e(lifecycle_date(raw.get('date'))),e(str(row['kind'])+' / '+str(row['execution_type'] or '—')),e(sender),e(recipient),e(raw.get('price'))]
            out.append('<tr class="nft-event-row">'+''.join('<td>'+v+'</td>' for v in cells)+'</tr>')
        out.append('</tbody></table></div>')
    return ''.join(out)


def panel(csrf):
    return '''<section id="nft-queue"><h2>NFT History Queue</h2>
<style>#nft-form{width:100%;max-width:none;box-sizing:border-box}#nft-form fieldset{flex:1 1 100%;min-width:0;margin:8px 0;padding:14px;border:1px solid #475569;border-radius:7px;display:flex;gap:12px;flex-wrap:wrap;align-items:end}#nft-form legend{font-weight:700;padding:0 6px}#nft-form fieldset p{flex-basis:100%;margin:0}#nft-feedback{position:sticky;top:0;z-index:2;background:#13233b;white-space:normal;padding:10px;border-left:3px solid #60a5fa}#nft-feedback:empty{display:none}#nft-feedback[data-error="true"]{border-color:#f87171}</style>
<p id="nft-feedback" role="status" aria-live="assertive" tabindex="-1"></p>
<p>Start once: enqueue CURRENT in-scope holdings by wallet/exact alias, or one exact NFT. Intake and all saved views make no marketplace requests. Starting a completed wallet intake queues a new current-ownership observation; active compatible intake is reused, and item histories remain globally shared. An independently installed no-agent scheduler drains one API call per tick; closing this tab does not cancel saved work. Scheduler deployment is separate; no continuous worker is launched by these buttons.</p>
<p>First saved access may initialize the additive shared archive and import legacy evidence locally; compact JSON and original legacy tables are preserved. Not a live marketplace refresh.</p>
<form id="nft-form" method="post" action="/nft-action" style="flex-wrap:wrap"><input type="hidden" name="csrf" value="'''+html.escape(csrf,quote=True)+'''">
<fieldset><legend>Wallet discovery</legend>
<label>Find current NFTs in wallet / exact alias<input name="wallet" placeholder="Wallet address or exact alias"></label>
<button name="action" value="enqueue_wallet" title="Wallet-only replacement for Queue Wallet / NFT">Queue Wallet’s NFTs</button>
<p>Find current in-scope holdings, then queue their NFT histories for the scheduled worker. Ignores individual NFT fields below. Adds work only; never resumes a paused queue.</p></fieldset>
<fieldset><legend>Individual NFT</legend>
<label>Exact POLYGON item string<input name="item" placeholder="POLYGON:0x…:26"></label>
<label>OR allowlisted contract<input name="contract" placeholder="POLYGON:0x… or 0x…"></label>
<label>Literal on-chain token ID<input name="token_id" placeholder="26, not display #27"></label>
<button name="action" value="saved_item" class="secondary">View Saved NFT History</button><button name="action" value="refresh_item" title="Gather / Refresh NFT History for this exact NFT only">Gather / Refresh This NFT</button>
<p>These buttons use only the exact NFT fields, not the wallet above. Saved view makes no requests; gather queues initial or recent-overlap work for the scheduled worker and never resumes a paused queue.</p></fieldset>
<button name="action" value="pause_all" class="secondary">Pause All Queue</button><button name="action" value="resume_all" class="secondary">Resume All Queue</button>
<label>Exact job ID<input name="job" type="number" min="1"></label>
<button name="action" value="pause_job" class="secondary">Pause Job</button><button name="action" value="resume_job" class="secondary">Resume Job / Auth Remediated</button><button name="action" value="cancel_job" class="secondary">Cancel Job</button><button name="action" value="retry_failed" class="secondary">Retry Failed Checkpoint</button><button name="action" value="full_rescan" class="secondary">Explicit Full NFT Rescan</button><button name="action" value="reconcile_holdings" class="secondary">New Current Holdings Observation</button>
<label>Wallet discovery run<select name="discovery_job"><option value="">Latest discovery</option></select></label>
<input type="hidden" name="asset" value=""><p>Selected NFT: <span id="nft-selected">none</span></p><button name="action" value="saved" class="secondary">Load Saved Queue / Timeline</button></form>
<p>Enqueue while paused does not unpause. Full rescan applies only to the selected item job, never every good scan. Invalid/expired cursors require explicit full rescan, not retry or ordinary resume. New Current Holdings Observation reruns only the selected completed/failed/cancelled discovery. Listing revisions are retained, not proof of all historic reprices.</p>
<div id="nft-result" role="status" aria-live="polite">Loading saved queue only…</div></section>
<script>(()=>{
const f=document.getElementById('nft-form'),result=document.getElementById('nft-result'),feedback=document.getElementById('nft-feedback');let busy=false;
function notice(text,error=false){if(!feedback?.dataset)return;feedback.textContent=text;feedback.dataset.error=String(error);if(error){feedback.focus({preventScroll:true});feedback.scrollIntoView({block:'nearest'})}}
async function decode(response){const text=await response.text();let data;try{data=JSON.parse(text)}catch(_){throw new Error('NFT endpoint HTTP '+response.status+' did not return JSON')}if(!response.ok)throw new Error(data.error||'NFT request failed');return data}
function show(data){result.innerHTML=data.result_html;const selected=f.querySelector('[name="asset"]');selected.value=data.selected_asset||'';document.getElementById('nft-selected').textContent=selected.value||'none';const discovery=f.querySelector('[name="discovery_job"]');discovery.replaceChildren(new Option('Latest discovery',''));for(const run of data.discoveries)discovery.add(new Option(run.label,String(run.job_id)));discovery.value=data.selected_discovery_job?String(data.selected_discovery_job):''}
async function refresh(){if(busy)return;busy=true;try{const asset=f.querySelector('[name="asset"]').value,discovery=f.querySelector('[name="discovery_job"]').value;show(await decode(await fetch('/nft-status?asset='+encodeURIComponent(asset)+'&discovery_job='+encodeURIComponent(discovery),{cache:'no-store'})))}catch(error){notice(error.message,true)}finally{busy=false}}
f.querySelector('[name="discovery_job"]').addEventListener('change',refresh);
f.addEventListener('submit',async event=>{event.preventDefault();const verb=event.submitter?.value||'saved';const value=name=>f.querySelector('[name="'+name+'"]').value;
if((verb==='saved_item'||verb==='refresh_item')&&!value('item')&&(!value('contract')||!value('token_id'))){notice('For wallet discovery use Queue Wallet’s NFTs; this button requires an exact item string or contract and literal token ID.',true);return}
if(verb==='enqueue_wallet'&&!value('wallet').trim()){notice('Enter a wallet or exact alias to Queue Wallet’s NFTs.',true);return}
if(busy){notice('A queue request is in progress; try again in a moment.',true);return}busy=true;notice(verb==='enqueue_wallet'?'Queueing wallet’s current NFTs…':verb==='refresh_item'?'Queueing this NFT’s history…':'Loading saved queue action…');const csrf=value('csrf');const body=new URLSearchParams({csrf:csrf,action:verb,wallet:value('wallet'),item:value('item'),contract:value('contract'),token_id:value('token_id'),job:value('job'),asset:value('asset'),discovery_job:value('discovery_job')});try{const data=await decode(await fetch('/nft-action',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','X-MTD-ANALYZER-CSRF':csrf},body:body}));show(data);notice(data.message||'Saved queue action complete. No marketplace requests were made.')}catch(error){notice(error.message,true)}finally{busy=false}});
refresh();setInterval(refresh,5000);
})();</script>'''
