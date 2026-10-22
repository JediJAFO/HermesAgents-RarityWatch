"""Saved-state-gated Exotic retry runner with bounded backoff.

Runs only unresolved technical monitor failures. Candidate listing changes are excluded:
they have their separate confirmation policy and are not failed updates.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from exotic_execution_lock import run_locked, BUSY
from recover_isolated_monitor_chrome import recover_isolated_browser

STATE = Path(r"__HERMES_HOME__/price-watches/mcfarlane-exotics.json")
COLLECTOR = Path(r"__HERMES_HOME__/scripts/run_mcfarlane_exotic_deterministic.py")
DEFERRED_PRIMARY = Path(r"__HERMES_HOME__/price-watches/mcfarlane-deferred-primary.json")
DELAYS_MINUTES = (1, 3, 5, 10, 20)
RETRY_KEY = "automatic_exotic_retry_state"
CANDIDATE_MARKER = "candidate listing change held for next-interval confirmation"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write(state: dict) -> None:
    temporary = STATE.with_suffix(".retry.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE)


def _read_deferred_primary(path: Path | None = None) -> dict | None:
    path = DEFERRED_PRIMARY if path is None else Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}


def run_deferred_primary() -> int:
    marker = _read_deferred_primary()
    if marker is None:
        return 0
    generation = marker.get('generation')
    occurrence = marker.get('occurrence')
    environment = os.environ.copy()
    if isinstance(occurrence, dict):
        environment['MCFARLANE_DEFERRED_PRIMARY_OCCURRENCE'] = json.dumps(occurrence, separators=(',', ':'))
    result = subprocess.run([sys.executable, str(COLLECTOR)], env=environment, check=False)
    if result.returncode != BUSY:
        current = _read_deferred_primary()
        if current is not None and ((generation and current.get('generation') == generation)
                                    or (not generation and current == marker)):
            DEFERRED_PRIMARY.unlink(missing_ok=True)
    return result.returncode


def retryable_names(outcome: dict) -> list[str]:
    names: list[str] = []
    skipped: set[str] = set()
    for failure in outcome.get("failed_collections") or []:
        if not isinstance(failure, dict):
            continue
        name, reason = failure.get("name"), str(failure.get("type") or "")
        if isinstance(name, str) and name and CANDIDATE_MARKER not in reason:
            names.append(name)
            if 'batch time budget reached before collection check' in reason:
                skipped.add(name)
    return sorted(dict.fromkeys(names), key=lambda name: name not in skipped)


def retryable_keys(outcome: dict) -> list[str]:
    keys: list[str] = []
    skipped: set[str] = set()
    for failure in outcome.get("failed_collections") or []:
        if not isinstance(failure, dict):
            continue
        key, reason = failure.get("watch_key"), str(failure.get("type") or "")
        if isinstance(key, str) and key and CANDIDATE_MARKER not in reason:
            keys.append(key)
            if 'batch time budget reached before collection check' in reason:
                skipped.add(key)
    return sorted(dict.fromkeys(keys), key=lambda key: key not in skipped)


def shared_certificate_failure(state: dict) -> bool:
    if state.get('shared_certificate_failure'):
        return True
    outcome = state.get('last_monitor_outcome') or {}
    if outcome.get('shared_failure') == 'certificate_authority_invalid':
        return True
    # Recognize an already-saved pre-upgrade full outage without rewriting state.
    failures = outcome.get('failed_collections') or []
    return (outcome.get('scope') == 'full' and not outcome.get('complete')
            and bool(failures) and all('ERR_CERT_AUTHORITY_INVALID' in str(x.get('type', ''))
                                      for x in failures))


def main() -> int:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    if shared_certificate_failure(state):
        return 0  # Only a new primary interval probes this shared trust failure.
    outcome = state.get("last_monitor_outcome")
    retry = state.get(RETRY_KEY)
    primary_id = state.get("last_primary_exotic_run_id")
    if (isinstance(retry, dict) and retry.get("exhausted")
            and retry.get("primary_run_id") == primary_id):
        return 0
    if not isinstance(outcome, dict) or outcome.get("complete"):
        if state.pop(RETRY_KEY, None) is not None:
            atomic_write(state)
        return 0
    keys = retryable_keys(outcome)
    names = retryable_names(outcome)
    if not names:
        # A targeted/manual recovery may have cleared the technical failures
        # while a candidate listing remains held for its separate confirmation.
        # Remove any stale retry budget so it cannot be reported as unresolved.
        if state.pop(RETRY_KEY, None) is not None:
            atomic_write(state)
        return 0
    now = utc_now()
    retry = state.get(RETRY_KEY)
    # Only a genuinely new primary run can replenish the bounded budget.
    primary_id = state.get("last_primary_exotic_run_id")
    new_primary = (primary_id is not None and isinstance(retry, dict)
                   and retry.get("primary_run_id") != primary_id)
    if not isinstance(retry, dict) or new_primary:
        state[RETRY_KEY] = {
            "primary_run_id": primary_id,
            "outcome_at": outcome.get("at"),
            "names": names,
            "keys": keys,
            "attempts_started": 0,
            "next_retry_at": iso(now + timedelta(minutes=DELAYS_MINUTES[0])),
        }
        atomic_write(state)
        return 0
    due = parse_time(retry.get("next_retry_at"))
    attempts = int(retry.get("attempts_started") or 0)
    if attempts >= len(DELAYS_MINUTES) or due is None or now < due:
        return 0
    environment = os.environ.copy()
    environment["MCFARLANE_RUN_KIND"] = "retry"
    # Re-evaluate unresolved work: another targeted pass may have changed it.
    if keys:
        environment["MCFARLANE_WATCH_KEYS"] = "|,|".join(keys)
    else:
        environment["MCFARLANE_COLLECTION_NAMES"] = "|".join(names)
    if outcome.get("shared_failure") == "local_browser_cdp_timeout":
        try:
            recover_isolated_browser()
        except (OSError, RuntimeError):
            return 1
    result = subprocess.run([sys.executable, str(COLLECTOR)], env=environment, check=False)
    if result.returncode == BUSY:
        return BUSY
    # The targeted collector updates last_monitor_outcome. Schedule only another
    # retry if that new outcome still has technical failures.
    updated = json.loads(STATE.read_text(encoding="utf-8"))
    updated_outcome = updated.get("last_monitor_outcome")
    next_names = retryable_names(updated_outcome) if isinstance(updated_outcome, dict) and not updated_outcome.get("complete") else []
    next_keys = retryable_keys(updated_outcome) if isinstance(updated_outcome, dict) and not updated_outcome.get("complete") else []
    used = attempts + 1
    if not next_names:
        updated.pop(RETRY_KEY, None)
    else:
        updated[RETRY_KEY] = {
            "primary_run_id": primary_id,
            "outcome_at": updated_outcome.get("at"),
            "names": next_names,
            "keys": next_keys,
            "attempts_started": used,
            "next_retry_at": iso(utc_now() + timedelta(minutes=DELAYS_MINUTES[used])) if used < len(DELAYS_MINUTES) else None,
            "exhausted": used >= len(DELAYS_MINUTES),
        }
    atomic_write(updated)
    return result.returncode


def retry_work_due() -> bool:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    if shared_certificate_failure(state):
        return False
    outcome = state.get("last_monitor_outcome")
    retry = state.get(RETRY_KEY)
    primary_id = state.get("last_primary_exotic_run_id")
    if (isinstance(retry, dict) and retry.get("exhausted")
            and retry.get("primary_run_id") == primary_id):
        return False
    if not isinstance(outcome, dict) or outcome.get("complete"):
        return retry is not None
    if not retryable_names(outcome):
        return retry is not None
    new_primary = (primary_id is not None and isinstance(retry, dict)
                   and retry.get("primary_run_id") != primary_id)
    if not isinstance(retry, dict) or new_primary:
        return True
    due = parse_time(retry.get("next_retry_at"))
    attempts = int(retry.get("attempts_started") or 0)
    return attempts < len(DELAYS_MINUTES) and due is not None and utc_now() >= due


def cron_entry() -> int:
    # A scheduled full scan that encountered a live lease takes precedence over
    # targeted technical retries. The marker remains until a real pass starts.
    if _read_deferred_primary() is not None:
        result = run_locked(run_deferred_primary)
        return 0 if result == BUSY else result
    # The once-per-minute scheduler should not create and remove the shared
    # marketplace lease unless retry state actually needs inspection/mutation.
    if not retry_work_due():
        return 0
    # BUSY is intentional deferral, not a cron failure. Keep it distinct inside
    # the orchestrator so skipped work never consumes the retry budget.
    result = run_locked(main)
    return 0 if result == BUSY else result


if __name__ == "__main__":
    raise SystemExit(cron_entry())
