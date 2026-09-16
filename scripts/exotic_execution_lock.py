"""Atomic cross-language execution lease. Busy is silent exit 75, never a run.

The lease spans collector AND orchestrator state writes. A hard-killed owner leaves
its lease fail-closed: inspect owner.json/processes before manually removing it.
Never expire a lease by age: a slow collector may still own its browser page.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets

LOCK = Path('__HERMES_HOME__/price-watches/.exotic-execution-lock')
TOKEN_ENV = 'MCFARLANE_EXOTIC_LOCK_TOKEN'
BUSY = 75

@contextmanager
def execution_lock(lock_path=None):
    lock_path = Path(lock_path) if lock_path is not None else LOCK
    token = os.environ.get(TOKEN_ENV)
    try:
        owner = json.loads((lock_path / 'owner.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        owner = {}
    if token and owner.get('token') == token:
        yield True
        return
    try:
        lock_path.mkdir()
    except FileExistsError:
        yield False
        return
    token = secrets.token_hex(24)
    previous = os.environ.get(TOKEN_ENV)
    try:
        (lock_path / 'owner.json').write_text(json.dumps({'pid': os.getpid(), 'token': token}), encoding='utf-8')
        os.environ[TOKEN_ENV] = token
        yield True
    finally:
        if previous is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = previous
        (lock_path / 'owner.json').unlink(missing_ok=True)
        lock_path.rmdir()

def run_locked(action, lock_path=None):
    with execution_lock(lock_path) as acquired:
        return action() if acquired else BUSY
