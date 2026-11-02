"""Explicit source-only snapshots. No network in snapshot/verification modes."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import secrets
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
        if host.rstrip('.') not in {'127.0.0.1:8765','127.0.0.1:9222','[::1]:9222','api.coingecko.com','api.rarible.org','mcfarlanetoys.digital','discord.com'}:
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

Supply private mcfarlane-exotics.json or mcfarlane-dc-sales.json in price-watches. Exact collection URLs/allowlists, historic baselines, dedupe and pending delivery records are deliberately NOT here. Supply mcfarlane-wallet-aliases.json using schema 3 canonical aliases rows with separate wallet and alias fields; keep synchronized exact_aliases/masked_aliases compatibility views for older restored components (an empty aliases/exact_aliases/masked_aliases structure is acceptable). Supply mcfarlane-pol-usd-daily.json (empty rates object is acceptable for sales). RARIBLE_API_KEY is injected via the runtime secret facility, never this repository. A lost baseline requires a separately approved silent rebaseline before alerts; do not start against an empty production state or replay saved pending events.

No automatically restored live state or end-to-end marketplace/delivery claim is made. Offline verification checks source closure and syntax only. Existing backup repositories may contain legacy private artifacts: review/remove those before any push; this source snapshot does not sanitize git history.
'''
    (target/'RESTORE.md').write_text(instructions,encoding='utf-8'); names.append('RESTORE.md')
    inventory={name:hashlib.sha256((target/name).read_bytes()).hexdigest() for name in sorted(names)}
    (target/'snapshot-manifest.json').write_text(json.dumps({'kind':kind,'files':inventory},indent=2)+'\n',encoding='utf-8')
    return names+['snapshot-manifest.json']

def scan_docx(path):
    import zipfile
    import io
    from xml.etree import ElementTree as ET
    with zipfile.ZipFile(io.BytesIO(path) if isinstance(path, bytes) else path) as package:
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

# Frozen identities of the four legacy backup commits reviewed during repair.
# Metadata is an additional check, never proof of backup ownership.
REVIEWED_LEGACY_COMMITS = {
    'sales': {'219b8242b4aeed49b4f7514897483116838d801c', '8873c7476e09643955130d004de1ab984b1c0cee'},
    'exotic': {'616b0f6ea373b6afb98ab33f9d6388c99d580fac', '1eb02cb37e995cb5ecc403d9948df0552012610a'},
}


@contextmanager
def publication_lock(repo):
    """Serialize backup mutations; never recover or clear an existing owner."""
    lock=repo/'.git/monitor-backup.lock'
    token=secrets.token_hex(16)
    try:
        with lock.open('x',encoding='utf-8') as handle:
            handle.write(token)
    except FileExistsError as error:
        raise RuntimeError('Backup publication is busy; existing lock retained') from error
    try:
        yield
    finally:
        if lock.is_file() and lock.read_text(encoding='utf-8')==token:
            lock.unlink()


def validate_backup_commit(kind, git, commit, names, deletions=()):
    """Verify historical contents, not merely the current sanitized snapshot."""
    record=git('show','--format=%s%n%an <%ae>%n%P','--no-patch',commit).splitlines()
    if len(record)!=3 or not re.fullmatch(r'backup: '+re.escape(kind)+r' source snapshot \d{4}-\d{2}-\d{2}',record[0]) \
            or record[1]!='Monitor backup <monitor@users.noreply.github.com>' \
            or not re.fullmatch(r'[0-9a-f]{40}',record[2]):
        raise RuntimeError('Unreviewed outstanding commit; refusing publication')
    paths=set(git('diff-tree','--no-commit-id','--name-only','-r',commit).splitlines())
    if not paths or paths-set(names)-set(deletions):
        raise RuntimeError('Outstanding commit differs from approved source inventory')
    raw_inventory=git('show',commit+':snapshot-manifest.json'); scan(raw_inventory)
    inventory=json.loads(raw_inventory)
    if set(inventory)!={'kind','files'}:raise RuntimeError('Unreviewed snapshot metadata')
    files=inventory['files']
    if inventory.get('kind')!=kind or not isinstance(files,dict) or set(files)-set(names):
        raise RuntimeError('Outstanding snapshot inventory is not approved')
    tree={}
    for line in git('ls-tree','-r',commit).splitlines():
        metadata,name=line.split('\t',1)
        if metadata.split()[:2] not in (['100644','blob'],['100755','blob']):
            raise RuntimeError('Unsafe outgoing Git tree entry')
        tree[name]=metadata
    if set(tree)-{'.gitignore'} != set(files)|{'snapshot-manifest.json'}:
        raise RuntimeError('Outgoing Git tree differs from exact snapshot inventory')
    for name,digest in files.items():
        if Path(name).is_absolute() or '..' in Path(name).parts or name not in names:
            raise RuntimeError('Unsafe outgoing snapshot path')
        data=git('show',commit+':'+name,binary=True)
        variants={data}
        if Path(name).suffix in ('.py','.js','.json','.md','.yaml'):
            # Git may normalize Windows CRLF text blobs to LF on commit.
            lf=data.replace(bytes((13,10)),bytes((10,)))
            variants.update((lf,lf.replace(bytes((10,)),bytes((13,10)))))
        if digest not in {hashlib.sha256(value).hexdigest() for value in variants}:
            raise RuntimeError('Outgoing snapshot hash mismatch')
        if name.endswith('.docx'):
            scan_docx(data)
        elif Path(name).suffix in ('.py','.js','.json','.md','.yaml'):
            text=data.decode('utf-8'); scan(text)
            if name.endswith('.py'):ast.parse(text,filename=name)
        else:
            raise RuntimeError('Unreviewed outgoing artifact type')
    return record[2]


def outstanding_backup_commits(kind, git, names, owned=(), deletions=()):
    if git('branch','--show-current')!='main':
        raise RuntimeError('Backup repository must be on main')
    head=git('rev-parse','HEAD')
    remote=git('ls-remote','origin','refs/heads/main').split()
    if len(remote)!=2 or not re.fullmatch(r'[0-9a-f]{40}',remote[0]) or remote[1]!='refs/heads/main':
        raise RuntimeError('Cannot establish remote main')
    if head==remote[0]:return head,remote[0],[]
    git('merge-base','--is-ancestor',remote[0],head)
    commits=git('rev-list','--reverse',remote[0]+'..'+head).splitlines()
    if not commits:raise RuntimeError('Cannot establish outstanding backup history')
    approved=set(REVIEWED_LEGACY_COMMITS.get(kind,()))|set(owned)
    parent=remote[0]
    for commit in commits:
        if not re.fullmatch(r'[0-9a-f]{40}',commit) or commit not in approved:
            raise RuntimeError('Outstanding commit has no reviewed backup ownership')
        if validate_backup_commit(kind,git,commit,names,deletions)!=parent:
            raise RuntimeError('Outstanding history is not a linear backup chain')
        parent=commit
    if parent!=head:raise RuntimeError('Outstanding history does not end at HEAD')
    return head,remote[0],commits


def backup_main(kind):
    parser=argparse.ArgumentParser()
    parser.add_argument('--snapshot',type=Path,help='Local source-only snapshot; no git/network')
    args=parser.parse_args()
    if args.snapshot:
        snapshot(kind,args.snapshot); print(f'Offline snapshot verified: {verify(args.snapshot)} artifacts'); return
    repo=Path(os.environ.get('MCFARLANE_'+kind.upper()+'_BACKUP_REPO',str(Path.home()/('Documents/HermesAgents-sanitized-backup' if kind=='exotic' else 'HermesAgents-MTDSalesAlerts'))))
    def capped():
        today=datetime.now(ZoneInfo('America/New_York')).date().isoformat()
        stamp=repo/'backup/.last-successful-backup-date'
        reservation=repo/('backup/.push-attempt-'+today)
        return today,stamp,reservation,(not daily_allowed(stamp,today) or reservation.exists())
    if capped()[3]:
        print('Daily cap: backup already succeeded or push was attempted today; deferred.'); return
    with publication_lock(repo):
        today,stamp,reservation,blocked=capped()
        if blocked:
            print('Daily cap: backup already succeeded or push was attempted today; deferred.'); return
        subprocess.run([os.sys.executable,str(HOME/'scripts/refresh-mcfarlane-prds.py')],check=True,timeout=120)
        env=dict(os.environ,GIT_TERMINAL_PROMPT='0')
        def git(*a,binary=False):
            out=subprocess.run(['git',*a],cwd=repo,env=env,text=not binary,capture_output=True,check=True,timeout=120).stdout
            return out if binary else out.rstrip('\n')
        ownership=repo/'.git/monitor-backup-owned-commits.json'
        owned=json.loads(ownership.read_text(encoding='utf-8')) if ownership.exists() else []
        if not isinstance(owned,list) or any(not isinstance(c,str) or not re.fullmatch(r'[0-9a-f]{40}',c) for c in owned):
            raise RuntimeError('Invalid backup ownership record; review required')
        with tempfile.TemporaryDirectory(prefix='monitor-snapshot-') as tmp:
            names=snapshot(kind,Path(tmp)); verify(Path(tmp))
            deletions=repository_gate(repo,git,names)
            head,remote,pending=outstanding_backup_commits(kind,git,names,owned,deletions)
            changed=[n for n in names if not (repo/n).exists() or (repo/n).read_bytes()!=(Path(tmp)/n).read_bytes()]
            if not changed and not deletions and not pending:
                print('No sanitized monitor changes; local backup is already published.'); return
            # Recheck immediately before mutating; the lock serializes our jobs,
            # while these checks also catch independently changed repository state.
            if git('rev-parse','HEAD')!=head:raise RuntimeError('Backup HEAD changed during validation')
            repository_gate(repo,git,names)
            for n in changed:
                (repo/n).parent.mkdir(parents=True,exist_ok=True); shutil.copy2(Path(tmp)/n,repo/n)
            if changed or deletions:
                git('add','-A','--',*(changed+deletions))
                git('-c','user.name=Monitor backup','-c','user.email=monitor@users.noreply.github.com','commit','-m',f'backup: {kind} source snapshot {today}','--',*(changed+deletions))
                created=git('rev-parse','HEAD')
                if created==head or validate_backup_commit(kind,git,created,names,deletions)!=head:
                    raise RuntimeError('New backup commit has unexpected ancestry')
                # The created object must match the exact freshly verified bytes.
                for name in names:
                    actual=git('show',created+':'+name,binary=True); expected=(Path(tmp)/name).read_bytes()
                    if Path(name).suffix in ('.py','.js','.json','.md','.yaml'):
                        actual=actual.replace(bytes((13,10)),bytes((10,)))
                        expected=expected.replace(bytes((13,10)),bytes((10,)))
                    if actual!=expected:raise RuntimeError('New backup commit differs from verified snapshot')
                pending.append(created);head=created
                temporary=ownership.with_name(ownership.name+'.'+secrets.token_hex(8))
                with temporary.open('x',encoding='utf-8') as handle:json.dump(pending,handle)
                os.replace(temporary,ownership)
            repository_gate(repo,git,names)
            if git('rev-parse','HEAD')!=head:raise RuntimeError('Backup HEAD changed before publication')
            # Re-evaluate the Eastern day at the actual attempt boundary.
            today,stamp,reservation,blocked=capped()
            if blocked:
                print('Daily cap reached during preparation; outstanding backup retained.'); return
            reservation.parent.mkdir(parents=True,exist_ok=True)
            with reservation.open('x',encoding='utf-8') as handle:handle.write(today+'\n')
            git('push','origin',head+':refs/heads/main')
            observed=git('ls-remote','origin','refs/heads/main').split()
            if len(observed)!=2 or observed!=[head,'refs/heads/main']:
                raise RuntimeError('Remote verification did not match validated backup commit')
            stamp.write_text(today+'\n',encoding='utf-8')
            print('Sanitized source backup pushed and remote commit verified.')
