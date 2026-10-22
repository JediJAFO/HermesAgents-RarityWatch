"""Windows-safe no-agent wrapper with delivery-acknowledged WhatsApp outbox."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from exotic_execution_lock import BUSY, run_locked

HOME = Path(__file__).resolve().parents[1]
STATE = HOME / "price-watches" / "mcfarlane-exotics.json"
LOCK = HOME / "price-watches" / ".exotic-execution-lock"
LEDGER = HOME / "cron" / "executions.db"
NODE = HOME / "node" / "node.exe"
DELIVERY = HOME / "scripts" / "run-mcfarlane-exotic-whatsapp.js"
JOB_ID = "__PRIVATE_JOB_ID__"


def atomic_write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".whatsapp-ack.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _delivery_row(ledger: Path, execution_id: str) -> dict | None:
    try:
        with closing(sqlite3.connect(Path(ledger))) as connection:
            connection.row_factory = sqlite3.Row
            columns = {row[1] for row in connection.execute("PRAGMA table_info(executions)")}
            handoff = ", handoff_pending" if "handoff_pending" in columns else ""
            row = connection.execute(
                f"SELECT status, delivery_outcome{handoff} FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
        result = dict(row) if row else None
        if result is not None:
            result.setdefault("handoff_pending", 0)
        return result
    except sqlite3.Error:
        return None


def reconcile_previous_delivery(state_path: Path = STATE, ledger: Path = LEDGER,
                                lock_path: Path = LOCK) -> str:
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    pending = state.get("pending_whatsapp_change")
    execution_id = pending.get("emitted_execution_id") if isinstance(pending, dict) else None
    if not execution_id:
        return "none"
    row = _delivery_row(ledger, execution_id)
    if (not row or row.get("handoff_pending")
            or row.get("status") in {"claimed", "running", "started"}):
        return "waiting"

    delivered = row.get("delivery_outcome") == "delivered"

    def update() -> int:
        current = json.loads(Path(state_path).read_text(encoding="utf-8"))
        queued = current.get("pending_whatsapp_change")
        if not isinstance(queued, dict) or queued.get("emitted_execution_id") != execution_id:
            return 0
        if delivered:
            current["last_whatsapp_delivered_change_fingerprint"] = queued.get("fingerprint")
            current["last_whatsapp_delivery_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            waiting = current.pop("queued_whatsapp_change", None)
            if isinstance(waiting, dict) and isinstance(waiting.get("events"), list) and waiting["events"]:
                current["pending_whatsapp_change"] = waiting
            else:
                del current["pending_whatsapp_change"]
        else:
            waiting = current.pop("queued_whatsapp_change", None)
            queued.pop("emitted_execution_id", None)
            queued.pop("emitted_at", None)
            if isinstance(waiting, dict) and isinstance(waiting.get("events"), list):
                queued["events"] = [*(queued.get("events") or []), *waiting["events"]]
                queued["at"] = waiting.get("at", queued.get("at"))
                queued["fingerprint"] = json.dumps(queued["events"], separators=(",", ":"))
        atomic_write(Path(state_path), current)
        return 0

    result = run_locked(update, lock_path)
    if result == BUSY:
        return "busy"
    return "delivered" if delivered else "retry"


def current_execution_id(env=None, ledger: Path = LEDGER) -> str | None:
    env = os.environ if env is None else env
    explicit = env.get("MCFARLANE_WHATSAPP_EXECUTION_ID")
    if explicit:
        return explicit
    try:
        import psutil
        ancestors = {os.getppid(), *(process.pid for process in psutil.Process().parents())}
        with closing(sqlite3.connect(Path(ledger))) as connection:
            rows = connection.execute(
                "SELECT id, pid FROM executions WHERE job_id=? AND status='running'", (JOB_ID,)
            ).fetchall()
        owned = [execution_id for execution_id, pid in rows if pid in ancestors]
        return owned[0] if len(owned) == 1 else None
    except (OSError, sqlite3.Error, ImportError):
        return None


def main() -> int:
    reconciliation = reconcile_previous_delivery()
    if reconciliation in {"waiting", "busy"}:
        sys.stdout.write("[SILENT]")
        return 0

    state = json.loads(STATE.read_text(encoding="utf-8"))
    pending = state.get("pending_whatsapp_change")
    child_env = dict(os.environ)
    if pending:
        execution_id = current_execution_id()
        if not execution_id:
            sys.stderr.write("WhatsApp execution identity unavailable; queued alert retained.\n")
            return 1
        child_env["MCFARLANE_WHATSAPP_EXECUTION_ID"] = execution_id

    result = subprocess.run(
        [str(NODE), str(DELIVERY)], text=True, capture_output=True, timeout=60, env=child_env,
    )
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
