"""Windows-safe no-agent cron wrapper for the Exotic collector."""
import json
import subprocess
import sys
import os
import sqlite3
from pathlib import Path
from contextlib import closing
from exotic_execution_lock import run_locked


def scheduled_heartbeat_context(env=None, ledger=None, ancestor_pids=None):
    """Read the owned primary cron occurrence, never infer it from wall time.

    Hermes clears scheduled_instant for manual fires. Require cron session plus
    exactly one running primary ledger entry owned by an ancestor process. This
    works with both inline scheduler and adopted external-worker executions.
    Failure/old ledger schema is conservative: ordinary delta reporting.
    """
    env = os.environ if env is None else env
    if any(env.get(k) for k in
            ('MCFARLANE_RUN_KIND', 'MCFARLANE_COLLECTION_NAMES', 'MCFARLANE_LIMIT')):
        return None
    ledger = ledger or Path(__file__).resolve().parents[1] / 'cron' / 'executions.db'
    try:
        direct_parent = os.getppid()
        if ancestor_pids is None:
            import psutil
            parents = psutil.Process().parents()
            ancestor_pids = {p.pid for p in parents}
            # Windows venv Python launcher may add a same-script parent.
            for parent in parents:
                if not any(Path(arg).resolve() == Path(__file__).resolve() for arg in parent.cmdline()[1:]):
                    direct_parent = parent.pid
                    break
        with closing(sqlite3.connect(Path(ledger).as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
            rows = conn.execute("SELECT id, pid, scheduled_instant FROM executions WHERE job_id=? AND status='running'",
                                ('__PRIVATE_JOB_ID__',)).fetchall()
        # No-agent scheduler scripts do not export the cron ContextVar. In that
        # case accept only their direct scheduler parent, never a shell/manual
        # descendant that merely shares the gateway ancestor.
        owned = [r for r in rows if r[1] in ancestor_pids and
                 (env.get('HERMES_CRON_SESSION') == '1' or r[1] == direct_parent)]
        if len(owned) == 1 and owned[0][2]:
            return {'execution_id': owned[0][0], 'scheduled_at': owned[0][2]}
    except (OSError, sqlite3.Error, ImportError):
        return None
    return None


def main():
    command = ["__HERMES_HOME__/node/node.exe", "__HERMES_HOME__/price-watches/run_mcfarlane_exotic_deterministic.js"]
    child_env = dict(os.environ)
    child_env.pop('MCFARLANE_PRIMARY_OCCURRENCE', None)
    occurrence = scheduled_heartbeat_context()
    if occurrence:
        child_env['MCFARLANE_PRIMARY_OCCURRENCE'] = json.dumps(occurrence)
        subprocess.run(command + ['--arm-scheduled-heartbeat'], env=child_env,
                       text=True, capture_output=True, timeout=20, check=True)
    # Refresh the current display-only listing valuation first. A rate-source
    # failure retains the saved rate and never prevents listing collection.
    subprocess.run(
        [sys.executable, "__HERMES_HOME__/scripts/refresh_exotic_current_pol_usd.py"],
        text=True, capture_output=True, timeout=45, check=False,
    )
    # The inherited lease covers preflight and execution, keeping selection stable.
    budget_result = subprocess.run(command + ['--print-execution-budget'],
                                   text=True, capture_output=True, timeout=20, check=True)
    budget = json.loads(budget_result.stdout)
    result = subprocess.run(
        command, text=True, capture_output=True, env=child_env,
        timeout=budget['wrapper_timeout_seconds'],
    )
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(run_locked(main))
