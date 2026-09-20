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

STATE = Path(r"__HERMES_HOME__/price-watches/mcfarlane-exotics.json")
COLLECTOR = Path(r"__HERMES_HOME__/scripts/run_mcfarlane_exotic_deterministic.py")
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
    environment["MCFARLANE_COLLECTION_NAMES"] = "|".join(names)
    result = subprocess.run([sys.executable, str(COLLECTOR)], env=environment, check=False)
    if result.returncode == BUSY:
        return BUSY
    # The targeted collector updates last_monitor_outcome. Schedule only another
    # retry if that new outcome still has technical failures.
    updated = json.loads(STATE.read_text(encoding="utf-8"))
    updated_outcome = updated.get("last_monitor_outcome")
    next_names = retryable_names(updated_outcome) if isinstance(updated_outcome, dict) and not updated_outcome.get("complete") else []
    used = attempts + 1
    if not next_names:
        updated.pop(RETRY_KEY, None)
    else:
        updated[RETRY_KEY] = {
            "primary_run_id": primary_id,
            "outcome_at": updated_outcome.get("at"),
            "names": next_names,
            "attempts_started": used,
            "next_retry_at": iso(utc_now() + timedelta(minutes=DELAYS_MINUTES[used])) if used < len(DELAYS_MINUTES) else None,
            "exhausted": used >= len(DELAYS_MINUTES),
        }
    atomic_write(updated)
    return result.returncode


def cron_entry() -> int:
    # BUSY is intentional deferral, not a cron failure. Keep it distinct inside
    # the orchestrator so skipped work never consumes the retry budget.
    result = run_locked(main)
    return 0 if result == BUSY else result


if __name__ == "__main__":
    raise SystemExit(cron_entry())
