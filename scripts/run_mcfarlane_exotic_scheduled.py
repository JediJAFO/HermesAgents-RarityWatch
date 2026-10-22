"""Cron-only entrypoint with a durable deferred-primary obligation."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets

from exotic_execution_lock import BUSY, run_locked
from run_mcfarlane_exotic_deterministic import main as collector_main, scheduled_heartbeat_context

HOME = Path(__file__).resolve().parents[1]
DEFERRED_PRIMARY = HOME / 'price-watches' / 'mcfarlane-deferred-primary.json'


def _is_9am_occurrence(occurrence: object) -> bool:
    if not isinstance(occurrence, dict):
        return False
    try:
        from zoneinfo import ZoneInfo
        instant = datetime.fromisoformat(str(occurrence['scheduled_at']).replace('Z', '+00:00'))
        local = instant.astimezone(ZoneInfo('America/New_York'))
        return (local.hour, local.minute, local.second) == (9, 0, 0)
    except (KeyError, TypeError, ValueError):
        return False


def _read_marker(path: Path | None = None) -> dict:
    path = DEFERRED_PRIMARY if path is None else Path(path)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_deferred(occurrence: dict | None, path: Path | None = None) -> str:
    path = DEFERRED_PRIMARY if path is None else Path(path)
    existing = _read_marker(path)
    existing_occurrence = existing.get('occurrence')
    # Preserve a missed 9 AM occurrence if a later scheduled scan is also busy;
    # the eventual full scan can then fulfill that day's heartbeat obligation.
    chosen = existing_occurrence if _is_9am_occurrence(existing_occurrence) and not _is_9am_occurrence(occurrence) else occurrence
    if chosen is None:
        chosen = existing_occurrence
    generation = secrets.token_hex(16)
    value = {
        'generation': generation,
        'requested_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'occurrence': chosen,
    }
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)
    return generation


def _clear_unchanged_deferred(marker: dict, path: Path | None = None) -> None:
    path = DEFERRED_PRIMARY if path is None else Path(path)
    if not marker:
        return
    current = _read_marker(path)
    if current == marker:
        path.unlink(missing_ok=True)


def main() -> int:
    previous = os.environ.get("HERMES_CRON_SESSION")
    os.environ["HERMES_CRON_SESSION"] = "1"
    try:
        occurrence = scheduled_heartbeat_context()
        prior_marker = _read_marker()
        result = run_locked(collector_main)
        if result == BUSY:
            _write_deferred(occurrence)
            return 0
        _clear_unchanged_deferred(prior_marker)
        return result
    finally:
        if previous is None:
            os.environ.pop("HERMES_CRON_SESSION", None)
        else:
            os.environ["HERMES_CRON_SESSION"] = previous


if __name__ == "__main__":
    raise SystemExit(main())
