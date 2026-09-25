"""Explicit source-only snapshots. No network in snapshot/verification modes."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

HOME = Path(__file__).resolve().parents[1]
DOCS = Path(os.environ.get('MCFARLANE_DOCS', str(Path.home() / 'Documents')))
MANIFEST = Path(__file__).with_name('monitor_backup_manifest.json')

def manifest():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))

def source_paths(kind, home=HOME):
    spec = manifest()
    grouped = [home / group / name for group in ('scripts', 'price-watches')
               for name in spec[kind].get(group, [])]
    rooted = [home / name for name in spec[kind].get('root_paths', [])]
    return grouped + rooted + [home / 'scripts' / name for name in spec['shared_scripts']]

def schedules(kind, jobs=None):
    if jobs is None:
        raw=json.loads((HOME/'cron/jobs.json').read_text(encoding='utf-8'))
        jobs=raw['jobs'] if isinstance(raw,dict) else raw
    allowed=set(manifest()[kind]['scripts'])
    # Only recurring configuration; exclude execution telemetry, IDs, destinations,
    # prompts, provider settings, and one-off preview jobs.
    return sorted([{'script':j['script'], 'schedule':{k:j['schedule'][k] for k in ('kind','expr') if k in j['schedule']},
                    'configured_enabled':bool(j.get('enabled')), 'enabled':False,
                    'no_agent':bool(j.get('no_agent')), 'timezone':'America/New_York',
                    'delivery':'CONFIGURE_PRIVATELY'}
                   for j in jobs if j.get('script') in allowed and j.get('schedule',{}).get('kind')=='cron'],key=lambda j:j['script'])

def fingerprint(kind, jobs=None):
    h=hashlib.sha256()
    for p in sorted(source_paths(kind)):
        h.update(p.relative_to(HOME).as_posix().encode()); h.update(p.read_bytes())
    h.update(json.dumps(schedules(kind,jobs),sort_keys=True,separators=(',',':')).encode())
    # Private collection identity and policy changes matter, observation churn does not.
    state=json.loads((HOME/'price-watches'/('mcfarlane-exotics.json' if kind=='exotic' else 'mcfarlane-dc-sales.json')).read_text(encoding='utf-8'))
    scope=[{k:c[k] for k in ('id','name','contract','source_url','category') if k in c} for c in state['collections']]
    h.update(json.dumps(sorted(scope,key=lambda c:json.dumps(c,sort_keys=True)),sort_keys=True).encode())
    # Hash declarative policies privately, never raw results, delivery queues,
    # baselines, enrichment progress or last-run telemetry. Declarations can
    # drift from code; a changed hash is a review signal, not compliance proof.
    policy={k:state[k] for k in ('alert_rule','cadence','collection_failure_policy',
            'display_policy','activity_incremental_review','delivery_policy','scope') if k in state}
    h.update(json.dumps(policy,sort_keys=True,separators=(',',':')).encode())
    return h.hexdigest()

def sanitized_source(text):
    # Restored sources are materialized only after operator selects a new root.
    text=text.replace(HOME.as_posix(),'__HERMES_HOME__').replace(str(HOME).replace('\\','/'),'__HERMES_HOME__')
    text=text.replace('__DOCUMENTS__','__DOCUMENTS__').replace('__USER_HOME__','__USER_HOME__')
    # Hermes job identities are deployment inputs, not portable source values.
    text=re.sub(r"(?<=[\"'])[0-9a-f]{12}(?=[\"'])", '__PRIVATE_JOB_ID__', text)
    return text

def scan(text):
    patterns=[r'0x[0-9a-fA-F]{40}',r'\b\d{17,20}\b',r'C:[/\\]Users[/\\](?!Public\b)[^/\\\s]+',
              r'https?://[^\s\"\'<>]*(?:discord(?:app)?\.com/api/webhooks|chat\.whatsapp\.com)',
              r'(?i)(?:gh[pousr]_[a-zA-Z0-9]{20,}|sk-[a-zA-Z0-9]{20,})']
    if any(re.search(p,text) for p in patterns):
        raise ValueError('Snapshot privacy scan failed (value withheld)')
    for host in re.findall(r'https?://([^/\s\"\'<>;,)}]+)',text):
        if host.rstrip('.') not in {'127.0.0.1:9222','[::1]:9222','api.coingecko.com','api.rarible.org','mcfarlanetoys.digital','discord.com'}:
            raise ValueError('Non-allowlisted URL in snapshot (value withheld)')

def snapshot(kind, target, docs=DOCS):
    target=Path(target); target.mkdir(parents=True,exist_ok=True)
    names=[]
    for src in source_paths(kind):
        rel=src.relative_to(HOME); dest=target/rel; dest.parent.mkdir(parents=True,exist_ok=True)
        text=sanitized_source(src.read_text(encoding='utf-8')); scan(text)
        dest.write_text(text,encoding='utf-8'); names.append(rel.as_posix())
    spec=manifest()[kind]
    for ext in ('md','docx'):
        src=docs/(spec['prd']+'.'+ext); dest=target/src.name
        if ext=='md': scan(src.read_text(encoding='utf-8'))
        else:
            scan_docx(src)
        shutil.copy2(src,dest); names.append(dest.name)
    config={'kind':kind,'schedules':schedules(kind),'restore_ready':False,
            'private_inputs_required':['collection allowlist and exact filtered URLs','verified baseline/dedupe state or controlled no-alert rebaseline','delivery destinations','RARIBLE_API_KEY','optional private wallet aliases'],
            'python':['python-docx','psutil','tzdata'],'node':['playwright'],
            'excluded':['raw state','wallet alias database','credentials','chat IDs','execution ledger','logs','browser profiles','node_modules','Python environment']}
    (target/'restore-config.json').write_text(json.dumps(config,indent=2)+'\n',encoding='utf-8'); names.append('restore-config.json')
    instructions='''# Source-only restore — NOT a disaster-recovery state backup

Keep every restored schedule disabled until private configuration and baseline validation are complete.
Install Python 3.11+, python-docx, psutil and tzdata; Node.js and Playwright (install under HERMES_HOME/hermes-agent so the existing absolute module reference resolves). Install Chrome separately. No package binaries or dependency lockfiles are included: versions must be validated on the target host.

Copy scripts/ and price-watches/ preserving their sibling layout. Materialize __HERMES_HOME__, __DOCUMENTS__, __USER_HOME__ in copied text files to forward-slash absolute target paths before execution. Do not run the templates in place. The production source uses fixed Windows paths; this explicit relocation step is required, including Node at HERMES_HOME/node/node.exe. Add Node to PATH for the WhatsApp wrapper. Review git repository paths separately before enabling backups.

Start a dedicated Chrome instance bound to loopback CDP port 9222, with a NEW monitor-only user-data directory. Never reuse a normal browser profile, expose CDP externally, or terminate user Chrome processes. Both collector and enrichment currently expect http://[::1]:9222. Browser startup/supervision and scheduler installation are deployment responsibilities, not bundled services.

Install Hermes independently. Recreate no-agent cron schedules from restore-config.json using supported Hermes scheduling controls, configure timezone America/New_York and destinations privately. Replace __PRIVATE_JOB_ID__ in the two Exotic heartbeat-context scripts with the new primary job identity. Original job IDs are not included. The executions.db schema must provide id, job_id, pid, status, scheduled_instant. Scheduler timeouts must exceed the collector budget plus preflight.

Supply private mcfarlane-exotics.json or mcfarlane-dc-sales.json in price-watches. Exact collection URLs/allowlists, historic baselines, dedupe and pending delivery records are deliberately NOT here. Supply mcfarlane-wallet-aliases.json (an empty exact_aliases/masked_aliases structure is acceptable) and mcfarlane-pol-usd-daily.json (empty rates object is acceptable for sales). RARIBLE_API_KEY is injected via the runtime secret facility, never this repository. A lost baseline requires a separately approved silent rebaseline before alerts; do not start against an empty production state or replay saved pending events.

No automatically restored live state or end-to-end marketplace/delivery claim is made. Offline verification checks source closure and syntax only. Existing backup repositories may contain legacy private artifacts: review/remove those before any push; this source snapshot does not sanitize git history.
'''
    (target/'RESTORE.md').write_text(instructions,encoding='utf-8'); names.append('RESTORE.md')
    inventory={name:hashlib.sha256((target/name).read_bytes()).hexdigest() for name in sorted(names)}
    (target/'snapshot-manifest.json').write_text(json.dumps({'kind':kind,'files':inventory},indent=2)+'\n',encoding='utf-8')
    return names+['snapshot-manifest.json']

def scan_docx(path):
    import zipfile
    from xml.etree import ElementTree as ET
    with zipfile.ZipFile(path) as package:
        if package.testzip(): raise ValueError('Corrupt DOCX package')
        for name in package.namelist():
            if name.endswith(('.xml','.rels')):
                root=ET.fromstring(package.read(name))
                scan(' '.join(root.itertext()))
                for element in root.iter():
                    if element.get('TargetMode') == 'External': scan(element.get('Target',''))
            elif name.startswith(('word/embeddings/','word/media/')):
                raise ValueError('Unreviewed embedded DOCX binary')

def verify(target):
    target=Path(target); inventory=json.loads((target/'snapshot-manifest.json').read_text(encoding='utf-8'))
    actual={p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}
    if actual - (set(inventory['files']) | {'snapshot-manifest.json'}):
        raise ValueError('Unlisted snapshot files')
    required={p.relative_to(HOME).as_posix() for p in source_paths(inventory['kind'])}
    if not required.issubset(inventory['files']):
        raise ValueError('Manifest omits required runtime dependency')
    stem=manifest()[inventory['kind']]['prd']
    approved=required | {stem+'.md',stem+'.docx','RESTORE.md','restore-config.json'}
    if set(inventory['files']) != approved:
        raise ValueError('Snapshot inventory differs from explicit allowlist')
    for name,digest in inventory['files'].items():
        p=target/name
        if Path(name).is_absolute() or '..' in Path(name).parts or p.is_symlink(): raise ValueError('Unsafe snapshot path')
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=digest: raise ValueError('Missing or changed artifact: '+name)
        if p.suffix in ('.py','.js','.json','.md'):
            text=p.read_text(encoding='utf-8'); scan(text)
            if p.suffix=='.py': ast.parse(text,filename=name)
        elif p.suffix=='.docx': scan_docx(p)
    return len(inventory['files'])

def daily_allowed(stamp,today):
    return not stamp.exists() or stamp.read_text(encoding='utf-8').strip()!=today

def repository_gate(repo, git, names):
    """Shared fail-closed gate; only hash-preserved, reviewed deletions allowed."""
    approval=repo/'.git/monitor-backup-cleanup.json'
    reviewed=json.loads(approval.read_text(encoding='utf-8'))['deletions'] if approval.exists() else {}
    tracked=set(git('ls-files').splitlines())
    deletions=[]
    for name, record in reviewed.items():
        if name not in tracked: continue  # already committed removal
        if name in names or Path(name).is_absolute() or '..' in Path(name).parts:
            raise RuntimeError('Unsafe cleanup approval')
        saved=Path(record['saved'])
        if saved.resolve().is_relative_to(repo.resolve()) or not saved.is_file() or hashlib.sha256(saved.read_bytes()).hexdigest()!=record['sha256']:
            raise RuntimeError('Cleanup quarantine missing or changed')
        if (repo/name).exists(): raise RuntimeError('Reviewed deletion reappeared')
        deletions.append(name)
    owned={'backup/.last-successful-backup-date','backup/.last-push-attempt-date'}
    for line in git('status','--porcelain','--untracked-files=all').splitlines():
        name=line[3:]
        if name in owned or re.fullmatch(r'backup/\.push-attempt-\d{4}-\d{2}-\d{2}',name):
            if line[:2]!='??': raise RuntimeError('Tracked backup marker change')
            continue
        if name in deletions and line[:2] in (' D','D '): continue
        raise RuntimeError('Backup repository is not clean; review existing changes before backup')
    extras=tracked-set(names)-set(deletions)-{'.gitignore'}
    if extras: raise RuntimeError('Legacy artifacts require explicit repository cleanup: '+', '.join(sorted(extras)))
    return sorted(deletions)

def backup_main(kind):
    parser=argparse.ArgumentParser()
    parser.add_argument('--snapshot',type=Path,help='Local source-only snapshot; no git/network')
    args=parser.parse_args()
    if args.snapshot:
        snapshot(kind,args.snapshot); print(f'Offline snapshot verified: {verify(args.snapshot)} artifacts'); return
    repo=Path(os.environ.get('MCFARLANE_'+kind.upper()+'_BACKUP_REPO',str(Path.home()/('Documents/HermesAgents-sanitized-backup' if kind=='exotic' else 'HermesAgents-MTDSalesAlerts'))))
    today=datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    stamp=repo/'backup/.last-successful-backup-date'
    reservation=repo/('backup/.push-attempt-'+today)
    # Check before any repo/staging mutation, including pending commits.
    if not daily_allowed(stamp,today) or not daily_allowed(reservation,today):
        print('Daily cap: backup already succeeded or push was attempted today; deferred.'); return
    subprocess.run([os.sys.executable,str(HOME/'scripts/refresh-mcfarlane-prds.py')],check=True,timeout=120)
    env=dict(os.environ,GIT_TERMINAL_PROMPT='0')
    def git(*a): return subprocess.run(['git',*a],cwd=repo,env=env,text=True,capture_output=True,check=True,timeout=120).stdout.rstrip('\n')
    with tempfile.TemporaryDirectory(prefix='monitor-snapshot-') as tmp:
        names=snapshot(kind,Path(tmp)); verify(Path(tmp))
        deletions=repository_gate(repo,git,names)
        changed=[n for n in names if not (repo/n).exists() or (repo/n).read_bytes()!=(Path(tmp)/n).read_bytes()]
        if not changed and not deletions:
            print('No sanitized monitor changes to back up.'); return
        for n in changed:
            (repo/n).parent.mkdir(parents=True,exist_ok=True); shutil.copy2(Path(tmp)/n,repo/n)
        git('add','-A','--',*(changed+deletions))
        git('-c','user.name=Monitor backup','-c','user.email=monitor@users.noreply.github.com','commit','-m',f'backup: {kind} source snapshot {today}','--',*(changed+deletions))
        reservation.parent.mkdir(parents=True,exist_ok=True)
        # Fail closed on uncertain push outcome: no second attempt this ET day.
        with reservation.open('x',encoding='utf-8') as handle:
            handle.write(today+'\n')
        git('push','origin','main')
        if git('rev-parse','HEAD') != git('ls-remote','origin','refs/heads/main').split()[0]:
            raise RuntimeError('Remote verification did not match local commit')
        stamp.write_text(today+'\n',encoding='utf-8')
        print('Sanitized source backup pushed and remote commit verified.')
