"""Deterministic lowest-priced all-category MTD Exotic listing monitor.

Uses Rarible item metadata as the exact rarity source and each item's active
best sell order as the listing source. Production runs are serial and preserve
last-known-good state on collection-local failures.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable
from mcfarlane_wallet_aliases import wallet_label


ROOT = Path(__file__).resolve().parent
HERMES = ROOT.parent
STATE = ROOT / "mtd-exotic-lowest-prices.json"
LEDGER = ROOT / "mtd-exotic-lowest-discord.json"
LOCK = ROOT / ".mtd-exotic-lowest-lock"
ALIASES = ROOT / "mcfarlane-wallet-aliases.json"
CHANNEL_ID = "__PRIVATE_DESTINATION_ID__"
CHANNEL_NAME = "mtd-exotic-sniper"
API = "https://api.rarible.org/v0.1/items/byCollection"
COLLECTION_API = "https://api.rarible.org/v0.1/collections/"
PAGE_SIZE = 1000
REQUEST_GAP_SECONDS = 10
HEARTBEAT_LIMIT = 30
EASTERN = datetime.now().astimezone().tzinfo

ROSTER = [
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Batman (1989) Cowl"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "The Tumbler"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Batman Beyond"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Wonder Woman Classic"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Jim Lee's The Flash"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Lobo's Spacehog"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Lobo"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Catwoman: Knightfall Black Suit"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Green Lantern - Hal Jordan"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Batwing (The Flash)"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Superman Action Comics 1"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Doomsday"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Batman by Todd McFarlane"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Martian Manhunter"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Knightfall"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Batman Poster"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Batman: Year Two, Designed by Todd McFarlane"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Bane: Knightfall"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "The Flash - Barry Allen"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Batman Beyond - Kryptonite Suit"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Jim Lee's Superman"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "The Tumbler - Joker Face"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Wonder Woman Gold"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Clayface - Build-A-Figure"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Animal Man - Build-A-Figure"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Mongul - Build-A-Figure"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Kalibak - Build-A-Figure"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Psycho-Pirate - Build-A-Figure"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Lobo on Spacehog - Justice League of America"),
    ("__PRIVATE_WALLET_OR_CONTRACT__", "Batman Who Laughs - Build-A-Figure"),
]


def now_iso() -> str:
    return datetime.now(EASTERN).isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


@contextmanager
def execution_lock(path: Path = LOCK):
    try:
        path.mkdir()
    except FileExistsError:
        yield False
        return
    try:
        (path / "owner.json").write_text(json.dumps({"pid": os.getpid(), "at": now_iso()}), encoding="utf-8")
        yield True
    finally:
        try:
            (path / "owner.json").unlink(missing_ok=True)
            path.rmdir()
        except OSError:
            pass


def initial_state() -> dict:
    return {
        "schema_version": 1,
        "watch_id": "mtd-dc-exotic-lowest-prices",
        "description": "Active Exotic buy-now listings across tracked all-category MTD contracts",
        "channel": {"id": CHANNEL_ID, "name": CHANNEL_NAME},
        "rarity": "Exotic",
        "collections": [
            {"contract": contract, "name": name, "enabled": True, "verified_listings": [], "baseline_status": "pending"}
            for contract, name in ROSTER
        ],
        "pending_collection_snapshots": {},
        "pending_alert_batches": [],
        "heartbeat": {},
    }


def load_state(path: Path = STATE) -> dict:
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else initial_state()
    existing = {str(row.get("contract", "")).lower(): row for row in state.get("collections", [])}
    collections = []
    seed_contracts = {contract for contract, _name in ROSTER}
    for contract, name in ROSTER:
        row = dict(existing.get(contract, {}))
        row.update({"contract": contract, "name": name, "enabled": row.get("enabled", True)})
        row.setdefault("verified_listings", [])
        row.setdefault("baseline_status", "pending")
        collections.append(row)
    for saved in state.get("collections", []):
        contract = str(saved.get("contract", "")).lower()
        if contract in seed_contracts:
            continue
        row = dict(saved)
        row["contract"] = contract
        row.setdefault("name", "Pending name lookup")
        row.setdefault("enabled", True)
        row.setdefault("verified_listings", [])
        row.setdefault("baseline_status", "pending")
        collections.append(row)
    state["collections"] = collections
    state.setdefault("pending_collection_snapshots", {})
    state.setdefault("pending_alert_batches", [])
    state.setdefault("heartbeat", {})
    state["channel"] = {"id": CHANNEL_ID, "name": CHANNEL_NAME}
    state["rarity"] = "Exotic"
    state["description"] = "Active Exotic buy-now listings across tracked all-category MTD contracts"
    return state


def api_key() -> str:
    value = os.getenv("RARIBLE_API_KEY")
    if not value:
        env = HERMES / ".env"
        if env.exists():
            for raw in env.read_text(encoding="utf-8").splitlines():
                if raw.startswith("RARIBLE_API_KEY="):
                    value = raw.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not value:
        raise RuntimeError("RARIBLE_API_KEY unavailable")
    return value


def discord_token() -> str:
    value = os.getenv("DISCORD_BOT_TOKEN")
    if not value:
        env = HERMES / ".env"
        if env.exists():
            for raw in env.read_text(encoding="utf-8").splitlines():
                if raw.startswith("DISCORD_BOT_TOKEN="):
                    value = raw.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not value:
        raise RuntimeError("DISCORD_BOT_TOKEN unavailable")
    return value


def normalize_wallet(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.split(":")[-1].strip().lower()


def resolve_seller(wallet: object) -> tuple[str | None, str]:
    normalized = normalize_wallet(wallet)
    if not normalized:
        return None, "Seller not recorded"
    try:
        aliases = json.loads(ALIASES.read_text(encoding="utf-8"))
    except Exception:
        aliases = {}
    return normalized, wallet_label(normalized, aliases, "Seller not recorded")


def rarity(item: dict) -> str | None:
    for attribute in ((item.get("meta") or {}).get("attributes") or []):
        if str(attribute.get("key", "")).strip().lower() == "rarity":
            return str(attribute.get("value", "")).strip()
    return None


def decimal_text(value: object) -> str | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return format(number.normalize(), "f")


def normalize_listing(item: dict, contract: str, collection_name: str) -> dict | None:
    if rarity(item) != "Exotic":
        return None
    order = item.get("bestSellOrder")
    if not isinstance(order, dict) or order.get("status") != "ACTIVE" or order.get("cancelled") is True:
        return None
    make_type = ((order.get("make") or {}).get("type") or {})
    if str(make_type.get("contract", "")).lower() != "polygon:" + contract:
        return None
    take_type = ((order.get("take") or {}).get("type") or {})
    if take_type.get("@type") != "ETH" or str(take_type.get("blockchain", "")).upper() != "POLYGON":
        return None
    token_id = str(item.get("tokenId") or make_type.get("tokenId") or "").strip()
    order_id = str(order.get("id") or "").strip()
    price = decimal_text(order.get("makePrice") or (order.get("take") or {}).get("value"))
    if not token_id or not order_id or not price:
        return None
    stock = decimal_text(order.get("makeStock") or (order.get("make") or {}).get("value"))
    if stock is None:
        return None
    usd = decimal_text(order.get("makePriceUsd"))
    seller_wallet, seller_display = resolve_seller(order.get("maker"))
    item_name = str(((item.get("meta") or {}).get("name")) or f"Token {token_id}").strip()
    return {
        "listing_id": order_id,
        "contract": contract,
        "collection_name": collection_name,
        "token_id": token_id,
        "item_name": item_name,
        "price": price,
        "currency": "POLYGON",
        "price_usd": usd,
        "seller_wallet": seller_wallet,
        "seller_display": seller_display,
        "order_created_at": order.get("createdAt"),
        "order_updated_at": order.get("lastUpdatedAt"),
        "url": f"https://mcfarlanetoys.digital/token/POLYGON:{contract}:{token_id}",
    }


def comparable(listings: Iterable[dict]) -> list[dict]:
    return sorted(
        [{"listing_id": x["listing_id"], "token_id": x["token_id"], "price": x["price"], "currency": x["currency"]} for x in listings],
        key=lambda x: (x["token_id"], x["listing_id"]),
    )


def fingerprint(listings: Iterable[dict]) -> str:
    raw = json.dumps(comparable(listings), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class SharedListingClient:
    def __new__(cls, *, snapshot_path=None, now=None):
        import sys
        scripts = str(HERMES / "scripts")
        if scripts not in sys.path: sys.path.insert(0, scripts)
        from mtd_shared_rarity_client import SavedRarityClient
        return SavedRarityClient(normalize_listing, listing_sort_key, snapshot_path=snapshot_path, now=now)


def default_client():
    import sys
    scripts = str(HERMES / "scripts")
    if scripts not in sys.path: sys.path.insert(0, scripts)
    from mtd_shared_listing_source import shared_enabled
    return SharedListingClient() if shared_enabled("exotic") else RaribleClient()


class RaribleClient:
    def __init__(self, key: str | None = None, gap_seconds: float = REQUEST_GAP_SECONDS, sleep: Callable[[float], None] = time.sleep):
        self.key = key or api_key()
        self.gap_seconds = gap_seconds
        self.sleep = sleep
        self.last_request_finished: float | None = None

    def _wait(self) -> None:
        if self.last_request_finished is None:
            return
        remaining = self.gap_seconds - (time.monotonic() - self.last_request_finished)
        if remaining > 0:
            self.sleep(remaining)

    def _request_url(self, url: str) -> dict:
        self._wait()
        request = urllib.request.Request(url, headers={"X-API-KEY": self.key, "Accept": "application/json", "User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(request, timeout=50) as response:
                return json.load(response)
        finally:
            self.last_request_finished = time.monotonic()

    def _request(self, params: dict) -> dict:
        return self._request_url(API + "?" + urllib.parse.urlencode(params))

    def collection_name(self, contract: str) -> str:
        collection_id = "POLYGON:" + contract
        data = self._request_url(COLLECTION_API + urllib.parse.quote(collection_id, safe=":"))
        name = str(((data.get("meta") or {}).get("name")) or data.get("name") or "").strip()
        if not name:
            raise RuntimeError("Rarible collection metadata did not include a name")
        return name

    def collection(self, contract: str, name: str) -> list[dict]:
        params = {"collection": "POLYGON:" + contract, "size": str(PAGE_SIZE)}
        data = self._request(params)
        items = list(data.get("items") or [])
        # Rarible currently returns a terminal continuation even when a short
        # page already contains the whole collection. Continue only full pages.
        continuation = data.get("continuation") if len(items) >= PAGE_SIZE else None
        while continuation:
            data = self._request({**params, "continuation": continuation})
            page = list(data.get("items") or [])
            items.extend(page)
            continuation = data.get("continuation") if len(page) >= PAGE_SIZE else None
        listings = [normalize_listing(item, contract, name) for item in items]
        result = [row for row in listings if row]
        result.sort(key=listing_sort_key)
        return result


def listing_sort_key(row: dict):
    try:
        price = Decimal(str(row.get("price")))
    except (InvalidOperation, TypeError, ValueError):
        price = Decimal("Infinity")
    return (price, str(row.get("collection_name", "")), str(row.get("token_id", "")), str(row.get("listing_id", "")))


def price_text(row: dict) -> str:
    price = Decimal(str(row["price"])); pol = f"{price:,.8f}".rstrip("0").rstrip(".")
    usd = row.get("price_usd")
    if usd:
        try:
            return f"{pol} POL (${Decimal(str(usd)):,.2f})"
        except InvalidOperation:
            pass
    return f"{pol} POL (USD unavailable)"


def compact_name(value: str, limit: int = 55) -> str:
    clean = " ".join(str(value).replace("`", "").replace("*", "").split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def render_new_listings(rows: list[dict]) -> str:
    lines = ["**New MTD Exotic listing(s)**"]
    for row in sorted(rows, key=listing_sort_key):
        lines.append(f"• {price_text(row)} — {compact_name(row['collection_name'], 38)} — {compact_name(row['item_name'], 48)} — {row['seller_display']}")
    return "\n".join(lines)


def all_verified_listings(state: dict) -> list[dict]:
    rows = []
    for collection in state.get("collections", []):
        if collection.get("enabled", True) and collection.get("baseline_status") == "verified":
            rows.extend(collection.get("verified_listings", []))
    return sorted(rows, key=listing_sort_key)


def heartbeat_listings(state: dict) -> list[dict]:
    """Return each contract's lowest numeric price group."""
    rows = []
    group_indexes = {}
    floor_prices = {}
    for row in all_verified_listings(state):
        contract = str(row.get("contract", "")).lower()
        contract_key = contract or "listing:" + str(row.get("listing_id", ""))
        try:
            price = Decimal(str(row.get("price")))
        except (InvalidOperation, TypeError, ValueError):
            # Keep at most one malformed legacy row per contract.
            if contract_key in group_indexes:
                continue
        else:
            if contract_key in floor_prices and price != floor_prices[contract_key]:
                continue
            floor_prices.setdefault(contract_key, price)
        if contract_key in group_indexes:
            grouped = rows[group_indexes[contract_key]]
            grouped["same_contract_price_count"] += 1
            grouped["same_contract_price_sellers"].append(
                str(row.get("seller_display") or "Seller not recorded")
            )
            continue
        group_indexes[contract_key] = len(rows)
        representative = dict(row)
        representative["same_contract_price_count"] = 1
        representative["same_contract_price_sellers"] = [
            str(row.get("seller_display") or "Seller not recorded")
        ]
        rows.append(representative)
    return rows


def grouped_seller_text(row: dict) -> str:
    counts = {}
    for seller in row.get("same_contract_price_sellers", []):
        seller = compact_name(seller, 40)
        counts[seller] = counts.get(seller, 0) + 1
    return ", ".join(
        f"{seller} ×{count}" if count > 1 else seller
        for seller, count in counts.items()
    )


def render_heartbeat(state: dict, date: str) -> str:
    rows = heartbeat_listings(state)[:HEARTBEAT_LIMIT]
    lines = [f"**MTD Exotic Lowest Prices — Top {HEARTBEAT_LIMIT} — {date}**"]
    if not rows:
        verified_count = sum(
            1 for row in state.get("collections", [])
            if row.get("enabled", True) and row.get("baseline_status") == "verified"
        )
        lines.append(f"No active Exotic listings in the verified {verified_count}-contract watch.")
    else:
        for row in rows:
            collection_name = compact_name(row["collection_name"], 38)
            if row.get("same_contract_price_count", 1) > 1:
                collection_name = f"{row['same_contract_price_count']}× {collection_name}"
                lines.append(f"• {price_text(row)} — {collection_name} — Sellers: {grouped_seller_text(row)}")
            else:
                lines.append(f"• {price_text(row)} — {collection_name} — {compact_name(row['item_name'], 48)} — {row['seller_display']}")
    return "\n".join(lines)


def discord_request(path: str, payload: dict | None = None) -> dict:
    headers = {"Authorization": "Bot " + discord_token(), "User-Agent": "HermesMTDExoticLowest/1.0", "Content-Type": "application/json"}
    request = urllib.request.Request("https://discord.com/api/v10/" + path, headers=headers,
                                     data=json.dumps(payload).encode() if payload is not None else None)
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.load(response)


def discord_chunks(text: str) -> list[str]:
    chunks = []
    while text:
        end = min(1900, len(text))
        if end < len(text):
            split = text.rfind("\n", 0, end)
            if split > 0:
                end = split
        chunks.append(text[:end].strip())
        text = text[end:].lstrip("\n")
    return chunks


def deliver_discord(text: str, batch_id: str) -> list[str]:
    ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {"batches": {}}
    batches = ledger.setdefault("batches", {})
    digest = hashlib.sha256(text.encode()).hexdigest()
    record = batches.get(batch_id)
    bodies = discord_chunks(text)
    if record is None:
        record = {"sha256": digest, "bodies": bodies, "messages": {}}
        batches[batch_id] = record
        atomic_json(LEDGER, ledger)
    elif record.get("sha256") != digest or record.get("bodies") != bodies:
        raise RuntimeError("Discord batch content changed")
    channel = discord_request("channels/" + CHANNEL_ID)
    if channel.get("id") != CHANNEL_ID or channel.get("name") != CHANNEL_NAME:
        raise RuntimeError("Discord channel identity mismatch")
    verified = []
    for index, body in enumerate(bodies):
        key = str(index); saved = record["messages"].get(key)
        if saved and saved.get("id"):
            message = discord_request(f"channels/{CHANNEL_ID}/messages/{saved['id']}")
        else:
            nonce = str(int(hashlib.sha256(f"{batch_id}:{index}".encode()).hexdigest()[:15], 16))
            message = discord_request(f"channels/{CHANNEL_ID}/messages", {"content": body, "allowed_mentions": {"parse": []}, "nonce": nonce, "enforce_nonce": True, "tts": False})
            record["messages"][key] = {"id": message["id"], "nonce": nonce}
            atomic_json(LEDGER, ledger)
            message = discord_request(f"channels/{CHANNEL_ID}/messages/{message['id']}")
        if message.get("channel_id") != CHANNEL_ID or message.get("content") != body or message.get("id") != record["messages"][key]["id"]:
            raise RuntimeError("Discord read-back mismatch")
        record["messages"][key]["verified"] = True
        atomic_json(LEDGER, ledger)
        verified.append(message["id"])
    record["complete"] = True
    atomic_json(LEDGER, ledger)
    return verified


def queue_alert(state: dict, rows: list[dict], at: str) -> None:
    if not rows:
        return
    text = render_new_listings(rows)
    batch_id = "new-" + hashlib.sha256((at + "\n" + text).encode()).hexdigest()[:24]
    if not any(batch.get("id") == batch_id for batch in state["pending_alert_batches"]):
        state["pending_alert_batches"].append({"id": batch_id, "created_at": at, "text": text})


def queue_interval_status(state: dict, run: dict) -> None:
    text = (
        "**MTD Exotic Lowest Prices update complete**\n"
        f"• {run['checked']}/{run['target_count']} contracts checked — no new Exotic listings found."
    )
    batch_id = "interval-status-" + hashlib.sha256((run["at"] + "\n" + text).encode()).hexdigest()[:24]
    if not any(batch.get("id") == batch_id for batch in state["pending_alert_batches"]):
        state["pending_alert_batches"].append({"id": batch_id, "created_at": run["at"], "text": text})


def deliver_pending_alerts(state: dict, path: Path = STATE, deliver: Callable[[str, str], list[str]] = deliver_discord) -> None:
    changed = False
    for batch in state.get("pending_alert_batches", []):
        if batch.get("delivered_at"):
            continue
        ids = deliver(batch["text"], batch["id"])
        batch["message_ids"] = ids
        batch["delivered_at"] = now_iso()
        changed = True
        atomic_json(path, state)
    if changed:
        state["pending_alert_batches"] = [x for x in state["pending_alert_batches"] if not x.get("delivered_at")][-100:]
        atomic_json(path, state)


def retry_pending_alerts(state_path: Path = STATE,
                         deliver: Callable[[str, str], list[str]] = deliver_discord,
                         lock_path: Path = LOCK) -> dict:
    """Drain only immutable saved batches; source readiness is irrelevant."""
    with execution_lock(lock_path) as acquired:
        if not acquired:
            return {"status": "busy"}
        state = load_state(state_path)
        deliver_pending_alerts(state, state_path, deliver)
        return {"status": "delivery_retried"}


def promote_snapshot(collection: dict, observed: list[dict], at: str) -> list[dict]:
    prior = collection.get("verified_listings", [])
    prior_ids = {row.get("listing_id") for row in prior}
    new_rows = [row for row in observed if row.get("listing_id") not in prior_ids]
    collection["verified_listings"] = observed
    collection["baseline_status"] = "verified"
    collection["last_verified_at"] = at
    collection.pop("last_error", None)
    return new_rows


def collect(mode: str, state_path: Path = STATE, client: RaribleClient | None = None,
            deliver: Callable[[str, str], list[str]] = deliver_discord,
            lock_path: Path = LOCK) -> dict:
    if mode not in {"baseline", "interval", "daily", "confirm"}:
        raise ValueError("invalid mode")
    with execution_lock(lock_path) as acquired:
        if not acquired:
            return {"status": "busy"}
        state = load_state(state_path)
        at = now_iso()
        client = client or default_client()
        pending = state["pending_collection_snapshots"]
        if mode == "confirm":
            target_contracts = set(pending)
            targets = [c for c in state["collections"] if c["contract"] in target_contracts]
            if not targets:
                deliver_pending_alerts(state, state_path, deliver)
                return {"status": "no_pending_candidates", "checked": 0}
        else:
            targets = [c for c in state["collections"] if c.get("enabled", True)]
        provenance = None
        if callable(getattr(client, "prepare", None)):
            if mode != "baseline":
                deliver_pending_alerts(state, state_path, deliver)
            try:
                client.prepare([c["contract"] for c in targets])
                provenance = client.provenance
                from mtd_shared_rarity_client import advances
                previous = state.get("shared_last_source")
                if previous and not advances(provenance, previous):
                    return {"status": "deferred", "reason": "awaiting_independent_generation", "ready": False, "generation": provenance["generation"], "checked": 0}
                at = provenance["observed_at"]
            except ValueError as exc:
                return {"status": "deferred", "reason": "source_unavailable", "ready": False, "error": str(exc), "checked": 0}
        failures = []
        promoted_new = []
        checked = 0
        for collection in targets:
            contract = collection["contract"]
            try:
                observed = client.collection(contract, collection["name"])
                checked += 1
                observed_fingerprint = fingerprint(observed)
                if mode == "baseline" or collection.get("baseline_status") != "verified":
                    promote_snapshot(collection, observed, at)
                    pending.pop(contract, None)
                    continue
                prior_fingerprint = fingerprint(collection.get("verified_listings", []))
                candidate = pending.get(contract)
                if observed_fingerprint == prior_fingerprint:
                    pending.pop(contract, None)
                    collection["last_checked_at"] = at
                    collection.pop("last_error", None)
                    continue
                if candidate and candidate.get("fingerprint") == observed_fingerprint and (provenance is None or advances(provenance, candidate.get("source"))):
                    promoted_new.extend(promote_snapshot(collection, observed, at))
                    pending.pop(contract, None)
                else:
                    pending[contract] = {"fingerprint": observed_fingerprint, "observed": observed, "first_seen_at": at}
                    if provenance is not None: pending[contract]["source"] = dict(provenance)
                    collection["last_checked_at"] = at
                    collection.pop("last_error", None)
            except Exception as exc:
                collection["last_error"] = str(exc)[:300]
                collection["last_failed_at"] = at
                failures.append({"contract": contract, "name": collection["name"], "error": str(exc)[:300]})
        complete = not failures and checked == len(targets)
        if provenance is not None and complete: state["shared_last_source"] = dict(provenance)
        if mode in {"interval", "daily"} and complete:
            state["last_full_complete_at"] = at
            if mode == "daily":
                heartbeat_slot = at
                if provenance is not None:
                    from mtd_shared_listing_schedule import CURRENT_OCCURRENCE
                    occurrence = CURRENT_OCCURRENCE.get()
                    heartbeat_slot = (occurrence.get("scheduled_at") if isinstance(occurrence, dict)
                                      and occurrence.get("execution_id") and not occurrence.get("manual") else None)
                if heartbeat_slot:
                    state["heartbeat"]["source_date"] = datetime.fromisoformat(heartbeat_slot.replace("Z", "+00:00")).astimezone(EASTERN).date().isoformat()
                    state["heartbeat"]["source_completed_at"] = at
        if mode == "confirm" and complete:
            state["last_confirmation_complete_at"] = at
        run = {"at": at, "mode": mode, "complete": complete, "checked": checked, "target_count": len(targets), "failures": failures, "new_listings_promoted": len(promoted_new), "pending_collection_count": len(pending)}
        queue_alert(state, promoted_new, at)
        if mode == "interval" and complete and not promoted_new and not pending:
            queue_interval_status(state, run)
        state["last_run"] = run
        atomic_json(state_path, state)
        if mode != "baseline":
            deliver_pending_alerts(state, state_path, deliver)
        return state["last_run"]


def send_heartbeat(state_path: Path = STATE, deliver: Callable[[str, str], list[str]] = deliver_discord,
                   lock_path: Path = LOCK) -> dict:
    with execution_lock(lock_path) as acquired:
        if not acquired:
            return {"status": "busy"}
        state = load_state(state_path)
        today = datetime.now(EASTERN).date().isoformat()
        heartbeat = state["heartbeat"]
        if heartbeat.get("delivered_date") == today:
            return {"status": "already_delivered", "date": today}
        if heartbeat.get("source_date") != today or state.get("pending_collection_snapshots"):
            return {"status": "not_ready", "date": today, "source_date": heartbeat.get("source_date"), "pending_collection_count": len(state.get("pending_collection_snapshots", {}))}
        text = heartbeat.get("pending_text") if heartbeat.get("pending_date") == today else render_heartbeat(state, today)
        heartbeat.update({"pending_date": today, "pending_text": text})
        atomic_json(state_path, state)
        batch_id = "heartbeat-" + today
        ids = deliver(text, batch_id)
        heartbeat.update({"delivered_date": today, "delivered_at": now_iso(), "message_ids": ids})
        heartbeat.pop("pending_date", None); heartbeat.pop("pending_text", None)
        atomic_json(state_path, state)
        return {"status": "delivered", "date": today, "message_ids": ids,
                "row_count": min(HEARTBEAT_LIMIT, len(heartbeat_listings(state)))}


def validate_roster() -> None:
    contracts = [contract for contract, _ in ROSTER]
    if len(contracts) != 30 or len(set(contracts)) != 30:
        raise RuntimeError("roster must contain exactly 30 unique contracts")
    if any(len(contract) != 42 or not contract.startswith("0x") or any(ch not in "0123456789abcdef" for ch in contract[2:]) for contract in contracts):
        raise RuntimeError("invalid Polygon contract in roster")


validate_roster()
