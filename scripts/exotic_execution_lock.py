"""Atomic cross-language execution lease. Busy is silent exit 75, never a run.

The lease spans collector AND orchestrator state writes. An unchanged lease whose
recorded process is conclusively dead is reclaimed automatically; live or
ambiguous owners remain fail-closed. Lease age alone never expires a live owner.
"""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import secrets
import time

LOCK = Path('__HERMES_HOME__/price-watches/.exotic-execution-lock')
TOKEN_ENV = 'MCFARLANE_EXOTIC_LOCK_TOKEN'
BUSY = 75
EMPTY_LOCK_GRACE_SECONDS = 120

def _pid_is_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return True
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        process = kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return ctypes.get_last_error() != 87  # Only invalid PID proves death.
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False

def _retry_windows_cleanup(action):
    for attempt in range(6):
        try:
            action()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            transient = isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EBUSY, errno.EPERM} or getattr(exc, 'winerror', None) in {5, 32}
            if not transient or attempt >= 5:
                raise
            time.sleep(0.025 * (attempt + 1))

def _quarantine_lock(lock_path, expected_owner=None, require_empty=False):
    lock_path = Path(lock_path)
    owner_path = lock_path / 'owner.json'
    mutation = lock_path / '.owner-mutation'
    try:
        mutation.mkdir()
    except OSError:
        return False
    moved = False
    quarantine = lock_path.with_name(lock_path.name + '.reclaim-' + secrets.token_hex(8))
    try:
        if expected_owner is not None:
            try:
                if owner_path.read_text(encoding='utf-8') != expected_owner:
                    return False
            except OSError:
                return False
        if require_empty and any(entry.name != mutation.name for entry in lock_path.iterdir()):
            return False
        try:
            os.replace(lock_path, quarantine)
            moved = True
        except OSError:
            return False
    finally:
        if not moved:
            try:
                mutation.rmdir()
            except OSError:
                pass
    try:
        _retry_windows_cleanup(lambda: (quarantine / 'owner.json').unlink(missing_ok=True))
        _retry_windows_cleanup(lambda: (quarantine / mutation.name).rmdir())
        _retry_windows_cleanup(quarantine.rmdir)
    except OSError:
        pass  # Quarantine debris cannot block a new shared lease.
    return True

def _reclaim_old_empty_lock(lock_path):
    lock_path = Path(lock_path)
    try:
        snapshot = lock_path.stat()
        if time.time() - snapshot.st_mtime < EMPTY_LOCK_GRACE_SECONDS:
            return False
        if any(lock_path.iterdir()):
            return False
        if lock_path.stat().st_mtime_ns != snapshot.st_mtime_ns:
            return False
        return _quarantine_lock(lock_path, require_empty=True)
    except OSError:
        return False

def reclaim_dead_lock(lock_path):
    """Remove an unchanged lease only when its recorded process is dead."""
    owner_path = Path(lock_path) / 'owner.json'
    try:
        original = owner_path.read_text(encoding='utf-8')
        owner = json.loads(original)
    except FileNotFoundError:
        return _reclaim_old_empty_lock(lock_path)
    except (OSError, ValueError):
        return False
    if _pid_is_alive(owner.get('pid')):
        return False
    if owner.get('active_pid') and _pid_is_alive(owner.get('active_pid')):
        return False
    acquired_at = owner.get('acquired_at')
    if isinstance(acquired_at, (int, float)) and time.time() - acquired_at < EMPTY_LOCK_GRACE_SECONDS:
        return False
    return _quarantine_lock(lock_path, original)

def _release_owned_lock(lock_path, token):
    owner_path = Path(lock_path) / 'owner.json'
    try:
        owner = json.loads(owner_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    if owner.get('token') != token:
        return False
    _retry_windows_cleanup(lambda: owner_path.unlink(missing_ok=True))
    _retry_windows_cleanup(Path(lock_path).rmdir)
    return True

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
        if not reclaim_dead_lock(lock_path):
            yield False
            return
        try:
            lock_path.mkdir()
        except FileExistsError:
            yield False
            return
    token = secrets.token_hex(24)
    previous = os.environ.get(TOKEN_ENV)
    try:
        (lock_path / 'owner.json').write_text(json.dumps({
            'pid': os.getpid(), 'token': token, 'acquired_at': time.time(),
        }), encoding='utf-8')
        os.environ[TOKEN_ENV] = token
        yield True
    finally:
        if previous is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = previous
        _release_owned_lock(lock_path, token)

def run_locked(action, lock_path=None):
    with execution_lock(lock_path) as acquired:
        return action() if acquired else BUSY
