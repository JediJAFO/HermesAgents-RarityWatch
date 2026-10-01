"""Zero-LLM manual full rarity update plus verified Discord heartbeat."""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Callable, Any
import uuid

HOME = Path(__file__).resolve().parents[1]
STATE = HOME / "price-watches" / "mcfarlane-exotics.json"
STATUS = HOME / "price-watches" / "rarity-manual-update-status.json"
LAUNCH_LOCK = HOME / "price-watches" / ".rarity-manual-update.lock"
WRAPPER = HOME / "scripts" / "run_mcfarlane_exotic_deterministic.py"
NODE = HOME / "node" / "node.exe"
COLLECTOR = HOME / "price-watches" / "run_mcfarlane_exotic_deterministic.js"
DELIVERY = HOME / "price-watches" / "rarity_watch_discord_delivery.py"
BUSY = 75


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def default_wrapper_run() -> tuple[int, str, str]:
    result = subprocess.run([sys.executable, str(WRAPPER)], cwd=str(HOME / "scripts"),
                            text=True, capture_output=True, timeout=1900, check=False)
    return result.returncode, result.stdout, result.stderr


def default_heartbeat_render() -> str:
    result = subprocess.run([str(NODE), str(COLLECTOR), "--print-saved-heartbeat"],
                            text=True, capture_output=True, timeout=30, check=True)
    return result.stdout


def default_deliver(text: str, batch_id: str) -> list[str]:
    spec = importlib.util.spec_from_file_location("manual_rarity_discord", DELIVERY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Discord().deliver(text, batch_id)


def default_priority_keys(state: dict[str, Any]) -> list[str]:
    module_path = HOME / "scripts" / "confirm_priority_exotic_listing_candidates.py"
    spec = importlib.util.spec_from_file_location("manual_rarity_priority", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    candidates = state.get("pending_exotic_change_candidates") or {}
    keys: list[str] = []
    for collection in state.get("collections") or []:
        identity = collection.get("watch_key") or collection.get("name")
        if identity in candidates and module.is_priority_listing_candidate(collection, candidates[identity]):
            keys.append(identity)
    return sorted(keys)


def default_confirmation_run(keys: list[str]) -> tuple[int, str, str]:
    # The saved state is committed after the collector's final marketplace
    # observation. Waiting from its mtime guarantees the user's 10-second gap.
    remaining = 10.0 - max(0.0, time.time() - STATE.stat().st_mtime)
    if remaining > 0:
        time.sleep(remaining)
    environment = os.environ.copy()
    environment["MCFARLANE_RUN_KIND"] = "confirmation"
    environment["MCFARLANE_WATCH_KEYS"] = "|,|".join(keys)
    result = subprocess.run([sys.executable, str(WRAPPER)], cwd=str(HOME / "scripts"),
                            env=environment, text=True, capture_output=True,
                            timeout=1900, check=False)
    return result.returncode, result.stdout, result.stderr


def run(*, mode: str = "update", state_path: Path = STATE, status_path: Path = STATUS, launch_lock: Path = LAUNCH_LOCK,
        wrapper_run: Callable[[], tuple[int, str, str]] = default_wrapper_run,
        confirmation_run: Callable[[list[str]], tuple[int, str, str]] = default_confirmation_run,
        priority_keys_fn: Callable[[dict[str, Any]], list[str]] = default_priority_keys,
        auto_confirmation_delay: float = 10.0, sleep_fn: Callable[[float], None] = time.sleep,
        heartbeat_render: Callable[[], str] = default_heartbeat_render,
        deliver: Callable[[str, str], list[str]] = default_deliver) -> dict[str, Any]:
    run_id = uuid.uuid4().hex
    status: dict[str, Any] = {"status": "running", "mode": mode, "run_id": run_id, "started_at": now(),
                              "llm_tokens": 0, "heartbeat_sent": False}
    atomic_json(status_path, status)
    try:
        if mode == "confirm_pending":
            initial_state = json.loads(Path(state_path).read_text(encoding="utf-8"))
            keys = sorted((initial_state.get("pending_exotic_change_candidates") or {}).keys())
            if not keys:
                status.update(status="no_pending", finished_at=now(),
                              message="No pending listing candidates; no marketplace request was made.")
                atomic_json(status_path, status)
                return status
            status.update(status="confirming", pending_count=len(keys))
            atomic_json(status_path, status)
            returncode, stdout, stderr = confirmation_run(keys)
        elif mode == "update":
            returncode, stdout, stderr = wrapper_run()
        else:
            raise ValueError(f"unsupported mode: {mode}")
        if returncode == BUSY:
            status.update(status="busy", finished_at=now(), message="Another rarity monitor operation owns the shared lease.")
            atomic_json(status_path, status)
            return status
        if returncode != 0:
            status.update(status="failed", finished_at=now(),
                          message=(stderr.strip() or f"Collector exited {returncode}")[:500])
            atomic_json(status_path, status)
            return status

        regular_ids: list[str] = []
        if stdout.strip():
            label = "confirmation" if mode == "confirm_pending" else "regular"
            regular_ids = deliver(stdout, f"manual-rarity-{label}-" + run_id)
            if not regular_ids:
                raise RuntimeError("regular Discord result was not verified")
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        outcome = state.get("last_monitor_outcome") or {}
        status["regular_result_sent"] = bool(regular_ids)
        status["regular_message_count"] = len(regular_ids)
        status["outcome_at"] = outcome.get("at")
        status["unresolved_count"] = len(outcome.get("failed_collections") or [])
        if mode == "update" and not outcome.get("complete"):
            priority_keys = priority_keys_fn(state)
            if priority_keys:
                status.update(status="waiting_confirmation", automatic_confirmation=True,
                              confirmation_due_seconds=auto_confirmation_delay,
                              priority_candidate_count=len(priority_keys),
                              message="Purchase-priority candidates queued for an independent targeted confirmation.")
                atomic_json(status_path, status)
                sleep_fn(auto_confirmation_delay)
                latest_state = json.loads(Path(state_path).read_text(encoding="utf-8"))
                latest_candidates = latest_state.get("pending_exotic_change_candidates") or {}
                priority_keys = [key for key in priority_keys if key in latest_candidates]
                if priority_keys:
                    returncode, confirm_stdout, confirm_stderr = confirmation_run(priority_keys)
                    if returncode == BUSY:
                        status.update(status="partial", finished_at=now(),
                                      message="Automatic confirmation deferred because another monitor operation owns the shared lease.")
                        atomic_json(status_path, status)
                        return status
                    if returncode != 0:
                        status.update(status="partial", finished_at=now(),
                                      message=(confirm_stderr.strip() or f"Confirmation exited {returncode}")[:500])
                        atomic_json(status_path, status)
                        return status
                    if confirm_stdout.strip():
                        confirmation_ids = deliver(confirm_stdout, "manual-rarity-confirmation-" + run_id)
                        if not confirmation_ids:
                            raise RuntimeError("confirmation Discord result was not verified")
                        status["confirmation_message_count"] = len(confirmation_ids)
                state = json.loads(Path(state_path).read_text(encoding="utf-8"))
                outcome = state.get("last_monitor_outcome") or {}
                status["outcome_at"] = outcome.get("at")
                status["unresolved_count"] = len(outcome.get("failed_collections") or [])
        if not outcome.get("complete"):
            status.update(status="partial", finished_at=now(),
                          message="Result delivered; heartbeat withheld until every watch is resolved.")
            atomic_json(status_path, status)
            return status

        heartbeat = heartbeat_render()
        heartbeat_ids = deliver(heartbeat, "manual-rarity-heartbeat-" + run_id)
        if not heartbeat_ids:
            raise RuntimeError("heartbeat Discord result was not verified")
        status.update(status="complete", finished_at=now(), heartbeat_sent=True,
                      heartbeat_message_count=len(heartbeat_ids),
                      message="Full update and refreshed heartbeat delivered and verified.")
        atomic_json(status_path, status)
        return status
    except Exception as exc:
        status.update(status="failed", finished_at=now(), message=str(exc)[:500])
        atomic_json(status_path, status)
        return status
    finally:
        Path(launch_lock).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-pending", action="store_true")
    args = parser.parse_args()
    result = run(mode="confirm_pending" if args.confirm_pending else "update")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"complete", "partial", "busy", "no_pending"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
