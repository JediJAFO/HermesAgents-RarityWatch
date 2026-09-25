"""Deterministic saved-state manager for Exotic/Legendary watches.

Every operation is local Python/Node code. No LLM or agent turn is involved.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.request import Request, urlopen
import uuid

HOME = Path(__file__).resolve().parents[1]
STATE = HOME / "price-watches" / "mcfarlane-exotics.json"
CATALOG = HOME / "price-watches" / "mcfarlane-dc-sales.json"
NODE = HOME / "node" / "node.exe"
COLLECTOR = HOME / "price-watches" / "run_mcfarlane_exotic_deterministic.js"
LOCK = HOME / "price-watches" / ".exotic-execution-lock"
RARITIES = ("Exotic", "Legendary")
CONTRACT_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def watch_key(contract: str, rarity: str) -> str:
    return f"{contract.lower()}|{rarity}"


def migrated_state(raw: dict[str, Any]) -> dict[str, Any]:
    state = dict(raw)
    migrated = []
    for original in raw.get("collections", []):
        item = dict(original)
        item.setdefault("rarity", "Exotic")
        item.setdefault("enabled", True)
        item["contract"] = str(item["contract"]).lower()
        item["watch_key"] = watch_key(item["contract"], item["rarity"])
        migrated.append(item)
    state["collections"] = migrated
    candidates = raw.get("pending_exotic_change_candidates") or {}
    migrated_candidates = {}
    for identity, fingerprint in candidates.items():
        if "|" in identity:
            migrated_candidates[identity] = fingerprint
            continue
        matches = [item for item in migrated if item.get("name") == identity]
        target = next((item for item in matches if item["rarity"] == "Exotic"), matches[0] if len(matches) == 1 else None)
        migrated_candidates[target["watch_key"] if target else identity] = fingerprint
    state["pending_exotic_change_candidates"] = migrated_candidates
    state["schema_version"] = max(2, int(state.get("schema_version", 1)))
    return state


def _validate(contract: Any, rarity: Any) -> tuple[str, str]:
    if not isinstance(contract, str) or not CONTRACT_RE.fullmatch(contract):
        raise ValueError("Polygon contract must be 0x followed by exactly 40 hexadecimal characters")
    if rarity not in RARITIES:
        raise ValueError("rarity must be exactly Exotic or Legendary")
    return contract.lower(), rarity


def _source_url(contract: str, rarity: str) -> str:
    return (f"https://mcfarlanetoys.digital/explore/POLYGON:{contract}/"
            f"?sort=cheapest&inStockOnly=true&traits%5BRarity%5D%5B0%5D={rarity}")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _lock_context(lock_path: Path):
    import importlib.util
    module_path = Path(__file__).with_name("exotic_execution_lock.py")
    spec = importlib.util.spec_from_file_location("rarity_watch_execution_lock", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.execution_lock(lock_path)


def _default_metadata(contract: str) -> dict[str, Any]:
    try:
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        exact = next((c for c in catalog.get("collections", [])
                      if str(c.get("contract", "")).lower() == contract), None)
        if exact and exact.get("name"):
            return {"name": exact["name"], "metadata_source": "exact local MTD catalog"}
    except (OSError, ValueError):
        pass
    url = f"https://api.rarible.org/v0.1/collections/POLYGON:{contract}"
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (Hermes rarity watch manager)"}
    if os.environ.get("RARIBLE_API_KEY"):
        headers["X-API-KEY"] = os.environ["RARIBLE_API_KEY"]
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        payload = json.load(response)
    name = (payload.get("meta") or {}).get("name") or payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuntimeError("exact collection metadata did not return a display name")
    return {"name": name.strip(), "metadata_source": "Rarible exact collection metadata"}


def _default_baseline(entry: dict[str, Any]) -> dict[str, Any]:
    env = dict(os.environ, MCFARLANE_ONBOARD_ENTRY=json.dumps(entry, separators=(",", ":")))
    completed = subprocess.run(
        [str(NODE), str(COLLECTOR), "--observe-onboarding-entry"], env=env,
        text=True, capture_output=True, timeout=420, check=False)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "onboarding collector failed").strip()[:500])
    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError("onboarding collector returned invalid JSON") from exc


def _saved_list(state: dict[str, Any]) -> dict[str, Any]:
    watches = [{
        "watch_key": item["watch_key"], "contract": item["contract"],
        "rarity": item["rarity"], "name": item.get("name") or "Pending name lookup",
        "category": item.get("category"), "enabled": item["enabled"],
        "baseline_status": item.get("baseline_status", "verified" if item.get("baseline") else "unverified"),
    } for item in state["collections"]]
    return {"ok": True, "action": "list", "count": len(watches), "watches": watches,
            "network_calls": 0, "notifications": 0}


def _seller_label(listing: dict[str, Any]) -> str:
    tag = str(listing.get("seller_name_tag") or "").strip()
    if tag and not re.fullmatch(r"0x[0-9a-fA-F]{40}", tag):
        return tag
    wallet = str(listing.get("seller_wallet") or "").strip()
    return "..." + wallet[-4:] if wallet else "Seller not recorded"


def _price_label(listing: dict[str, Any], state: dict[str, Any]) -> str:
    value = listing.get("price")
    try:
        number = float(value)
        amount = f"{number:,.0f}" if number.is_integer() else f"{number:,.8f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        amount = str(value or "price unavailable")
        number = None
    currency = "POL" if listing.get("currency") in {"POLYGON", "MATIC", "POL"} else str(listing.get("currency") or "")
    rate = (state.get("current_pol_usd") or {}).get("rate")
    try:
        usd = f" (${number * float(rate):,.2f})" if number is not None and currency == "POL" and float(rate) > 0 else ""
    except (TypeError, ValueError):
        usd = ""
    return f"{amount} {currency}{usd}".strip()


def _onboarding_report(entry: dict[str, Any], state: dict[str, Any]) -> str:
    rarity, name = entry["rarity"], entry["name"]
    baseline = entry["baseline"]
    listings = baseline.get("listings") or []
    lines = [f"**Rarity watch added — [{rarity}] {name}**"]
    if not baseline.get("for_sale") or not listings:
        lines.append(f"No active [{rarity}] BUY NOW listings verified.")
    else:
        count = len(listings)
        lines.append(f"{count} active BUY NOW listing{'s' if count != 1 else ''} verified:")
        lines.extend(f"• {_price_label(item, state)} — {_seller_label(item)}" for item in listings)
    lines.append("Included in subsequent interval checks and the 8AM ET heartbeat.")
    return "\r\n".join(lines) + "\r\n"


def _default_notice_deliverer(text: str, batch_id: str) -> list[str]:
    module_path = HOME / "price-watches" / "rarity_watch_discord_delivery.py"
    spec = importlib.util.spec_from_file_location("rarity_watch_discord_delivery", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Discord().deliver(text, batch_id)


def execute(request: dict[str, Any], *, state_path: Path = STATE, lock_path: Path = LOCK,
            metadata_resolver: Callable[[str], dict[str, Any]] | None = None,
            baseline_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
            notice_deliverer: Callable[[str, str], list[str]] | None = None) -> dict[str, Any]:
    action = request.get("action")
    if action == "list":
        return _saved_list(migrated_state(json.loads(Path(state_path).read_text(encoding="utf-8"))))
    if action not in {"add", "disable", "remove"}:
        raise ValueError(f"unsupported action: {action}")
    contract, rarity = _validate(request.get("contract"), request.get("rarity"))
    key = watch_key(contract, rarity)
    metadata_resolver = metadata_resolver or _default_metadata
    baseline_runner = baseline_runner or _default_baseline
    notice_deliverer = notice_deliverer or _default_notice_deliverer
    notice = None
    with _lock_context(Path(lock_path)) as acquired:
        if not acquired:
            raise RuntimeError("rarity watch is busy; no state changed")
        state = migrated_state(json.loads(Path(state_path).read_text(encoding="utf-8")))
        matches = [item for item in state["collections"] if item["watch_key"] == key]
        if action in {"disable", "remove"}:
            if not matches:
                raise ValueError(f"watch not saved: {key}")
            if action == "disable":
                matches[0]["enabled"] = False
            else:
                state["collections"] = [item for item in state["collections"] if item["watch_key"] != key]
            candidates = state.get("pending_exotic_change_candidates")
            if isinstance(candidates, dict):
                candidates.pop(key, None)
            retry = state.get("automatic_exotic_retry_state")
            if isinstance(retry, dict):
                if isinstance(retry.get("keys"), list):
                    retry["keys"] = [saved_key for saved_key in retry["keys"] if saved_key != key]
                if isinstance(retry.get("targets"), list):
                    retry["targets"] = [target for target in retry["targets"]
                                        if not isinstance(target, dict) or target.get("watch_key") != key]
            _atomic_json(Path(state_path), state)
            return {"ok": True, "action": action, "watch_key": key,
                    "remaining": len(state["collections"]), "network_calls": 0, "notifications": 0}
        if matches:
            saved_notice = (state.get("rarity_onboarding_notices") or {}).get(key)
            if not saved_notice or saved_notice.get("status") != "pending":
                raise ValueError(f"watch already saved: {key}")
            notice = dict(saved_notice)
            entry = matches[0]
        else:
            metadata = metadata_resolver(contract)
            display = request.get("display")
            name = display.strip() if isinstance(display, str) and display.strip() else metadata.get("name")
            if not isinstance(name, str) or not name.strip():
                raise RuntimeError("exact collection metadata did not resolve a name")
            entry = {
                "name": name.strip(), "contract": contract, "rarity": rarity, "watch_key": key,
                "enabled": True, "source_url": _source_url(contract, rarity),
                "baseline_status": "onboarding", "metadata_source": metadata.get("metadata_source", "exact resolver"),
            }
            category = request.get("category")
            if isinstance(category, str) and category.strip():
                entry["category"] = category.strip()
            observation = baseline_runner(dict(entry))
            if observation.get("kind") != "VERIFIED" or not isinstance(observation.get("baseline"), dict):
                reason = str(observation.get("reason") or "baseline unavailable")[:300]
                raise RuntimeError(f"baseline was not verified; watch was not added: {reason}")
            entry["baseline"] = observation["baseline"]
            entry["baseline_status"] = "verified"
            if observation.get("observed_at"):
                entry["last_successful_observation"] = observation["observed_at"]
            state["collections"].append(entry)
            batch_id = "rarity-onboarding-" + uuid.uuid4().hex
            notice = {
                "batch_id": batch_id, "status": "pending",
                "report": _onboarding_report(entry, state),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            state.setdefault("rarity_onboarding_notices", {})[key] = notice
            _atomic_json(Path(state_path), state)

    try:
        message_ids = notice_deliverer(notice["report"], notice["batch_id"])
        if not message_ids:
            raise RuntimeError("Discord returned no verified message IDs")
    except Exception as exc:
        return {"ok": False, "action": "add", "watch_key": key, "watch_enabled": True,
                "discord_status": "pending", "error": f"Discord delivery pending: {str(exc)[:240]}",
                "network_calls": "live metadata and marketplace onboarding" if not matches else 0,
                "notifications": 0}

    with _lock_context(Path(lock_path)) as acquired:
        if not acquired:
            return {"ok": False, "action": "add", "watch_key": key, "watch_enabled": True,
                    "discord_status": "pending", "error": "Discord sent; saved acknowledgment pending",
                    "network_calls": 0, "notifications": 1}
        state = migrated_state(json.loads(Path(state_path).read_text(encoding="utf-8")))
        saved_notice = (state.get("rarity_onboarding_notices") or {}).get(key)
        if saved_notice and saved_notice.get("batch_id") == notice["batch_id"]:
            saved_notice.update(status="delivered", verified_message_ids=message_ids,
                                delivered_at=datetime.now(timezone.utc).isoformat())
            _atomic_json(Path(state_path), state)
        watch = next(item for item in _saved_list(state)["watches"] if item["watch_key"] == key)
    return {"ok": True, "action": "add", "watch": watch,
            "seeded_existing_inventory": True, "discord_status": "delivered",
            "network_calls": "live metadata and marketplace onboarding" if not matches else 0,
            "notifications": 1}


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Exotic/Legendary collection watches without an LLM")
    parser.add_argument("--json", help="JSON request; stdin is used when omitted")
    args = parser.parse_args()
    request = json.loads(args.json if args.json is not None else __import__("sys").stdin.read())
    try:
        print(json.dumps(execute(request), indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
