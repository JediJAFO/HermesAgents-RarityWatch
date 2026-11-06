"""Saved-only demand queue. Disabled by the shared-source family flags.

No marketplace transport. Leases cover bounded queue operations only. Consumers
retain their own state/leases/delivery. Never unlink any lease pathname.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import uuid

HOME = Path(__file__).resolve().parents[1]
STATE = HOME / 'price-watches/mtd-shared-listing-demands.json'


def instant(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result.astimezone(timezone.utc)


@contextmanager
def lease(path, *, readonly=False):
    """Real nonblocking OS byte lock; readonly probes do not create/write files."""
    path = Path(path)
    if readonly and not path.exists():
        yield True
        return
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = open(path, 'r+b' if readonly else 'a+b')
    except FileNotFoundError:
        yield True
        return
    acquired = False
    try:
        if not readonly and handle.seek(0, 2) == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        yield acquired
    finally:
        if acquired:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _read(path):
    value = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'schema': 1, 'requests': {}}
    if value.get('schema') != 1 or not isinstance(value.get('requests'), dict):
        raise ValueError('invalid demand queue; refusing reset')
    files=sorted(path.parent.glob(path.name+'.inbox.*.json'))
    acknowledged=set(value.get('inbox_acked',[]))
    value['inbox_acked']=[name for name in acknowledged if (path.parent/name).exists()]
    for file in files:
        if file.name in acknowledged: continue
        entry=json.loads(file.read_text(encoding='utf-8'))
        prior=value['requests'].get(entry['key'])
        if not prior or instant(entry['min_started_at'])>=instant(prior['min_started_at']):
            value['requests'][entry['key']]=entry
    value['_inbox_files']=[str(file) for file in files]
    return value


def _write(path, value):
    value=dict(value)
    inbox=value.pop('_inbox_files',[])
    if inbox:
        value['inbox_acked']=sorted(set(value.get('inbox_acked',[]))|{Path(p).name for p in inbox})
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with open(tmp, 'x', encoding='utf-8') as f:
            json.dump(value, f, sort_keys=True); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        for file in inbox:
            Path(file).unlink(missing_ok=True)
    finally:
        tmp.unlink(missing_ok=True)


def request_refresh(key, min_started_at, *, after_generation=None, state_path=None,
                    family=None, mode=None, occurrence=None):
    """Register demand, dedup exact requirements; returns immutable ack token.

    after_generation should be {generation, started_at, observed_at}; strings
    reject reuse of that generation, but callers must also supply its end as
    min_started_at to establish strictly independent producer observations.
    """
    path = Path(state_path or STATE)
    minimum = instant(min_started_at).isoformat()
    entry = dict(key=key, min_started_at=minimum, after_generation=after_generation,
                 family=family, mode=mode, occurrence=occurrence)
    with lease(path.with_suffix('.lock')) as acquired:
        if not acquired:
            # Immutable durable arrival journal: a busy queue must not erase an
            # original morning occurrence. Main queue folds it under its lease.
            import hashlib
            entry['token']=hashlib.sha256(json.dumps(entry,sort_keys=True).encode()).hexdigest()
            journal=path.with_name(path.name+'.inbox.'+entry['token']+'.json')
            _write(journal,entry)
            return entry['token']
        value = _read(path)
        prior = value['requests'].get(key)
        if prior and instant(prior['min_started_at']) > instant(minimum):
            return prior['token']
        if prior and all(prior.get(k) == v for k, v in entry.items()):
            return prior['token']
        entry['token'] = uuid.uuid4().hex
        value['requests'][key] = entry
        _write(path, value)
        return entry['token']


def acknowledge(key, token, *, state_path=None):
    path = Path(state_path or STATE)
    with lease(path.with_suffix('.lock')) as acquired:
        if not acquired:
            return False
        value = _read(path)
        if value['requests'].get(key, {}).get('token') != token:
            return False
        del value['requests'][key]
        _write(path, value)
        return True


def enabled(family):
    from mtd_shared_listing_source import shared_enabled
    return shared_enabled(family)


def _load_view(**kwargs):
    from mtd_shared_listing_source import load_view
    return load_view(**kwargs)


def _config_int(name, default, maximum):
    from mtd_shared_listing_source import CONFIG
    try:
        value = json.loads(CONFIG.read_text(encoding='utf-8')).get(name, default)
        return value if type(value) is int and 0 <= value <= maximum else default
    except (OSError, ValueError, AttributeError):
        return default


def completion_daily_time():
    if not enabled('completion'):
        return 9, 0
    return (_config_int('completion_daily_hour', 9, 23), _config_int('completion_daily_minute', 0, 59))


def source_minimum(occurrence, family):
    """Pinned debt wins over config; invalid lag never relaxes freshness."""
    if occurrence.get('source_min_started_at'):
        instant(occurrence['source_min_started_at'])
        return occurrence['source_min_started_at']
    scheduled = occurrence['scheduled_at']
    if not enabled(family) or occurrence.get('manual'):
        return scheduled
    from datetime import timedelta
    lag = _config_int('evaluation_lag_seconds', 0, 3600)
    return (instant(scheduled) - timedelta(seconds=lag)).isoformat() if lag else scheduled


def pin_occurrence(occurrence, family, *, legacy=False):
    if not occurrence or not enabled(family) or occurrence.get('manual'):
        return occurrence
    if family != 'completion' and not legacy and _config_int('evaluation_lag_seconds', 0, 3600) == 0:
        return occurrence
    result = dict(occurrence)
    result.setdefault('source_min_started_at', occurrence['scheduled_at'] if legacy else source_minimum(occurrence, family))
    if family == 'completion' and 'daily_heartbeat' not in result:
        from zoneinfo import ZoneInfo
        local = instant(occurrence['scheduled_at']).astimezone(ZoneInfo('America/New_York'))
        result['daily_heartbeat'] = (local.hour, local.minute, local.second, local.microsecond) == (*((9, 0) if legacy else completion_daily_time()), 0, 0)
    return result


def _primary_requirement(key, occurrence, family, state_path=None):
    path = Path(state_path or STATE)
    prior = _read(path)['requests'].get(key)
    if prior:
        return prior['min_started_at'], prior.get('occurrence') or occurrence
    occurrence = pin_occurrence(occurrence, family)
    return source_minimum(occurrence, family), occurrence


def source_ready(min_started_at, after_generation=None, **kwargs):
    """Validated completed source or False; never manufacture zero observations."""
    try:
        view = _load_view(**kwargs)
        start, end = instant(view['started_at']), instant(view['observed_at'])
        if start < instant(min_started_at) or end < start:
            return False
        if after_generation:
            prior = after_generation if isinstance(after_generation, dict) else {'generation': after_generation}
            if view['generation'] == prior['generation']:
                return False
            if prior.get('started_at') and start <= instant(prior['started_at']):
                return False
            if prior.get('observed_at') and (end <= instant(prior['observed_at']) or start <= instant(prior['observed_at'])):
                return False
        return view
    except Exception:
        return False


def completion_preflight(occurrence, *, state_path=None):
    """Caller must validate ownership first. No invented cron occurrence."""
    if not enabled('completion'):
        return True
    if not isinstance(occurrence, dict) or occurrence.get('manual') or not occurrence.get('execution_id') or not occurrence.get('scheduled_at'):
        return False
    try:
        minimum, occurrence = _primary_requirement('completion:' + str(occurrence['execution_id']), occurrence, 'completion', state_path)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if source_ready(minimum):
        return True
    try:
        request_refresh('completion:' + str(occurrence['execution_id']), minimum,
                        state_path=state_path, family='completion', occurrence=occurrence)
    except Exception:
        return False
    return False


from contextvars import ContextVar
CURRENT_OCCURRENCE = ContextVar('mtd_shared_original_occurrence', default=None)
JOBS = {('exotic','interval'):'__PRIVATE_JOB_ID__', ('exotic','daily'):'__PRIVATE_JOB_ID__',
        ('legendary','interval'):'__PRIVATE_JOB_ID__', ('legendary','daily'):'__PRIVATE_JOB_ID__'}


def owned_occurrence(family, mode, *, ledger=None, ancestor_pids=None):
    """Read exactly one owned running occurrence; never infer cron wall time."""
    import sqlite3
    try:
        direct_parent=os.getppid()
        if ancestor_pids is None:
            import psutil, sys
            parents=psutil.Process().parents()
            ancestor_pids = {p.pid for p in parents}
            script=Path(sys.argv[0]).resolve()
            for parent in parents:
                if not any(Path(arg).resolve()==script for arg in parent.cmdline()[1:]):
                    direct_parent=parent.pid;break
        path = Path(ledger or HOME/'cron/executions.db')
        from contextlib import closing
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            rows=db.execute("SELECT id,pid,scheduled_instant FROM executions WHERE job_id=? AND status='running'",(JOBS[(family,mode)],)).fetchall()
        rows=[r for r in rows if r[1] in ancestor_pids and r[2] and
              (os.environ.get('HERMES_CRON_SESSION')=='1' or r[1]==direct_parent)]
        if len(rows)==1:
            instant(rows[0][2])
            return {'execution_id':rows[0][0],'scheduled_at':rows[0][2]}
    except Exception:
        pass
    return None


def _monitor(family):
    import importlib, sys
    if family not in ('exotic','legendary'): raise ValueError('unknown rarity')
    directory=str(HOME/'price-watches')
    if directory not in sys.path: sys.path.insert(0,directory)
    return importlib.import_module('mtd_'+family+'_lowest_monitor')


def _consume(family,mode,occurrence):
    token=CURRENT_OCCURRENCE.set(occurrence)
    try: return _monitor(family).collect(mode)
    finally: CURRENT_OCCURRENCE.reset(token)


def _monitor_state(family):
    return json.loads((HOME/('price-watches/mtd-'+family+'-lowest-prices.json')).read_text(encoding='utf-8'))


def _confirmation_requirements(state):
    from datetime import timedelta
    candidates=list((state.get('pending_collection_snapshots') or {}).values())
    if not candidates: return None
    ends=[]; prior=None
    for candidate in candidates:
        source=candidate.get('source_generation') or candidate.get('source')
        if isinstance(source,dict):
            ends.append(instant(source['observed_at']))
            if prior is None or instant(source['observed_at'])>instant(prior['observed_at']): prior=source
        elif candidate.get('first_seen_at'): ends.append(instant(candidate['first_seen_at']))
        else: raise ValueError('candidate lacks provenance')
    return (max(ends)+timedelta(microseconds=1)).isoformat(),prior


def _queue_confirmation(family,state_path):
    requirements=_confirmation_requirements(_monitor_state(family))
    if requirements:
        minimum,prior=requirements
        request_refresh(family+':confirm',minimum,after_generation=prior,
                        family=family,mode='confirm',state_path=state_path)


def _retry_delivery(family, saved):
    # Consumer owns its lease and immutable batch/idempotency ledger.
    if saved.get('pending_alert_batches'):
        return _monitor(family).retry_pending_alerts()


def run_rarity(family,mode,*,occurrence=None,state_path=None,consumer=None,monitor_state=None):
    consume=consumer or _consume
    if not enabled(family): return consume(family,mode,None)
    try:
        if mode != 'baseline':
            _retry_delivery(family, monitor_state if monitor_state is not None else _monitor_state(family))
        if mode=='confirm':
            requirements=_confirmation_requirements(monitor_state if monitor_state is not None else _monitor_state(family))
            if requirements is None: return consume(family,mode,None)
            minimum,prior=requirements; key=family+':confirm'
        else:
            occurrence=occurrence or owned_occurrence(family,mode)
            if occurrence is None:
                occurrence={'manual':True,'scheduled_at':datetime.now(timezone.utc).isoformat()}
            key=family+':'+mode+':'+occurrence['scheduled_at']
            minimum,occurrence=_primary_requirement(key,occurrence,family,state_path)
            saved=monitor_state if monitor_state is not None else _monitor_state(family)
            prior=saved.get('shared_last_source')

        token=request_refresh(key,minimum,after_generation=prior,state_path=state_path,
                              family=family,mode=mode,occurrence=occurrence)
        if not source_ready(minimum,after_generation=prior): return {'status':'deferred'}
        result=consume(family,mode,occurrence)
        if result.get('complete') or result.get('status')=='no_pending_candidates':
            _queue_confirmation(family,state_path)
            acknowledge(key,token,state_path=state_path)
        return result
    except Exception: return {'status':'deferred'}


def producer_busy():
    with lease(HOME/'price-watches/mtd-listing-wallets.worker.lock',readonly=True) as free:
        return not free


def _launch():
    import subprocess
    from launch_mtd_listing_wallets import launch_spec
    import psutil
    spec=launch_spec()
    kwargs={'cwd':spec['cwd'],'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL}
    if os.name=='nt': kwargs['creationflags']=subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS
    else: kwargs['start_new_session']=True
    child=subprocess.Popen(spec['args'],**kwargs)
    return {'pid':child.pid,'created':psutil.Process(child.pid).create_time()}


def _child_active(record):
    if not record: return False
    try:
        import psutil
        process=psutil.Process(record['pid'])
        return process.create_time()==record['created'] and process.is_running()
    except ImportError: return True
    except psutil.NoSuchProcess: return False
    except Exception: return True


def pump(*,state_path=None,consumer=None,launcher=None,producer_busy=None):
    """Bounded saved-only tick. Completion consumes in its existing retry job."""
    path=Path(state_path or STATE)
    if not path.exists() and not any(path.parent.glob(path.name+'.inbox.*.json')): return {'status':'idle'}
    consume=consumer or _consume
    try:
        with lease(path.with_suffix('.lock')) as acquired:
            if not acquired: return {'status':'deferred'}
            value=_read(path)
            if value.get('_inbox_files'): _write(path,value)
            requests=list(value['requests'].values())
        need_source=False; consumed=0
        for entry in sorted(requests,key=lambda r:instant(r['min_started_at'])):
            family=entry.get('family')
            if not family or not enabled(family): continue
            prior=entry.get('after_generation')
            if entry.get('mode') and entry['mode'] != 'baseline':
                _retry_delivery(family, _monitor_state(family))
            if entry.get('mode'):
                latest=_monitor_state(family).get('shared_last_source')
                if latest and (not isinstance(prior,dict) or instant(latest['observed_at'])>instant(prior['observed_at'])):
                    prior=latest
            if not source_ready(entry['min_started_at'],after_generation=prior):
                need_source=True;continue
            if entry.get('mode'):
                result=consume(family,entry['mode'],entry.get('occurrence'))
                if not (result.get('complete') or result.get('status')=='no_pending_candidates'): continue
                _queue_confirmation(family,path)
            if acknowledge(entry['key'],entry['token'],state_path=path): consumed+=1
        if need_source:
            with lease(path.with_suffix('.launch.lock')) as acquired:
                if acquired:
                    record_path=path.with_suffix('.child.json')
                    record=json.loads(record_path.read_text()) if record_path.exists() else None
                    if not _child_active(record) and not (producer_busy or globals()['producer_busy'])():
                        record=(launcher or _launch)()
                        if record: _write(record_path,record)
        return {'status':'ok','consumed':consumed}
    except Exception: return {'status':'deferred'}
