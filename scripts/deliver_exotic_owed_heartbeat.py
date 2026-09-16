"""Saved-only owed 8AM handoff. No browser, marketplace, or sender calls.

Default: stdout once when debt is due and every collection has recovered;
cron owns actual Discord delivery. --status never consumes. --seed-execution
repairs a verified *today* scheduled occurrence under the shared lease, no send.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from exotic_execution_lock import run_locked, BUSY

HOME = Path(__file__).resolve().parents[1]
STATE = HOME / 'price-watches' / 'mcfarlane-exotics.json'
LEDGER = HOME / 'cron' / 'executions.db'
ET = ZoneInfo('America/New_York')


def seed(execution_id, now=None):
    """Caller must own execution lease; reread state after acquisition."""
    now = now or datetime.now(ET)
    with closing(sqlite3.connect(LEDGER.as_uri()+'?mode=ro',uri=True)) as conn:
        row = conn.execute('SELECT scheduled_instant,status FROM executions WHERE id=? AND job_id=?',
                           (execution_id,'7fbfa20e145b')).fetchone()
    if not row or not row[0] or row[1] not in ('running','completed'):
        raise ValueError('Not a verified primary scheduled execution')
    scheduled = datetime.fromisoformat(row[0].replace('Z','+00:00'))
    if scheduled.tzinfo is None:
        raise ValueError('Occurrence lacks timezone')
    local = scheduled.astimezone(ET)
    if local.date() != now.astimezone(ET).date() or (local.hour,local.minute,local.second,local.microsecond) != (8,0,0,0) or scheduled > now:
        raise ValueError('Only today actual elapsed 08:00 ET occurrence can be repaired')
    state = json.loads(STATE.read_text(encoding='utf-8'))
    date = local.date().isoformat()
    if (state.get('discord_daily_heartbeat') or {}).get('date','') >= date:
        return 0
    if (state.get('pending8am') or {}).get('date','') >= date:
        return 0
    state['pending8am'] = {'date':date,'execution_id':execution_id,'scheduled_at':row[0],
                           'generation':state.get('last_primary_exotic_run_id'),
                           'armed_at':now.isoformat(),'source':'verified-cron-ledger-repair'}
    tmp=STATE.with_name(STATE.name+f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(tmp,STATE)
    return 0


def status():
    state=json.loads(STATE.read_text(encoding='utf-8'))
    pending=state.get('pending8am')
    outcome=state.get('last_monitor_outcome') or {}
    scheduled=datetime.fromisoformat(pending['scheduled_at'].replace('Z','+00:00')) if pending else None
    rows=state.get('collections',[])
    ready=bool(pending and pending['date']==datetime.now(ET).date().isoformat()
               and (state.get('discord_daily_heartbeat') or {}).get('date','') < pending['date']
               and outcome.get('complete') is True and not outcome.get('failed_collections')
               and rows and all(c.get('baseline') is not None and c.get('last_successful_observation') and
                  datetime.fromisoformat(c['last_successful_observation'].replace('Z','+00:00')) >= scheduled for c in rows))
    print(json.dumps({'ready':ready,'pending8am':pending,'handed_off':state.get('discord_daily_heartbeat'),
                      'collections':len(rows),'complete':outcome.get('complete'),
                      'failed_collections':outcome.get('failed_collections'),'outcome_at':outcome.get('at')}))
    return 0


def handoff():
    result=subprocess.run([str(HOME/'node'/'node.exe'),str(HOME/'price-watches'/'run_mcfarlane_exotic_deterministic.js'),
                           '--handoff-saved-owed-heartbeat'],text=True,capture_output=True,encoding='utf-8',timeout=30)
    if result.stdout: sys.stdout.write(result.stdout)
    if result.stderr: sys.stderr.write(result.stderr)
    return 0 if result.returncode == BUSY else result.returncode


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--status',action='store_true')
    mode.add_argument('--seed-execution')
    args=parser.parse_args()
    if args.status: return status()
    if args.seed_execution: return run_locked(lambda:seed(args.seed_execution))
    return handoff()

if __name__=='__main__': raise SystemExit(main())
