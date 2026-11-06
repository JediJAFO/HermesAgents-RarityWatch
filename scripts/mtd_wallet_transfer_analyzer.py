"""Loopback-only McFarlane Toys Digital wallet transfer analyzer."""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import json
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import parse_qs, urlparse
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

TYPES = ("SELL", "TRANSFER", "MINT")
OVERLAP_SECONDS = 86400
HISTORY_LIMIT = 100
ACTIVITIES_API = "https://api.rarible.org/v0.1/activities/search"
MIN_CALL_GAP_SECONDS = 10.0
HOME = Path(__file__).resolve().parents[1]
PRICE_WATCHES = HOME / "price-watches"
DEFAULT_STATE = PRICE_WATCHES / "mtd-wallet-transfer-analysis.json"
DEFAULT_LOCK = PRICE_WATCHES / ".mtd-wallet-transfer-analysis.lock"
DEFAULT_CATALOG = PRICE_WATCHES / "mcfarlane-dc-sales.json"
DEFAULT_ALIASES = PRICE_WATCHES / "mcfarlane-wallet-aliases.json"
DEFAULT_CSRF = HOME / "cache" / "mtd-wallet-transfer-analyzer-csrf.key"
MAX_REQUEST_BYTES = 4096
_HISTORY_IMPORT_LOCK = threading.RLock()


def history_module(name):
    """Support dynamically loaded portal entrypoints without leaking sys.path."""
    import importlib
    import sys
    directory = str(Path(__file__).resolve().parent)
    with _HISTORY_IMPORT_LOCK:
        added = directory not in sys.path
        if added: sys.path.insert(0, directory)
        try:
            # Store/provider lazy helpers import the canonical analyzer by name.
            importlib.import_module('mtd_wallet_transfer_analyzer')
            return importlib.import_module(name)
        finally:
            if added: sys.path.remove(directory)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Activity time must include timezone")
    return parsed


def address(value: object) -> str:
    return str(value or "").split(":")[-1].lower()


def transactions(rows: list[dict], wallet: str, catalog: dict[str, dict], *, private: bool = False) -> list[dict]:
    normalized = {}
    for activity in rows:
        if activity.get("reverted"):
            continue
        kind = activity.get("@type")
        if kind not in TYPES:
            raise ValueError("Unexpected activity type")
        sale = kind == "SELL"
        nft = activity.get("nft", {}).get("type", {}) if sale else activity
        collection = str(nft.get("collection") or nft.get("contract") or "").lower()
        if collection not in catalog or not collection.startswith("polygon:"):
            raise ValueError("Activity outside exact Polygon allowlist")
        sender = address(activity.get("seller") if sale else activity.get("from"))
        recipient = address(activity.get("buyer") if sale else activity.get("owner"))
        # Rarible represents ordinary mints with a null `from`. Treat that as
        # the zero address for classification; mints are evidence only and are
        # excluded from the report. All other movement parties must be exact.
        if kind == "MINT" and not sender:
            sender = "0x" + "0" * 40
        if not re.fullmatch(r"0x[0-9a-f]{40}", sender) or not re.fullmatch(r"0x[0-9a-f]{40}", recipient):
            raise ValueError("Activity contains malformed sender or recipient wallet")
        if wallet not in (sender, recipient):
            raise ValueError("Activity does not involve queried wallet")
        identity = str(activity.get("id") or "").strip()
        transaction_hash = str(activity.get("transactionHash") or (activity.get("blockchainInfo") or {}).get("transactionHash") or "").strip()
        activity_date = activity.get("date")
        if not identity or not transaction_hash or not isinstance(activity_date, str) or not activity_date.strip():
            raise ValueError("Missing date, transaction, or activity identity")
        timestamp(activity_date)
        action = ("Bought" if recipient == wallet else "Sold") if sale else (
            "Minted" if kind == "MINT" else ("Transfer in" if recipient == wallet else "Transfer out"))
        token_id = str(nft.get("tokenId") or "").strip()
        if not token_id or token_id == "None":
            raise ValueError("Activity tokenId is missing")
        quantity_raw = (activity.get("nft") or {}).get("value", activity.get("value", "1"))
        try:
            quantity = Decimal(str(quantity_raw))
        except (InvalidOperation, ValueError):
            raise ValueError("Activity quantity must be positive and finite") from None
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("Activity quantity must be positive and finite")
        quantity_text = format(quantity, "f")
        if "." in quantity_text:
            quantity_text = quantity_text.rstrip("0").rstrip(".")
        embedded_name = (activity.get("meta") or {}).get("name") or (activity.get("nft") or {}).get("name") or nft.get("name")
        collection_name = catalog[collection].get("display_name") or "Name unavailable"
        nft_name = str(embedded_name or collection_name).strip()
        if not re.search(r"#" + re.escape(token_id) + r"(?:\b|$)", nft_name):
            nft_name += " #" + token_id
        event = {
            "activity_ids": [identity], "type": kind, "action": action, "date": activity["date"],
            "transaction_hash": transaction_hash, "collection": collection,
            "collection_name": collection_name, "nft_name": nft_name, "asset_identity": collection + ":" + token_id,
            "token_id": token_id, "sender": sender, "recipient": recipient,
            "quantity": quantity_text,
        }
        if sale:
            payment = activity.get("payment", {}).get("type", {})
            native = payment.get("@type") == "ETH" and payment.get("blockchain") == "POLYGON"
            event.update(price=activity.get("price"), currency="POL" if native else "Payment asset not identified",
                         usd=activity.get("priceUsd") if activity.get("priceUsd") is not None else activity.get("amountUsd"))
        normalized[identity] = event

    if private:
        return list(normalized.values())

    def asset_tx(event: dict) -> tuple:
        return event["transaction_hash"], event["collection"], event["token_id"]

    def representation(event: dict) -> tuple:
        return asset_tx(event) + (event["sender"], event["recipient"], event["quantity"])

    mint_transactions = {event["transaction_hash"] for event in normalized.values() if event["type"] == "MINT"}
    mint_keys = {asset_tx(event) for event in normalized.values() if event["type"] == "MINT"}
    sales = {representation(event): event for event in normalized.values()
             if event["type"] == "SELL" and event["transaction_hash"] not in mint_transactions
             and asset_tx(event) not in mint_keys
             and (event["action"] == "Sold" or event["sender"] != "0x" + "0" * 40)}
    groups = {}
    for event in normalized.values():
        # A transaction containing any allowlisted MINT is a primary-sale,
        # redemption, or craft transaction. Exclude the entire transaction so
        # consumed-token transfer legs are not misreported as ordinary moves.
        if event["transaction_hash"] in mint_transactions:
            continue
        if event["type"] == "TRANSFER" and representation(event) in sales:
            sales[representation(event)]["activity_ids"].extend(event["activity_ids"])
            continue
        if event["type"] == "SELL" and representation(event) not in sales:
            continue
        group = groups.setdefault(event["transaction_hash"], {"transaction_hash": event["transaction_hash"], "date": event["date"], "events": []})
        group["events"].append(event)
        if timestamp(event["date"]) > timestamp(group["date"]):
            group["date"] = event["date"]
    return sorted((group for group in groups.values() if group["events"]),
                  key=lambda group: (timestamp(group["date"]), group["transaction_hash"]), reverse=True)


def collect_wallet(fetch: Callable[[dict], dict], wallet: str, catalog: dict[str, dict], previous: dict | None,
                   progress: Callable[[dict], None] | None = None, max_pages: int = 500) -> dict:
    report = progress or (lambda _event: None)
    cutoff = timestamp(previous["checkpoint"]) - timedelta(seconds=OVERLAP_SECONDS) if previous else None
    rows: list[dict] = []
    cursor = None
    cursors = set()
    last_date = None
    page_number = 0
    for _ in range(max_pages):
        page_number += 1
        payload = {"size": 100, "sort": "LATEST", "filter": {
            "blockchains": ["POLYGON"], "types": list(TYPES),
            "collections": [entry["id"] for entry in catalog.values()],
            "users": {"any": ["ETHEREUM:" + wallet]},
        }}
        if cursor:
            payload["cursor"] = cursor
        report({"stage": "fetching", "message": f"Fetching activity page {page_number}", "page": page_number,
                "network_calls": page_number - 1, "activities_scanned": len(rows),
                "transactions_found": len(transactions(rows, wallet, catalog)) if rows else 0, "pacing_wait": 0})
        page = fetch(payload)
        current = page.get("activities")
        if not isinstance(current, list):
            raise ValueError("Malformed activities response")
        for activity in current:
            current_date = timestamp(activity.get("date"))
            if last_date is not None and current_date > last_date:
                raise ValueError("Activities are not newest-first")
            last_date = current_date
        rows.extend(current)
        groups = transactions(rows, wallet, catalog)
        report({"stage": "processing", "message": f"Processed activity page {page_number}", "page": page_number,
                "network_calls": page_number, "activities_scanned": len(rows), "transactions_found": len(groups), "pacing_wait": 0})
        cursor = page.get("cursor")
        if not cursor:
            break
        if not current or cursor in cursors:
            raise ValueError("Empty or repeated pagination cursor")
        cursors.add(cursor)
        if cutoff is not None and last_date < cutoff:
            break
        if cutoff is None and len(groups) >= HISTORY_LIMIT and last_date < timestamp(groups[HISTORY_LIMIT - 1]["date"]):
            break
    else:
        raise ValueError("Page budget exhausted; saved state unchanged")

    eligible = [activity for activity in rows if cutoff is None or timestamp(activity["date"]) >= cutoff]
    old_raw = list(previous.get("recent_raw", [])) if previous else []
    merged = {str(activity["id"]): activity for activity in old_raw}
    merged.update({str(activity["id"]): activity for activity in eligible})
    all_groups = transactions(list(merged.values()), wallet, catalog)
    history = all_groups[:HISTORY_LIMIT]
    retained_transactions = {group["transaction_hash"] for group in history}
    recent_raw = [activity for activity in merged.values()
                  if str(activity.get("transactionHash") or (activity.get("blockchainInfo") or {}).get("transactionHash") or "") in retained_transactions]
    previous_transactions = {group["transaction_hash"] for group in previous.get("history", [])} if previous else set()
    known_activity_ids = set(previous.get("seen_activity_ids", [])) if previous else set()
    new_count = (sum(group["transaction_hash"] not in previous_transactions and any(
        activity_id not in known_activity_ids for event in group["events"] for activity_id in event["activity_ids"])
        for group in history) if previous else len(history))
    checkpoint_values = [activity["date"] for activity in rows]
    if previous:
        checkpoint_values.append(previous["checkpoint"])
    checkpoint = max(checkpoint_values, key=timestamp) if checkpoint_values else utcnow()
    return {
        "wallet": wallet, "mode": "incremental update" if previous else "initial scan", "observed_at": utcnow(),
        "checkpoint": checkpoint, "seen_activity_ids": sorted(str(activity["id"]) for activity in recent_raw),
        "history": history, "recent_raw": recent_raw, "new_transaction_count": new_count,
        "pages_fetched": page_number, "network_calls": page_number, "activities_scanned": len(rows),
        "history_exhausted": not bool(cursor),
    }


def load_catalog(path: Path) -> dict[str, dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("collections")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Catalog must contain collections")
    catalog = {}
    for row in rows:
        identity = str(row.get("id") or "")
        if not re.fullmatch(r"POLYGON:0x[0-9a-fA-F]{40}", identity):
            raise ValueError("Catalog contains a malformed Polygon collection ID")
        catalog[identity.lower()] = dict(row)
    return catalog


def alias_for(wallet: str, aliases: dict) -> str | None:
    wallet = str(wallet).lower()
    entries = aliases.get("aliases", []) if isinstance(aliases, dict) else []
    for entry in entries:
        if str(entry.get("wallet") or "").lower() == wallet and isinstance(entry.get("alias"), str):
            return entry["alias"]
    exact = aliases.get("exact_aliases", {}) if isinstance(aliases, dict) else {}
    if isinstance(exact.get(wallet), str):
        return exact[wallet]
    matches = []
    for entry in aliases.get("masked_aliases", []) if isinstance(aliases, dict) else []:
        if wallet.startswith(str(entry.get("prefix") or "").lower()) and wallet.endswith(str(entry.get("suffix") or "").lower()):
            matches.append(str(entry.get("name_tag") or entry.get("alias") or ""))
    return matches[0] if len(matches) == 1 and matches[0] else None


def party_label(wallet: str, aliases: dict) -> str:
    alias = alias_for(wallet, aliases)
    abbreviated = "0x..." + str(wallet)[-4:]
    return ((html.escape(alias) + " — ") if alias else "") + html.escape(abbreviated)


def detect_round_trips(history: list[dict], wallet: str) -> list[dict]:
    pending_out = {}
    pending_bought = {}
    patterns = []
    for group in sorted(history, key=lambda item: timestamp(item["date"])):
        for event in sorted(group.get("events", []), key=lambda item: timestamp(item["date"])):
            asset = event["asset_identity"]
            if event["action"] in ("Transfer out", "Sold") and event["sender"] == wallet:
                candidates = pending_bought.get(asset, [])
                index = next((index for index in range(len(candidates) - 1, -1, -1)
                              if candidates[index]["counterparty"] == event["recipient"] and
                              timestamp(candidates[index]['date']) < timestamp(event['date'])), None)
                if index is not None:
                    prior = candidates.pop(index)
                    patterns.append({"asset_identity": asset, "direction": "bought_then_out",
                                     "counterparty": event["recipient"], "left_at": prior["date"],
                                     "returned_at": event["date"], "in_transaction_hash": prior["transaction_hash"],
                                     "out_transaction_hash": event["transaction_hash"], "nft_name": event["nft_name"]})
                else:
                    pending_out.setdefault(asset, []).append({"counterparty": event["recipient"], "date": event["date"],
                                                               "transaction_hash": event["transaction_hash"], "nft_name": event["nft_name"]})
            elif event["action"] == "Bought" and event["recipient"] == wallet:
                candidates = pending_out.get(asset, [])
                index = next((index for index in range(len(candidates) - 1, -1, -1)
                              if candidates[index]["counterparty"] == event["sender"] and
                              timestamp(candidates[index]['date']) < timestamp(event['date'])), None)
                if index is not None:
                    prior = candidates.pop(index)
                    patterns.append({"asset_identity": asset, "direction": "out_then_bought",
                                     "counterparty": event["sender"], "left_at": prior["date"],
                                     "returned_at": event["date"], "out_transaction_hash": prior["transaction_hash"],
                                     "return_transaction_hash": event["transaction_hash"], "nft_name": event["nft_name"]})
                else:
                    pending_bought.setdefault(asset, []).append({"counterparty": event["sender"], "date": event["date"],
                                                                  "transaction_hash": event["transaction_hash"], "nft_name": event["nft_name"]})
    return sorted(patterns, key=lambda row: timestamp(row["returned_at"]), reverse=True)


def saved_findings(result: dict, counterparties: dict[str, list[dict]], catalog: dict) -> dict:
    """Exact-identity deterministic review rules over saved evidence only."""
    wallet = result['wallet']
    matrix = {}
    for group in result.get('history', []):
        for event in group['events']:
            other = event['sender'] if event['recipient'] == wallet else event['recipient']
            if other in (wallet, '0x' + '0' * 40):
                continue
            row = matrix.setdefault(other, {action: 0 for action in ('Bought', 'Sold', 'Transfer in', 'Transfer out')})
            row[event['action']] = row.get(event['action'], 0) + 1
            row.setdefault('dates', []).append(event['date'])
            row.setdefault('assets_in', set())
            row.setdefault('assets_out', set())
            row['assets_in' if event['recipient'] == wallet else 'assets_out'].add(event['asset_identity'])
            minute = timestamp(event['date']).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M')
            row.setdefault('bursts', {})[minute] = row.setdefault('bursts', {}).get(minute, 0) + 1
    for row in matrix.values():
        row.update(first=min(row['dates'], key=timestamp), latest=max(row['dates'], key=timestamp),
                   unique_assets=len(row['assets_in'] | row['assets_out']),
                   same_asset_overlap=len(row['assets_in'] & row['assets_out']),
                   interaction_dates=len({timestamp(d).date() for d in row['dates']}),
                   max_events_per_minute=max(row['bursts'].values()))
        for key in ('dates', 'assets_in', 'assets_out', 'bursts'):
            del row[key]
    paths = []
    for other, raw in sorted(counterparties.items()):
        normalized = transactions(raw, other, catalog, private=True)
        ordinary = [e for g in transactions(raw, other, catalog) for e in g['events']]
        acquisitions = [e for e in normalized if e['recipient'] == other and e['action'] == 'Minted']
        acquisitions += [e for e in ordinary if e['recipient'] == other and e['action'] in ('Bought', 'Transfer in')]
        sales = [e for e in ordinary if e['action'] == 'Sold' and e['recipient'] == wallet]
        for sale in sales:
            preceding = [e for e in acquisitions if e['asset_identity'] == sale['asset_identity'] and
                         timestamp(e['date']) < timestamp(sale['date'])]
            if preceding:
                prior = max(preceding, key=lambda e: (timestamp(e['date']), e['activity_ids'][0]))
                paths.append({'counterparty': other, 'asset_identity': sale['asset_identity'], 'nft_name': sale['nft_name'],
                              'acquisition': prior['action'], 'acquired_at': prior['date'], 'sold_at': sale['date']})
    return {'matrix': matrix, 'acquisition_paths': paths}


def coverage_html(coverage: dict, label: str = '') -> str:
    keys = [('Oldest', 'oldest'), ('Newest', 'newest'), ('Last successful page retrieval', 'last_success'),
            ('History exhausted (provider pagination only)', 'history_exhausted'), ('Partial', 'partial'),
            ('Stale (>24h / unknown)', 'stale'), ('Last error', 'error'), ('Source', 'source')]
    details = ' · '.join(html.escape(name) + ': ' + html.escape(str(coverage.get(key, 'unknown')))
                         for name, key in keys)
    return ('<h2>Evidence coverage' + label + '</h2><p>' + details + '</p><p>Scope: Polygon · SELL / TRANSFER / MINT · '
            + str(len(coverage.get('scope_collections', []))) + ' exact allowlisted collections · scope fingerprint '
            + html.escape(str(coverage.get('scope', 'unknown'))) +
            ' · NFT event display: latest 100 transactions only; archive and findings are not capped.</p>')


def findings_html(result: dict, aliases: dict) -> str:
    findings = result.get('findings') or saved_findings(result, {}, {})
    rows = []
    for other, row in sorted(findings['matrix'].items()):
        cells = [party_label(other, aliases)] + [html.escape(str(row.get(key, 'unknown'))) for key in
                 ('Bought', 'Sold', 'Transfer in', 'Transfer out', 'first', 'latest', 'unique_assets',
                  'same_asset_overlap', 'interaction_dates', 'max_events_per_minute')]
        rows.append('<tr>' + ''.join('<td>' + cell + '</td>' for cell in cells) + '</tr>')
    heads = ('Alias / wallet', 'Bought from', 'Sold to', 'Transfer in from', 'Transfer out to', 'First', 'Latest',
             'Unique assets', 'Same-asset in/out overlap', 'Interaction dates', 'Max events / UTC minute')
    paths = ''.join('<li>' + party_label(p['counterparty'], aliases) + ' · ' + html.escape(p['nft_name']) +
                    ' · ' + html.escape(p['acquisition'] + ' ' + p['acquired_at'] + ' → Sold to analyzed wallet ' + p['sold_at'])
                    + '</li>' for p in findings['acquisition_paths'])
    coverage = coverage_html(result.get('coverage', {}))
    broad = result.get('broad_monitor_coverage', {})
    coverage += '<p>Broad transfer monitor (saved window metadata only; events not merged): ' + ' · '.join(
        html.escape(key + ': ' + str(broad.get(key, 'unknown'))) for key in ('start', 'end', 'complete', 'last_complete_check')) + '</p>'
    for other, saved_coverage in result.get('counterparty_coverage', {}).items():
        coverage += coverage_html(saved_coverage, ' — ' + party_label(other, aliases))
    return (coverage + '<h2>Counterparty matrix — all retained evidence</h2><div class="table-scroll"><table><thead><tr>'
            + ''.join('<th>' + h + '</th>' for h in heads) + '</tr></thead><tbody>' + ''.join(rows) +
            '</tbody></table></div><h2>Selected counterparty acquisition → sale paths</h2><ul>' +
            (paths or '<li>Exact preceding acquisition not found in retained evidence.</li>') +
            '</ul><p>Deterministic findings for review, not proof of common ownership or XP eligibility. '
            'Mint does not establish code redemption; payment-flow evidence is absent. Repeated purchases alone '
            'are weak evidence. Missing or stale history limits conclusions. No recursive wallet crawling.</p>')


def valid_order_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"POLYGON:0x[0-9a-fA-F]{64}", value) is not None


def matched_order_evidence(activity: dict, orders: list[dict]) -> dict:
    """Match execution identity before interpreting provider timestamps or side.

    Schema: @rarible/api-client Activity.OrderMatchSell.type is SELL/ACCEPT_BID.
    A generic @type SELL, opposite-side order, or dual hashes is not route proof.
    """
    output = {'classification': 'unknown', 'confidence': 'insufficient evidence',
              'order_created_at': None, 'order_age_seconds': None, 'effective_start_at': None,
              'matched_order_age_seconds': None, 'exposure_seconds': None, 'restriction': 'unknown',
              'gap': 'Listing publication and price revision history unavailable; order age is not time at the final cheap price.'}
    if activity.get('@type') != 'SELL' or activity.get('reverted'):
        return output
    for order in orders:
        try:
            identity = order.get('id')
            if not valid_order_id(identity) or order.get('cancelled') or order.get('status') not in ('ACTIVE', 'FILLED'):
                continue
            ask = identity == 'POLYGON:' + str(activity.get('sellerOrderHash'))
            bid = identity == 'POLYGON:' + str(activity.get('buyerOrderHash'))
            if ask == bid:
                continue
            maker, other = (activity['seller'], activity['buyer']) if ask else (activity['buyer'], activity['seller'])
            # Union addresses must use the Ethereum namespace, never strip arbitrary chains.
            if not all(re.fullmatch(r'ETHEREUM:0x[0-9a-fA-F]{40}', str(v)) for v in (maker, other, order.get('maker'))):
                continue
            if order['maker'].lower() != maker.lower():
                continue
            taker = order.get('taker')
            zero = 'ETHEREUM:0x' + '0'*40
            if taker is not None and (not re.fullmatch(r'ETHEREUM:0x[0-9a-fA-F]{40}', str(taker)) or
                                      taker.lower() not in (zero.lower(), other.lower())):
                continue
            if order.get('platform') != activity.get('source'):
                continue
            currency = order['take' if ask else 'make']['type']
            if currency.get('@type') not in ('ETH', 'ERC20', 'CURRENCY_NATIVE', 'CURRENCY_TOKEN'):
                continue
            nft = order['make' if ask else 'take']
            asset = nft['type']
            sold_asset = activity['nft']['type']
            collection = str(asset.get('collection') or asset.get('contract') or asset.get('collectionId') or '').lower()
            sold_collection = str(sold_asset.get('collection') or sold_asset.get('contract') or '').lower()
            floor = not ask and asset.get('@type') in ('COLLECTION', 'NFT_OF_COLLECTION')
            if collection != sold_collection or not collection.startswith('polygon:'):
                continue
            if floor:
                asset_match = 'collection bid; token selected by execution activity'
            else:
                if asset.get('@type') not in ('ERC721', 'ERC1155', 'NFT'):
                    continue
                token = str(asset.get('tokenId')) if asset.get('@type') != 'NFT' else str(asset.get('itemId', '')).split(':')[-1]
                if token != str(sold_asset.get('tokenId')):
                    continue
                asset_match = 'exact contract and token'
            quantity = Decimal(str(nft['value']))
            sold_quantity = Decimal(str(activity['nft']['value']))
            if not quantity.is_finite() or not sold_quantity.is_finite() or sold_quantity <= 0 or quantity < sold_quantity:
                continue
            created, sold = timestamp(order['createdAt']), timestamp(activity['date'])
            updated = timestamp(order['lastUpdatedAt'])
            effective = max([order['createdAt']] + ([order['startedAt']] if order.get('startedAt') else []), key=timestamp)
            if created > sold or updated < created or timestamp(effective) > sold:
                continue
            if order.get('endedAt') and timestamp(order['endedAt']) < sold:
                continue
            age = (sold-created).total_seconds()
            # Ask timing is independent of whether execution route can be proven.
            if ask:
                output.update(order_created_at=order['createdAt'], order_age_seconds=age, effective_start_at=effective)
            execution_match = activity.get('orderId') == identity
            route = activity.get('type')
            if execution_match and ((ask and route == 'SELL') or (bid and route == 'ACCEPT_BID')):
                output.update(classification='buy-now' if ask else 'accepted bid',
                              confidence='provider matched activity + order', matched_order_age_seconds=age,
                              asset_match=asset_match, source=activity.get('source'),
                              restriction='restricted to execution counterparty' if taker and taker.lower() != zero.lower()
                                          else 'explicit unrestricted taker' if taker else 'unknown')
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    if output['classification'] == 'unknown':
        output['gap'] += ' Authoritative execution order/type/side match missing or rejected.'
    return output

def sale_evidence_html(result: dict) -> str:
    rows = []
    for evidence in result.get('sale_evidence', [])[:HISTORY_LIMIT]:
        # Hashes, order IDs and full maker/taker wallets remain private.
        values = [evidence['nft_name'], evidence.get('purchased_at'), evidence['classification'],
                  evidence.get('order_created_at'), evidence.get('order_age_seconds'),
                  evidence.get('effective_start_at'), 'unavailable', evidence.get('confidence'),
                  evidence.get('source') or evidence.get('activity_type') or 'unknown', evidence.get('restriction'),
                  ', '.join(evidence.get('order_lookup_status', [])) or 'no valid identifiers',
                  evidence.get('order_retrieved_at'), evidence.get('order_source'), evidence['gap']]
        rows.append('<tr>' + ''.join('<td>' + html.escape(str(v if v is not None else 'unavailable')) + '</td>' for v in values) + '</tr>')
    progress = result.get('order_progress', {})
    status = '<p>Order lookup progress: ' + html.escape(' · '.join(str(k) + ': ' + str(v) for k, v in progress.items())) + '</p>' if progress else ''
    if result.get('action_error'):
        status += '<p class="error">' + html.escape(result['action_error']) + '</p>'
    heads = ('NFT', 'Purchase timestamp', 'Execution', 'Matched sell order created', 'Provider order age (seconds)',
             'Effective configured start (not publication)', 'Public exposure', 'Confidence', 'Activity source',
             'Taker restriction', 'Order lookup status', 'Order retrieved', 'Order provenance', 'Evidence gaps')
    return ('<h2>Sale execution / listing exposure</h2>' + status +
            '<p>Execution is classified only with authoritative matched activity type, order identity, parties, asset and quantity. '
            'SELL or two order hashes alone are insufficient. Unknown remains unknown. A configured start is not publication; '
            'order creation-to-sale is provider order age, not public exposure or time at the final cheap price. '
            'Accepted bid listing duration is unavailable. Post-sale retrieval and activity first observation are not exposure. '
            'Public LIST publication, continuous availability and detailed reprice history are unavailable from this order lookup. '
            'No public listing duration or low-price duration is inferred. Short duration is not proof of common control or XP intent.</p>'
            '<div class="table-scroll"><table><thead><tr>' + ''.join('<th>' + h + '</th>' for h in heads) +
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')

def order_queue_html(queue: dict) -> str:
    if not queue: return ''
    counters=' · '.join(label+': '+str(int(queue.get(key,0))) for label,key in [('Queued','queued'),('Running','running'),('Saved','saved'),('Missing','missing'),('Failed','failed'),('Deferred','deferred'),('Remaining','remaining'),('Total orders','total')])
    paused='PAUSED — use Resume All Queue' if queue.get('queue_paused') else 'enabled'
    return '<section class="order-queue-summary"><h2>Purchase-order background queue</h2><p><strong>Shared queue: '+paused+'</strong> · Authentication blocked: '+str(bool(queue.get('auth_blocked')))+ '</p><p>'+counters+'</p><p>Latest saved progress: '+html.escape(str(queue.get('latest_progress') or 'not started'))+' · Next due (epoch): '+html.escape(str(queue.get('next_attempt_at'))) +' · Provider deferred until (epoch): '+html.escape(str(queue.get('provider_not_before')))+'</p><p>Fixed snapshot of saved secondary-purchase order identifiers; not an NFT count. No wallet activity refresh or recursive discovery. Completed missing (404) lookups need an explicit retry. Saved viewing: network calls 0; notifications 0; LLM tokens 0.</p></section>'


def render_report(result: dict, aliases: dict) -> str:
    wallet = result["wallet"]
    counter = {}
    event_rows = []
    for group in result.get("history", [])[:HISTORY_LIMIT]:
        eastern = timestamp(group["date"]).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M:%S %p %Z")
        for event in group.get("events", []):
            destination = event["recipient"]
            other = event["sender"] if destination == wallet else destination
            if other and other != wallet and other != "0x" + "0" * 40:
                row = counter.setdefault(other, {"transactions": set(), "inbound": 0, "outbound": 0,
                                                 "first": event["date"], "latest": event["date"]})
                row["transactions"].add(group["transaction_hash"])
                row["inbound" if destination == wallet else "outbound"] += 1
                if timestamp(event["date"]) < timestamp(row["first"]): row["first"] = event["date"]
                if timestamp(event["date"]) > timestamp(row["latest"]): row["latest"] = event["date"]
            price = ""
            if event.get("price") is not None:
                price = html.escape(str(event["price"]) + " " + str(event.get("currency") or ""))
                if event.get("usd") is not None:
                    try:
                        price += " / $" + format(Decimal(str(event["usd"])), ".2f")
                    except (InvalidOperation, ValueError):
                        price += " / " + html.escape(str(event["usd"]))
            action = event["action"]
            if action in ("Bought", "Transfer in"):
                relationship, related_wallet = "From", event["sender"]
            elif action in ("Sold", "Transfer out"):
                relationship, related_wallet = "To", event["recipient"]
            else:
                relationship, related_wallet = "To", event["recipient"]
            context = html.escape(event.get("collection_name", ""))
            if event.get("quantity") not in (None, "1"):
                context += " · qty " + html.escape(str(event["quantity"]))
            if price:
                context += " · " + price
            event_rows.append(
                '<tr class="event-row"><td class="key">{}</td><td class="key action">{}</td><td class="key nft">{}</td>'
                '<td class="key relationship">{}</td><td class="key related-wallet">{}</td><td>{}</td></tr>'.format(
                    html.escape(eastern), html.escape(action), html.escape(event["nft_name"]),
                    relationship, party_label(related_wallet, aliases), context))
    summaries = []
    for other, row in sorted(counter.items(), key=lambda item: (-len(item[1]["transactions"]), item[0])):
        first = timestamp(row["first"]).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %Z")
        latest = timestamp(row["latest"]).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %Z")
        summaries.append("<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            len(row["transactions"]), row["inbound"], row["outbound"], html.escape(first), html.escape(latest), party_label(other, aliases)))
    round_trip_rows = []
    for pattern in detect_round_trips(result.get("history", []), wallet):
        first = timestamp(pattern["left_at"]).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p %Z")
        second = timestamp(pattern["returned_at"]).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p %Z")
        sequence = ("Bought from counterparty → sent or sold back" if pattern.get("direction") == "bought_then_out"
                    else "Sent or sold to counterparty → bought back")
        round_trip_rows.append("<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(pattern["nft_name"]), html.escape(sequence), party_label(wallet, aliases),
            party_label(pattern["counterparty"], aliases), html.escape(first + " → " + second)))
    round_trips = ('''<h2>Potential linked-wallet movement patterns</h2><p><strong>pattern requiring review:</strong> an NFT was either sent or sold to a counterparty and later bought back, or bought from this counterparty, then later sent or sold back. This is not proof of common ownership or XP gaming.</p><div class="table-scroll"><table><thead><tr><th>NFT</th><th>Sequence</th><th>Analyzed wallet</th><th>Counterparty</th><th>First → second event</th></tr></thead><tbody>''' + "".join(round_trip_rows) + "</tbody></table></div>") if round_trip_rows else ""
    summary = (f'{html.escape(str(result.get("mode", "saved analysis")))} · New transactions: {int(result.get("new_transaction_count", 0))} · '
               f'Total saved transactions: {int(result.get("saved_transaction_count", len(result.get("history", []))))} · '
               f'Pages / network calls: {int(result.get("pages_fetched", 0))} / {int(result.get("network_calls", 0))} · '
               f'Saved file: {int(result.get("saved_file_size", 0))} bytes · Timestamp: {html.escape(str(result.get("observed_at", "")))} · '
               'Notifications: 0 · LLM tokens: 0')
    return f'''<section class="analysis-report"><style>.analysis-report{{max-width:1750px}}.table-scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px 11px;border-bottom:1px solid #31415f;text-align:left;vertical-align:top}}.key,.asset,.relationship,.related-wallet{{white-space:nowrap}}.nft{{font-weight:700}}code{{white-space:nowrap}}</style>
<p class="zero-output"><strong>No Discord or WhatsApp output.</strong> This private local report sends no notifications. Notifications: 0.</p><p><strong>Analyzed wallet:</strong> {party_label(wallet, aliases)}</p><p>{summary}</p>
<h2>Counterparty Summary</h2><div class="table-scroll"><table><thead><tr><th>Shared transactions</th><th>Inbound events</th><th>Outbound events</th><th>First interaction</th><th>Latest interaction</th><th>Alias / wallet</th></tr></thead><tbody>{''.join(summaries)}</tbody></table></div>
{order_queue_html(result.get('order_queue',{}))}{findings_html(result, aliases)}{sale_evidence_html(result)}{round_trips}<h2>NFT Events</h2><div class="table-scroll"><table><thead><tr><th>Date / Time</th><th>Action</th><th>NFT Name</th><th>Relationship</th><th>Wallet / Alias</th><th>Collection / Sale Context</th></tr></thead><tbody>{''.join(event_rows)}</tbody></table></div></section>'''


def api_key(root: Path) -> str:
    value = os.environ.get("RARIBLE_API_KEY")
    if value:
        return value
    root = Path(root)
    for path in (root / ".env", root.parent / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            match = re.match(r"^(?:export\s+)?RARIBLE_API_KEY\s*=\s*(.*?)\s*$", line.strip())
            if match:
                return match.group(1).strip("\"'")
    raise RuntimeError("RARIBLE_API_KEY is not configured on the local backend")


class OrderRequestError(RuntimeError):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f'Rarible HTTP {code}; stopped without retry')


class RaribleClient:
    def __init__(self, *, root: Path, key: str | None = None, opener=None, clock=None, sleeper=None,
                 progress: Callable[[dict], None] | None = None):
        self.root = Path(root)
        self.key = key or api_key(self.root)
        self.opener = opener or urllib.request.urlopen
        self.clock = clock or time.time
        self.sleep = sleeper or time.sleep
        self.progress = progress or (lambda _event: None)

    def __call__(self, payload: dict) -> dict:
        return self._leased(payload)

    def get_order(self, identity: str) -> dict | None:
        if not valid_order_id(identity):
            raise ValueError('Malformed Polygon order identifier; no request made')
        return self._leased(None, identity)

    def _leased(self, payload: dict | None, identity: str | None = None):
        # Same exclusive file used by the scheduled holder collector. Do not
        # reclaim ambiguous ownership or disturb its recoverable state commits.
        lease = self.root / 'holder-activity-state.lock'
        try:
            fd = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise RuntimeError('Holder monitor locked; retry explicitly later') from None
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump({'pid': os.getpid(), 'started_at': utcnow()}, handle)
            return self._request(payload, identity)
        finally:
            lease.unlink()

    def _request(self, payload: dict | None, identity: str | None = None) -> dict | None:
        pacing = self.root / "holder-activity-pacing.json"
        wait = 0.0
        if pacing.exists():
            finished = float(json.loads(pacing.read_text(encoding="utf-8"))["finished_epoch"])
            wait = max(0.0, MIN_CALL_GAP_SECONDS - (self.clock() - finished))
            if wait:
                self.progress({"stage": "waiting", "message": f"Durable Rarible pacing wait: {wait:.1f} seconds", "pacing_wait": wait})
                self.sleep(wait)
        request = urllib.request.Request('https://api.rarible.org/v0.1/orders/' + identity if identity else ACTIVITIES_API,
            data=None if identity else json.dumps(payload).encode("utf-8"), method="GET" if identity else "POST", headers={
            "X-API-KEY": self.key, "Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Hermes-MTD-Wallet-Analyzer",
        })
        try:
            with self.opener(request, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict):
                raise RuntimeError("Malformed Rarible response")
            return result
        except urllib.error.HTTPError as exc:
            if identity and exc.code == 404:
                return None
            raise OrderRequestError(exc.code) from None
        except urllib.error.URLError:
            raise RuntimeError("Rarible network request failed; stopped without retry") from None
        finally:
            atomic_write(pacing, {"finished_epoch": self.clock()})


def validate_wallet(value: str) -> str:
    value = str(value).strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise ValueError("Wallet must be 0x followed by exactly 40 hexadecimal characters")
    return value.lower()


def resolve_wallet(value: str, aliases: dict) -> str:
    value = str(value).strip()
    if value.lower().startswith('0x'):
        return validate_wallet(value)
    matches = set()
    for row in aliases.get('aliases', []):
        if str(row.get('alias', '')).casefold() == value.casefold() and row.get('wallet'):
            matches.add(validate_wallet(row['wallet']))
    for wallet, alias in aliases.get('exact_aliases', {}).items():
        if str(alias).casefold() == value.casefold():
            matches.add(validate_wallet(wallet))
    if len(matches) != 1:
        raise ValueError('Name Tag must resolve to exactly one saved full wallet; supply the full wallet instead')
    return matches.pop()


def read_state(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {"schema_version": 1, "wallets": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("wallets"), dict):
        raise ValueError("Saved analysis state is malformed")
    return data


def atomic_write(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, separators=(",", ":"), ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _pid_is_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process = kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return ctypes.get_last_error() != 87
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _reclaim_dead_analyzer_lock(path: Path) -> bool:
    owner_path = path / "owner.json"
    try:
        original = owner_path.read_text(encoding="utf-8")
        owner = json.loads(original)
    except (OSError, ValueError):
        return False
    if _pid_is_alive(owner.get("pid")):
        return False
    mutation = path / ".owner-mutation"
    try:
        mutation.mkdir()
    except OSError:
        return False
    quarantine = path.with_name(path.name + ".reclaim-" + secrets.token_hex(8))
    moved = False
    try:
        if owner_path.read_text(encoding="utf-8") != original:
            return False
        os.replace(path, quarantine)
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
        (quarantine / "owner.json").unlink(missing_ok=True)
        (quarantine / mutation.name).rmdir()
        quarantine.rmdir()
    except OSError:
        pass
    return True


@contextmanager
def state_lock(path: Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError:
        if not _reclaim_dead_analyzer_lock(path):
            raise RuntimeError("Wallet transfer analyzer is busy; no state changed")
        try:
            path.mkdir()
        except FileExistsError as exc:
            raise RuntimeError("Wallet transfer analyzer is busy; no state changed") from exc
    token = secrets.token_hex(24)
    owner_path = path / "owner.json"
    try:
        owner_path.write_text(json.dumps({"pid": os.getpid(), "token": token, "started_at": utcnow()}), encoding="utf-8")
        yield
    finally:
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            owner = {}
        if owner.get("token") == token:
            owner_path.unlink(missing_ok=True)
            try:
                path.rmdir()
            except OSError:
                pass


def load_saved_wallet(wallet: str, *, state_path: Path, catalog_path: Path | None = None,
                      counterparties: tuple[str, ...] = ()) -> dict:
    wallet = validate_wallet(wallet)
    if catalog_path is not None:
        with state_lock(Path(state_path).with_suffix('.archive.lock')):
            catalog = load_catalog(catalog_path)
            archive = EvidenceArchive(Path(state_path).with_suffix('.sqlite3'))
            archive.seed(read_state(state_path), catalog)
            result = archive.snapshot(wallet, catalog)
            raw = {validate_wallet(other): archive.raw(validate_wallet(other), catalog) for other in counterparties}
            result['findings'] = saved_findings(result, raw, catalog)
            result['counterparty_coverage'] = {other: archive.snapshot(other, catalog)['coverage'] for other in raw}
            broad_path = Path(state_path).parent / 'mtd-transfer-monitor.json'
            try:
                broad = json.loads(broad_path.read_text(encoding='utf-8'))
                window = broad.get('window', {})
                result['broad_monitor_coverage'] = {key: window.get(key) for key in ('start', 'end', 'complete')}
                result['broad_monitor_coverage']['last_complete_check'] = broad.get('last_complete_check')
            except (OSError, ValueError, TypeError):
                result['broad_monitor_coverage'] = {'available': False}
            return result
    saved = read_state(state_path).get("wallets", {}).get(wallet)
    if not saved:
        raise ValueError("No saved analysis exists for this wallet")
    result = dict(saved)
    result.update(mode="saved-only load", network_calls=0, pages_fetched=0, activities_scanned=0,
                  notifications=0, llm_tokens=0, saved_transaction_count=len(saved.get("history", [])),
                  saved_file_size=Path(state_path).stat().st_size)
    return result


def analyze_and_save(wallet: str, *, state_path: Path, lock_path: Path, catalog_path: Path,
                     fetch: Callable[[dict], dict], progress: Callable[[dict], None] | None = None,
                     mode: str = 'incremental', max_pages: int = 3) -> dict:
    wallet = validate_wallet(wallet)
    with state_lock(lock_path), state_lock(Path(state_path).with_suffix('.archive.lock')):
        state = read_state(state_path)
        catalog = load_catalog(catalog_path)
        archive = EvidenceArchive(Path(state_path).with_suffix('.sqlite3'))
        archive.seed(state, catalog)
        result = collect_archive(fetch, wallet, catalog, archive, mode=mode, max_pages=max_pages, progress=progress)
        # Compatibility snapshot only; the SQLite archive is authoritative and never trimmed.
        compact = dict(result, history=result['history'][:HISTORY_LIMIT])
        compact['checkpoint'] = result['coverage'].get('checkpoint') or result['coverage'].get('newest') or utcnow()
        raw = archive.raw(wallet, catalog)
        retained = {g['transaction_hash'] for g in compact['history']}
        compact['recent_raw'] = [r for r in raw if (r.get('transactionHash') or
                                 (r.get('blockchainInfo') or {}).get('transactionHash')) in retained]
        compact['seen_activity_ids'] = [r['id'] for r in compact['recent_raw']]
        state['wallets'][wallet] = compact
        state['updated_at'] = utcnow()
        atomic_write(state_path, state)
        return result


def scope_key(catalog: dict) -> str:
    return hashlib.sha256(json.dumps({'collections': sorted(catalog), 'types': list(TYPES),
                                     'chain': 'POLYGON'}, sort_keys=True).encode()).hexdigest()


class EvidenceArchive:
    """Private additive evidence; callers serialize writes with the analyzer lease."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS order_attempts (
                    order_id TEXT NOT NULL, retrieved_at TEXT NOT NULL, source TEXT NOT NULL,
                    status TEXT NOT NULL, http_status INTEGER, raw TEXT);
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY, raw TEXT, status TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL, source TEXT NOT NULL, http_status INTEGER);
                CREATE TABLE IF NOT EXISTS order_revisions (
                    order_id TEXT NOT NULL, digest TEXT NOT NULL, raw TEXT NOT NULL,
                    first_retrieved TEXT NOT NULL, last_retrieved TEXT NOT NULL, source TEXT NOT NULL,
                    PRIMARY KEY(order_id,digest));
                CREATE TABLE IF NOT EXISTS evidence (
                    wallet TEXT NOT NULL, activity_id TEXT NOT NULL, digest TEXT NOT NULL,
                    raw TEXT NOT NULL, collection TEXT NOT NULL, token_id TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL, PRIMARY KEY(wallet,activity_id));
                CREATE TABLE IF NOT EXISTS revisions (
                    wallet TEXT NOT NULL, activity_id TEXT NOT NULL, digest TEXT NOT NULL,
                    raw TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                    PRIMARY KEY(wallet,activity_id,digest));
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, wallet TEXT NOT NULL, scope TEXT NOT NULL, mode TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT, calls INTEGER NOT NULL,
                    status TEXT NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS imports (wallet TEXT PRIMARY KEY, imported_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS legacy_events (
                    wallet TEXT NOT NULL, identity TEXT NOT NULL, event TEXT NOT NULL,
                    PRIMARY KEY(wallet,identity));
                CREATE TABLE IF NOT EXISTS coverage (
                    wallet TEXT NOT NULL, scope TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(wallet,scope));
            ''')
        self.shared = history_module('mtd_nft_history_store').HistoryStore(self.path)
        self.shared.migrate_legacy()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def status(self, wallet: str, catalog: dict) -> dict:
        with self.connect() as db:
            row = db.execute('SELECT data FROM coverage WHERE wallet=? AND scope=?',
                             (wallet, scope_key(catalog))).fetchone()
        return json.loads(row[0]) if row else {'history_exhausted': False, 'partial': True,
                                             'last_success': None, 'error': None, 'scope': scope_key(catalog)}

    def save_status(self, wallet: str, catalog: dict, status: dict):
        status.update(scope_collections=sorted(catalog), scope_types=list(TYPES), scope_chain='POLYGON')
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO coverage VALUES(?,?,?)',
                       (wallet, scope_key(catalog), json.dumps(status)))

    def store(self, wallet: str, catalog: dict, rows: list[dict], *, status: dict | None = None, _db=None, _legacy=False):
        transactions(rows, wallet, catalog)  # Validate the complete page before any commit.
        now = utcnow()
        with (nullcontext(_db) if _db is not None else self.connect()) as db:
            # Canonical raw payloads are global; this wallet adds observation links only.
            self.shared.ingest(rows, target=wallet, retrieved_at=None if _legacy else now,
                               source=None if _legacy else 'wallet activities/search',
                               provenance={'legacy_table':'compact','imported_at':now,'coverage':'bounded/partial'} if _legacy else None,
                               _db=db)
            if status is not None:
                db.execute('INSERT OR REPLACE INTO coverage VALUES(?,?,?)',
                           (wallet, scope_key(catalog), json.dumps(status)))

    def seed(self, state: dict, catalog: dict):
        """Atomic additive import. Never fabricate raw data or trust old exhaustion flags."""
        with self.connect() as db:
            for wallet, saved in state.get('wallets', {}).items():
                wallet = validate_wallet(wallet)
                if db.execute('SELECT 1 FROM imports WHERE wallet=?', (wallet,)).fetchone():
                    continue
                rows = saved.get('recent_raw', [])
                # Historical scope can differ. Import only the validated current allowlist.
                rows = [r for r in rows if str(((r.get('nft') or {}).get('type') or r).get('collection') or r.get('contract') or '').lower() in catalog]
                status = self.status(wallet, catalog)
                if not status.get('last_attempt'):
                    status.update(source='legacy compact import; discarded raw/mints unknown',
                                  history_exhausted=False, partial=True)
                self.store(wallet, catalog, rows, status=status, _db=db, _legacy=True)
                for group in saved.get('history', []):
                    for event in group.get('events', []):
                        if event.get('collection') not in catalog:
                            continue
                        timestamp(event['date'])
                        validate_wallet(event['sender'])
                        validate_wallet(event['recipient'])
                        if wallet not in (event['sender'], event['recipient']) or not event.get('token_id') or not event.get('activity_ids'):
                            raise ValueError('Malformed legacy event')
                        db.execute('INSERT OR IGNORE INTO legacy_events VALUES(?,?,?)',
                                   (wallet, event['activity_ids'][0], json.dumps(event)))
                db.execute('INSERT INTO imports VALUES(?,?)', (wallet, utcnow()))

    def raw(self, wallet: str, catalog: dict) -> list[dict]:
        from mtd_nft_history_provider import identities
        selected = []
        with self.connect() as db:
            reviewed = {r[0] for r in db.execute("SELECT DISTINCT id FROM nh_review WHERE provider='rarible'")}
        for raw in self.shared.raw_for_target(wallet):
            kind = raw.get('@type')
            if kind not in TYPES or raw.get('reverted') or raw.get('id') in reviewed:
                continue
            parties = (raw.get('seller'), raw.get('buyer')) if kind == 'SELL' else (raw.get('from'), raw.get('owner'))
            if wallet not in tuple(address(p) for p in parties):
                continue
            try:
                keys = identities(raw)
                if len(keys) != 1:
                    continue
                chain, contract, token = next(iter(keys)).split(':')
                collection = (chain + ':' + contract).lower()
                if collection not in catalog:
                    continue
                # Adapt generic NFT identity for the legacy movement classifier only.
                row = dict(raw)
                if kind == 'SELL':
                    nft = dict(row.get('nft') or {})
                    nft['type'] = dict(nft.get('type') or {}, collection=chain + ':' + contract, tokenId=token)
                    row['nft'] = nft
                else:
                    row.update(collection=chain + ':' + contract, tokenId=token)
                transactions([row], wallet, catalog)
                try:
                    transactions([raw], wallet, catalog)
                    row = raw  # Preserve exact raw payload for compact compatibility snapshots.
                except (ValueError, TypeError, KeyError):
                    pass
            except (ValueError, TypeError, KeyError, AttributeError):
                continue  # Retained raw remains available in lifecycle review, not wallet conclusions.
            selected.append(row)
        return sorted(selected, key=lambda r: r['id'])

    def sale_metadata(self, wallet: str, groups: list[dict]) -> list[dict]:
        """Preserve provenance; SELL and two order hashes do not identify execution side."""
        output = []
        with self.connect() as db:
            for group in groups:
                for event in group['events']:
                    if event['type'] != 'SELL':
                        continue
                    identity = event['activity_ids'][0]
                    current = db.execute('''SELECT r.raw,(SELECT max(o.retrieved_at) FROM nh_observations o
                        WHERE o.provider=c.provider AND o.id=c.id AND o.digest=c.digest)
                        FROM nh_current c JOIN nh_revisions r USING(provider,id,digest)
                        WHERE c.provider='rarible' AND c.id=?''', (identity,)).fetchone()
                    if not current:
                        continue
                    revisions = db.execute('''SELECT r.raw,min(o.retrieved_at) FROM nh_revisions r
                        LEFT JOIN nh_observations o USING(provider,id,digest)
                        WHERE r.provider='rarible' AND r.id=? GROUP BY r.digest ORDER BY min(o.retrieved_at),r.digest''',
                        (identity,)).fetchall()
                    raw = json.loads(current[0])
                    output.append({'activity_id': identity, 'nft_name': event['nft_name'],
                                   'classification': 'unknown', 'exposure_seconds': None,
                                   'gap': 'Authoritative matched order side, public/restricted taker, and listing publication/reprice timeline unavailable; order enrichment not implemented.',
                                   'observed_first': revisions[0][1] if revisions else None, 'observed_last': current[1],
                                   'revision_prices': [json.loads(r[0]).get('price') for r in revisions],
                                   'activity_type': raw.get('type'), 'source': raw.get('source'),
                                   'order_fields': {key: raw.get(key) for key in (
                                       'buyerOrderHash', 'sellerOrderHash', 'orderId', 'maker', 'taker', 'side',
                                       'createdAt', 'lastUpdatedAt', 'start', 'end', 'startedAt', 'endedAt', 'date')}})
                    candidates, invalid = activity_order_ids(raw)
                    cached = [db.execute('SELECT raw,status,retrieved_at,source,http_status FROM orders WHERE order_id=?', (oid,)).fetchone() for oid in candidates]
                    output[-1].update(matched_order_evidence(raw, [json.loads(r[0]) for r in cached if r and r[0] and r[1] == 'saved']))
                    output[-1].update(purchased_at=raw['date'], order_lookup_status=[r[1] if r else 'pending' for r in cached],
                                      order_retrieved_at=max([r[2] for r in cached if r], default=None),
                                      order_source='Rarible GET /v0.1/orders/{id}', invalid_order_ids=invalid)
                    if invalid:
                        output[-1]['gap'] += ' Malformed literal order identifiers rejected without repair.'

        return output

    def snapshot(self, wallet: str, catalog: dict) -> dict:
        raw = self.raw(wallet, catalog)
        groups = transactions(raw, wallet, catalog)
        raw_ids = {str(row['id']) for row in self.shared.raw_for_target(wallet)}
        mint_txs = {str(row.get('transactionHash') or (row.get('blockchainInfo') or {}).get('transactionHash'))
                    for row in raw if row.get('@type') == 'MINT' and not row.get('reverted')}
        with self.connect() as db:
            legacy = [json.loads(row[0]) for row in db.execute('SELECT event FROM legacy_events WHERE wallet=?', (wallet,))]
        legacy = [event for event in legacy if event['collection'] in catalog and
                  not raw_ids.intersection(event['activity_ids']) and event['transaction_hash'] not in mint_txs]
        by_tx = {group['transaction_hash']: group for group in groups}
        for event in legacy:
            group = by_tx.setdefault(event['transaction_hash'], {'transaction_hash': event['transaction_hash'],
                                                               'date': event['date'], 'events': []})
            # Matching newer raw representations supersede legacy normalized events.
            key = lambda e: (e['collection'], e['token_id'], e['sender'], e['recipient'], e['quantity'])
            if not any(key(existing) == key(event) for existing in group['events']):
                group['events'].append(event)
        groups = sorted(by_tx.values(), key=lambda g: (timestamp(g['date']), g['transaction_hash']), reverse=True)
        coverage = self.status(wallet, catalog)
        coverage['legacy_event_count'] = len(legacy)
        dates = [row['date'] for row in raw] + [event['date'] for event in legacy]
        with self.connect() as db:
            revisions = sum(db.execute("SELECT count(*) FROM nh_revisions WHERE provider='rarible' AND id=?", (row['id'],)).fetchone()[0] for row in raw)
        coverage.update(oldest=min(dates, key=timestamp) if dates else None,
                        newest=max(dates, key=timestamp) if dates else None,
                        raw_activity_count=len(raw), revision_count=revisions,
                        scope_collections=sorted(catalog), display_limit=HISTORY_LIMIT,
                        stale=not coverage.get('last_success') or
                        timestamp(coverage['last_success']) < datetime.now(timezone.utc) - timedelta(hours=24))
        purchase_ids = {e['activity_ids'][0] for g in groups for e in g['events'] if e['action'] == 'Bought'}
        order_ids = {oid for row in raw if row['id'] in purchase_ids for oid in activity_order_ids(row)[0]}
        with self.connect() as db:
            order_states = {r[0]: r[1] for r in db.execute('SELECT order_id,status FROM orders')}
        order_progress = {state: sum(order_states.get(oid) == state for oid in order_ids) for state in ('saved', 'missing')}
        order_progress.update(errors=sum(order_states.get(oid) == 'error' for oid in order_ids),
                              remaining=sum(oid not in order_states for oid in order_ids),
                              total=len(order_ids))
        return {'wallet': wallet, 'history': groups, 'coverage': coverage, 'order_progress': order_progress,
                'order_queue': self.shared.order_queue_status(wallet),
                'sale_evidence': self.sale_metadata(wallet, groups),
                'saved_transaction_count': len(groups), 'network_calls': 0, 'pages_fetched': 0,
                'notifications': 0, 'llm_tokens': 0, 'saved_file_size': self.path.stat().st_size,
                'mode': 'saved-only load'}


def activity_order_ids(activity: dict) -> tuple[list[str], int]:
    identities = []
    invalid = 0
    for key in ('orderId', 'sellerOrderHash', 'buyerOrderHash'):
        value = activity.get(key)
        if value is None:
            continue
        identity = value if key == 'orderId' else 'POLYGON:' + str(value)
        if valid_order_id(identity):
            if identity not in identities:
                identities.append(identity)
        else:
            invalid += 1
    return identities, invalid


def queue_saved_purchase_orders(wallet: str, catalog: dict, archive: EvidenceArchive) -> dict:
    """Snapshot all retained confirmed secondary purchases, never the display slice."""
    wallet=validate_wallet(wallet)
    snapshot=archive.snapshot(wallet,catalog)
    purchases={e['activity_ids'][0] for g in snapshot['history'] for e in g['events'] if e['action']=='Bought'}
    identities=sorted({oid for row in archive.raw(wallet,catalog) if row['id'] in purchases for oid in activity_order_ids(row)[0]})
    return archive.shared.enqueue_orders(wallet,identities,scope=scope_key(catalog),coverage=snapshot['coverage'])


def enrich_saved_orders(fetch: Callable, wallet: str, catalog: dict, archive: EvidenceArchive, *,
                        max_calls: int = 3, progress: Callable | None = None, retry_missing: bool = False) -> dict:
    """Only saved secondary purchases; successful cache rows form the durable cursor."""
    wallet = validate_wallet(wallet)
    if type(max_calls) is not int or not 1 <= max_calls <= 5:
        raise ValueError('Choose 1 to 5 order calls')
    report = progress or (lambda _event: None)
    purchases = {e['activity_ids'][0] for g in archive.snapshot(wallet, catalog)['history']
                 for e in g['events'] if e['action'] == 'Bought'}
    identities = sorted({oid for row in archive.raw(wallet, catalog) if row['id'] in purchases
                         for oid in activity_order_ids(row)[0]})
    with archive.connect() as db:
        cached = {row[0]: row[1] for row in db.execute('SELECT order_id,status FROM orders')}
    pending = [oid for oid in identities if oid not in cached or (retry_missing and cached[oid] in ('missing', 'error'))]
    calls = 0
    error = 'Previous order lookup failed; use explicit Retry Missing / Failed Orders' if not retry_missing and any(cached.get(oid) == 'error' for oid in identities) else None
    run_id = secrets.token_hex(16)
    with archive.connect() as db:
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                   (run_id, wallet, scope_key(catalog), 'orders', utcnow(), None, 0, 'running', None))
    for identity in ([] if error else pending[:max_calls]):
        report({'stage': 'orders', 'message': 'Looking up saved purchase order', 'network_calls': calls,
                'orders_remaining': len(pending)-calls, 'pacing_wait': 0})
        calls += 1
        raw = None
        http_status = None
        try:
            raw = fetch(identity)
            http_status = 404 if raw is None else 200
            if raw is None:
                state = 'missing'
            elif not isinstance(raw, dict) or raw.get('id') != identity:
                raise ValueError('Provider order identity mismatch')
            else:
                state = 'saved'
        except Exception as exc:
            state = 'error'
            http_status = getattr(exc, 'code', http_status)
            error = 'Order response identity / validation failure' if isinstance(exc, (ValueError, TypeError, KeyError)) else 'Provider order request failed; explicit retry required'
        encoded = json.dumps(raw, sort_keys=True, separators=(',', ':')) if raw is not None else None
        now = utcnow()
        source = 'https://api.rarible.org/v0.1/orders/' + identity
        with archive.connect() as db:
            db.execute('INSERT INTO order_attempts VALUES(?,?,?,?,?,?)', (identity, now, source, state, http_status, encoded))
            db.execute('INSERT OR REPLACE INTO orders VALUES(?,?,?,?,?,?)', (identity, encoded, state, now, source, http_status))
            if encoded is not None:
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                db.execute('INSERT INTO order_revisions VALUES(?,?,?,?,?,?) ON CONFLICT(order_id,digest) DO UPDATE SET last_retrieved=excluded.last_retrieved',
                           (identity, digest, encoded, now, now, source))
        cached[identity] = state
        report({'stage': 'orders saved', 'message': 'Order lookup evidence saved; ' + state,
                'network_calls': calls, 'orders_remaining': sum(oid not in cached for oid in identities), 'pacing_wait': 0})
        if error:
            break
    counts = {state: sum(cached.get(oid) == state for oid in identities) for state in ('saved', 'missing', 'error')}
    remaining = sum(oid not in cached for oid in identities)
    with archive.connect() as db:
        db.execute('UPDATE runs SET finished_at=?,calls=?,status=?,error=? WHERE id=?',
                   (utcnow(), calls, 'error' if error else 'partial' if remaining else 'complete', error, run_id))
    result = archive.snapshot(wallet, catalog)
    result.update(network_calls=calls, mode='saved purchase order enrichment', action_error=error,
                  order_progress={'calls': calls, 'remaining': remaining, 'cached': len(identities)-remaining,
                                  'saved': counts['saved'], 'missing': counts['missing'], 'errors': counts['error']})
    return result


def collect_archive(fetch: Callable, wallet: str, catalog: dict, archive: EvidenceArchive, *,
                    mode: str = 'incremental', max_pages: int = 3, progress: Callable | None = None) -> dict:
    """One user-started bounded run. No retries, crawling, notifications or model calls."""
    wallet = validate_wallet(wallet)
    if mode not in ('incremental', 'backfill') or type(max_pages) is not int or not 1 <= max_pages <= 5:
        raise ValueError('Choose incremental or backfill and 1 to 5 pages')
    report = progress or (lambda _event: None)
    status = archive.status(wallet, catalog)
    track = dict(status.get(mode) or {})
    if track.get('done'):
        track = {} if mode == 'incremental' else track
    calls = scanned = completed_pages = 0
    status['run_count'] = status.get('run_count', 0) + 1
    status['last_attempt'] = utcnow()
    status['error'] = None
    status['last_mode'] = mode
    archive.save_status(wallet, catalog, status)
    run_id = secrets.token_hex(16)
    with archive.connect() as db:
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                   (run_id, wallet, scope_key(catalog), mode, utcnow(), None, 0, 'running', None))
    try:
        if not track.get('done'):
            if not track:
                track = {'cursor': None, 'seen': [], 'last_date': None, 'done': False,
                         'cutoff': (timestamp(status['checkpoint']) - timedelta(seconds=OVERLAP_SECONDS)).isoformat()
                         if mode == 'incremental' and status.get('checkpoint') else None,
                         'newest': None}
            for page_number in range(1, max_pages + 1):
                payload = {'size': 100, 'sort': 'LATEST', 'filter': {
                    'blockchains': ['POLYGON'], 'types': list(TYPES),
                    'collections': [catalog[key]['id'] for key in sorted(catalog)],
                    'users': {'any': ['ETHEREUM:' + wallet]}}}
                if track.get('cursor'):
                    payload['cursor'] = track['cursor']
                report({'stage': 'fetching', 'message': f'{mode}: page {page_number} of at most {max_pages}',
                        'page': page_number, 'network_calls': calls, 'activities_scanned': scanned, 'pacing_wait': 0})
                calls += 1
                page = fetch(payload)
                rows = page.get('activities')
                if not isinstance(rows, list):
                    raise ValueError('Malformed activities response')
                transactions(rows, wallet, catalog)
                last_date = track.get('last_date')
                for row in rows:
                    if last_date and timestamp(row['date']) > timestamp(last_date):
                        raise ValueError('Activities are not newest-first')
                    last_date = row['date']
                cursor = page.get('cursor')
                if cursor and (not isinstance(cursor, str) or not rows or cursor in track['seen'] or cursor == track.get('cursor')):
                    raise ValueError('Empty or repeated pagination cursor')
                next_track = dict(track, cursor=cursor, last_date=last_date,
                                  seen=track['seen'] + ([cursor] if cursor else []))
                dates = [row['date'] for row in rows] + ([track['newest']] if track.get('newest') else [])
                next_track['newest'] = max(dates, key=timestamp) if dates else None
                reached_overlap = bool(track.get('cutoff') and last_date and timestamp(last_date) < timestamp(track['cutoff']))
                next_track['done'] = not cursor or reached_overlap
                status[mode] = next_track
                if not cursor:
                    status['history_exhausted'] = True
                status['partial'] = not status.get('history_exhausted', False) or not next_track['done']
                status['last_success'] = utcnow()
                if next_track['done'] and next_track.get('newest'):
                    candidates = [next_track['newest']] + ([status['checkpoint']] if status.get('checkpoint') else [])
                    status['checkpoint'] = max(candidates, key=timestamp)
                archive.store(wallet, catalog, rows, status=status)
                completed_pages += 1
                track = next_track
                scanned += len(rows)
                report({'stage': 'saved', 'message': 'Validated page and resume checkpoint saved',
                        'page': page_number, 'network_calls': calls, 'activities_scanned': scanned,
                        'transactions_found': len(archive.snapshot(wallet, catalog)['history']), 'pacing_wait': 0})
                if track['done']:
                    break
    except Exception as exc:
        # Never include provider bodies, URLs, or private addresses in public errors.
        status = archive.status(wallet, catalog)
        status.update(error=('Pagination / validation failure' if isinstance(exc, (ValueError, TypeError, KeyError))
                             else 'Provider request failure; resume available'), partial=True)
        archive.save_status(wallet, catalog, status)
    result = archive.snapshot(wallet, catalog)
    coverage = result['coverage']
    with archive.connect() as db:
        db.execute('UPDATE runs SET finished_at=?,calls=?,status=?,error=? WHERE id=?',
                   (utcnow(), calls, 'error' if coverage.get('error') else 'partial' if not track.get('done') else 'complete',
                    coverage.get('error'), run_id))
    result.update(mode=mode, network_calls=calls, pages_fetched=completed_pages, activities_scanned=scanned,
                  observed_at=utcnow())
    return result


def persistent_token(path: Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
    except FileExistsError:
        pass
    value = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", value):
        raise RuntimeError("Analyzer CSRF secret is invalid")
    return value


def make_server(*, port: int = 8774, csrf_path: Path = DEFAULT_CSRF, aliases_path: Path = DEFAULT_ALIASES,
                state_path: Path = DEFAULT_STATE, lock_path: Path = DEFAULT_LOCK, catalog_path: Path = DEFAULT_CATALOG,
                analyze_fn: Callable[[str, Callable[[dict], None]], dict] | None = None,
                saved_fn: Callable[[str], dict] | None = None, collect_fn: Callable | None = None,
                order_fetch_fn: Callable | None = None) -> ThreadingHTTPServer:
    page_style = ':root{color-scheme:dark}*{box-sizing:border-box}body{font:15px system-ui;max-width:1750px;margin:0;padding:28px;background:#0b1220;color:#e7eef8}h1{margin:0 0 8px}.notice{padding:13px 15px;background:#13233b;border:1px solid #31527c;border-radius:8px}form{display:flex;gap:10px;align-items:end;margin:20px 0;padding:17px;background:#111c2f;border:1px solid #2a3e60;border-radius:9px}label{display:grid;gap:6px;flex:1;font-weight:700}input,button{font:inherit;padding:11px;border:1px solid #456083;border-radius:7px;background:#0d1728;color:#fff}button{cursor:pointer;background:#1f6feb;font-weight:750}button.secondary{background:#263750}.progress{padding:13px 15px;border:1px solid #31527c;border-radius:8px;background:#10233d}.progress[hidden]{display:none}.error{color:#ffb4b4}@media(max-width:800px){form{display:grid}}'
    csrf = persistent_token(csrf_path)
    jobs: dict[str, dict] = {}
    jobs_lock = threading.Lock()
    active_lock = threading.Lock()

    def aliases() -> dict:
        path = Path(aliases_path)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def live(wallet: str, progress: Callable[[dict], None], mode: str = 'incremental', max_pages: int = 3,
             other: str | None = None, target: str = 'wallet') -> dict:
        if mode in ('orders', 'retry_orders'):
            with state_lock(lock_path), state_lock(Path(state_path).with_suffix('.archive.lock')):
                catalog = load_catalog(catalog_path)
                archive = EvidenceArchive(Path(state_path).with_suffix('.sqlite3'))
                archive.seed(read_state(state_path), catalog)
                # Construct credentials/client only when an uncached lookup is actually made.
                clients = []
                def get_order(identity):
                    if order_fetch_fn:
                        return order_fetch_fn(identity)
                    if not clients:
                        clients.append(RaribleClient(root=Path(state_path).parent, progress=progress))
                    return clients[0].get_order(identity)
                return enrich_saved_orders(get_order, wallet, catalog, archive, max_calls=max_pages,
                                           progress=progress, retry_missing=mode == 'retry_orders')
        selected = other if target == 'counterparty' else wallet
        if collect_fn:
            outcome = collect_fn(selected, progress, mode, max_pages)
        elif analyze_fn:
            outcome = analyze_fn(selected, progress)
        else:
            client = RaribleClient(root=PRICE_WATCHES, progress=progress)
            outcome = analyze_and_save(selected, state_path=state_path, lock_path=lock_path, catalog_path=catalog_path,
                                       fetch=client, progress=progress, mode=mode, max_pages=max_pages)
        if analyze_fn and not collect_fn:
            return outcome  # Existing dependency-injected fixtures.
        result = saved(wallet, other)
        result.update(mode=mode + ' · target: ' + ('selected counterparty' if target == 'counterparty' else 'analyzed wallet'),
                      network_calls=outcome.get('network_calls', 0), pages_fetched=outcome.get('pages_fetched', 0),
                      activities_scanned=outcome.get('activities_scanned', 0), observed_at=outcome.get('observed_at'))
        result['action_error'] = outcome.get('coverage', {}).get('error')
        return result

    def saved(wallet: str, other: str | None = None) -> dict:
        return saved_fn(wallet) if saved_fn else load_saved_wallet(wallet, state_path=state_path,
                catalog_path=catalog_path, counterparties=(other,) if other else ())

    def orders_action(fields):
        wallet=resolve_wallet(fields.get('wallet',''),aliases())
        action=fields.get('action','saved')
        batch=int(fields['batch']) if fields.get('batch') else None
        with state_lock(Path(state_path).with_suffix('.archive.lock')):
            catalog=load_catalog(catalog_path)
            archive=EvidenceArchive(Path(state_path).with_suffix('.sqlite3'))
            if action=='enqueue':
                archive.seed(read_state(state_path),catalog)
                queue=queue_saved_purchase_orders(wallet,catalog,archive)
            elif action in ('pause','resume'):
                archive.shared.queue_control(action)
                queue=archive.shared.order_queue_status(wallet,batch=batch)
            elif action=='retry': queue=archive.shared.retry_orders(wallet,batch=batch)
            elif action=='saved': queue=archive.shared.order_queue_status(wallet,batch=batch)
            else: raise ValueError('Unknown order queue action')
        return {'queue':queue,'result_html':order_queue_html(queue),'network_calls':0,'notifications':0,'llm_tokens':0}

    def nft_saved(fields):
        action = history_module('mtd_nft_history_ui').action
        with state_lock(Path(state_path).with_suffix('.archive.lock')):
            catalog = load_catalog(catalog_path)
            archive = EvidenceArchive(Path(state_path).with_suffix('.sqlite3'))
            archive.seed(read_state(state_path), catalog)
            return action(archive.shared, fields, catalog, aliases())

    def start_job(wallet: str, mode: str = 'incremental', max_pages: int = 3,
                  other: str | None = None, target: str = 'wallet') -> str:
        if not active_lock.acquire(blocking=False):
            raise RuntimeError("Another wallet analysis is already running")
        job_id = secrets.token_urlsafe(18)
        with jobs_lock:
            jobs.clear()
            jobs[job_id] = {"status": "queued", "stage": "queued", "message": "Queued for local analysis",
                            "page": 0, "network_calls": 0, "activities_scanned": 0, "transactions_found": 0,
                            "pacing_wait": 0, "started_epoch": time.time()}

        def worker():
            def progress(event: dict):
                with jobs_lock:
                    if job_id in jobs:
                        jobs[job_id].update(event)
                        jobs[job_id]["status"] = "running"
                        jobs[job_id]["elapsed"] = max(0, time.time() - jobs[job_id]["started_epoch"])
            try:
                result = live(wallet, progress, mode, max_pages, other, target)
                rendered = render_report(result, aliases())
                with jobs_lock:
                    incomplete = bool(result.get('action_error'))
                    jobs[job_id].update(status='complete', stage='partial/error' if incomplete else 'complete',
                                        message='Run stopped with error; saved evidence retained' if incomplete else 'Bounded run finished; inspect coverage (may be partial)',
                                        result_html=rendered, network_calls=result.get("network_calls", 0),
                                        page=result.get("pages_fetched", 0), transactions_found=len(result.get("history", [])),
                                        elapsed=max(0, time.time() - jobs[job_id]["started_epoch"]))
            except Exception as exc:
                with jobs_lock:
                    jobs[job_id].update(status="error", stage="error", message=str(exc)[:500] or "Analysis failed",
                                        elapsed=max(0, time.time() - jobs[job_id]["started_epoch"]))
            finally:
                active_lock.release()
        threading.Thread(target=worker, daemon=True, name="mtd-wallet-analysis-" + job_id[:8]).start()
        return job_id

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def allowed_host(self) -> bool:
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def origin_ok(self) -> bool:
            value = self.headers.get("Origin")
            return value in (None, "null", f"http://127.0.0.1:{self.server.server_port}")

        def headers_common(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")

        def json_response(self, value: dict, code: int = 200):
            payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.headers_common()
            self.end_headers()
            self.wfile.write(payload)

        def page(self, report_html: str = "", error: str = "", code: int = 200):
            error_html = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
            document = f'''<!doctype html><html><head><meta charset="utf-8"><title>MTD Wallet / Transfers</title><style>
{page_style}
#analyzer-form{{flex-wrap:wrap}}#analyzer-form label{{min-width:210px}}
</style></head><body><nav><a href="http://127.0.0.1:8765/" target="_top">← MTD Control Panel</a> · <a href="/nft-history">Open NFT History</a> · <a href="/wallet-graph">Open Wallet Graph</a></nav><h1>Wallet / Transfers</h1><nav><a href="/nft-history">Open NFT History</a> · <a href="/wallet-graph">Open Wallet Graph</a></nav><p class="notice"><strong>Private loopback report. No Discord or WhatsApp output.</strong> No notifications are sent; notifications remain 0 and LLM tokens remain 0.</p>
<form id="analyzer-form" method="post" action="/analyze" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}"><label>Polygon wallet / exact Name Tag<input name="wallet" placeholder="Full wallet or unique saved alias" required></label><label>Selected counterparty (optional)<input name="counterparty" placeholder="e.g. DB_Ruckus or full wallet"></label><label>Live target<select name="target"><option value="wallet">Analyzed wallet only</option><option value="counterparty">Selected counterparty only</option></select></label><label>API-call/page budget (1–5)<input name="max_pages" type="number" min="1" max="5" value="3" required></label><button id="analyze-button" type="submit">Analyze / Update Wallet</button><button type="submit" name="mode" value="backfill" formaction="/backfill">Backfill Selected Target</button><button type="submit" formaction="/order-queue" name="action" value="enqueue">Queue Pending Purchase Orders</button><button type="submit" formaction="/order-queue" name="action" value="saved" class="secondary">View Saved Order Queue</button><button type="submit" formaction="/order-queue" name="action" value="pause" class="secondary">Pause All Queue</button><button type="submit" formaction="/order-queue" name="action" value="resume" class="secondary">Resume All Queue</button><button type="submit" formaction="/order-queue" name="action" value="retry" class="secondary">Retry Queued Missing / Failed Orders</button><button type="submit" formaction="/enrich">Enrich Saved Purchases Once</button><button class="secondary" type="submit" formaction="/retry-orders">Retry Missing / Failed Orders</button><button class="secondary" type="submit" formaction="/load" formmethod="post">Load Saved Wallet</button></form><p>Queue Pending Purchase Orders snapshots only this wallet’s saved secondary-purchase order IDs across all retained evidence. The existing no-agent scheduler continues without repeated clicks: one API call per scheduled tick, sharing its total budget with NFT history and ownership discovery. Queueing never resumes a paused queue. No new wallet activity, NFT history tasks, or notifications are created. Pause / Resume All Queue affects the shared queue; authentication failures require explicit retry after remediation. The 1–5 budget above applies only to manual one-shot actions.</p><p>Load Saved Wallet re-renders saved evidence only. Live update or backfill: one explicitly selected wallet, at most 5 serial API calls per click, resume next click. Manual activity actions do not continue automatically or crawl counterparties. Provider pagination exhaustion is not proof of lifetime completeness. Enrich Saved Purchases looks up only saved secondary purchases for the analyzed wallet, at most the selected number (1–5) of serial order calls; click again to resume. Saved orders and missing (404) results are cached permanently; retry missing / failed orders explicitly. No public listing or reprice history is fetched. No payment tracing.</p>{error_html}
<div id="progress" class="progress" role="status" aria-live="polite" hidden><strong id="progress-title">Preparing</strong><span id="progress-detail"></span></div><div id="order-queue-result" aria-live="polite"></div><div id="results">{report_html}</div>
<script>
const form=document.getElementById("analyzer-form"),button=document.getElementById("analyze-button"),box=document.getElementById("progress"),title=document.getElementById("progress-title"),detail=document.getElementById("progress-detail"),results=document.getElementById("results");
async function readJson(response){{const text=await response.text();try{{return JSON.parse(text)}}catch(_e){{throw new Error("Analyzer endpoint returned HTTP "+response.status+" instead of JSON")}}}}
function show(state,started){{box.hidden=false;title.textContent=state.status==="complete"?"Analysis complete":state.status==="error"?"Analysis failed":"Analyzing wallet";const elapsed=state.elapsed!==undefined?Math.floor(state.elapsed):Math.floor((Date.now()-started)/1000);detail.textContent=[state.message||state.stage,"Stage: "+(state.stage||"queued"),"Page: "+(state.page||0),"Network calls: "+(state.network_calls||0),"Activities scanned: "+(state.activities_scanned||0),"Transactions found: "+(state.transactions_found||0),"Pacing wait: "+(state.pacing_wait||0)+"s","Elapsed: "+elapsed+"s"].join(" · ")}}
async function poll(job,started){{while(true){{const response=await fetch("/status?job="+encodeURIComponent(job),{{cache:"no-store"}});const state=await readJson(response);show(state,started);if(state.status==="complete"){{results.innerHTML=state.result_html;button.disabled=false;return}}if(state.status==="error"){{button.disabled=false;return}}await new Promise(resolve=>setTimeout(resolve,1000))}}}}
let orderGeneration=0;
function queueProgress(data){{document.getElementById('order-queue-result').innerHTML=data.result_html;}}
async function pollOrderQueue(wallet,batch,generation){{if(generation!==orderGeneration)return;try{{const response=await fetch('/order-queue-status?'+new URLSearchParams({{wallet:wallet,batch:String(batch||'')}}),{{cache:'no-store'}});const data=await readJson(response);if(!response.ok)throw new Error(data.error||'Saved order queue unavailable');if(generation!==orderGeneration)return;queueProgress(data);}}catch(error){{document.getElementById('order-queue-result').textContent=error.message;}}if(generation===orderGeneration)setTimeout(()=>pollOrderQueue(wallet,batch,generation),5000);}}
async function submitOrderQueue(wallet,csrfValue,action){{const generation=++orderGeneration;try{{const response=await fetch('/order-queue',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded','X-MTD-ANALYZER-CSRF':csrfValue}},body:new URLSearchParams({{wallet:wallet,csrf:csrfValue,action:action}})}});const data=await readJson(response);if(!response.ok)throw new Error(data.error||'Order queue action failed');queueProgress(data);setTimeout(()=>pollOrderQueue(wallet,data.queue.batch,generation),5000);}}catch(error){{document.getElementById('order-queue-result').textContent=error.message;}}}}
form.addEventListener("submit",async event=>{{event.preventDefault();const wallet=form.querySelector('[name="wallet"]').value.trim();const csrfValue=form.querySelector('[name="csrf"]').value;const saved=event.submitter&&event.submitter.getAttribute("formaction")==="/load";const action=event.submitter?.getAttribute("formaction");if(action==='/order-queue'){{await submitOrderQueue(wallet,csrfValue,event.submitter.value||'saved');return;}}++orderGeneration;const mode=action==="/enrich"?"orders":action==="/retry-orders"?"retry_orders":action==="/backfill"?"backfill":"incremental";const body=new URLSearchParams({{wallet:wallet,csrf:csrfValue,counterparty:form.querySelector('[name="counterparty"]').value.trim(),target:form.querySelector('[name="target"]').value,max_pages:form.querySelector('[name="max_pages"]').value,mode:mode}});const started=Date.now();button.disabled=true;try{{const response=await fetch(saved?"/load":"/start",{{method:"POST",headers:{{"Content-Type":"application/x-www-form-urlencoded","X-MTD-ANALYZER-CSRF":csrfValue}},body:body}});if(saved){{const text=await response.text();const doc=new DOMParser().parseFromString(text,"text/html");if(!response.ok)throw new Error(doc.querySelector('.error')?.textContent||"Saved load failed: HTTP "+response.status);results.innerHTML=doc.getElementById('results').innerHTML;show({{status:"complete",stage:"saved-only",message:"Saved evidence loaded; network calls 0"}},started);button.disabled=false;return}}const state=await readJson(response);if(!response.ok)throw new Error(state.error||"Unable to start analysis");show(state,started);await poll(state.job_id,started)}}catch(error){{show({{status:"error",stage:"error",message:error.message}},started);button.disabled=false}}}});
</script></body></html>'''
            self.html_response(document, code)

        def nft_page(self):
            panel = history_module('mtd_nft_history_ui').panel
            document = f'''<!doctype html><html><head><meta charset="utf-8"><title>MTD NFT History</title><style>{page_style}
body{{max-width:none;width:100%}}nav a{{color:#9dc5ff}}
</style></head><body><nav><a href="http://127.0.0.1:8765/" target="_top">← MTD Control Panel</a> · <a href="/wallet-transfers">Open Wallet / Transfers</a> · <a href="/wallet-graph">Open Wallet Graph</a></nav><h1>NFT History</h1><nav><a href="/wallet-transfers">Open Wallet / Transfers</a> · <a href="/wallet-graph">Open Wallet Graph</a></nav>
<p class="notice">Shared saved NFT history and queue. Find current NFTs in a wallet, or view one exact NFT. Wallet transfer analysis is a separate form. No Discord or WhatsApp output.</p>
{panel(csrf)}</body></html>'''
            self.html_response(document)

        def html_response(self, document: str, code: int = 200):
            payload = document.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; frame-ancestors http://127.0.0.1:8765")
            self.headers_common()
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self.allowed_host():
                if urlparse(self.path).path == '/nft-status': self.json_response({'error':'Request rejected by local security checks'},403)
                else: self.send_error(403)
                return
            parsed = urlparse(self.path)
            if parsed.path == '/order-queue-status':
                if not self.origin_ok():
                    self.json_response({'error':'Request rejected by local security checks'},403)
                    return
                try:
                    fields={key:values[0] for key,values in parse_qs(parsed.query).items()}
                    fields['action']='saved'
                    self.json_response(orders_action(fields))
                except ValueError as exc: self.json_response({'error':str(exc)},400)
                except Exception: self.json_response({'error':'Saved order queue busy; retry after active local write completes'},503)
                return
            if parsed.path == '/nft-status':
                if not self.origin_ok():
                    self.json_response({'error':'Request rejected by local security checks'},403)
                    return
                try:
                    self.json_response(nft_saved({'action':'saved','asset':parse_qs(parsed.query).get('asset',[''])[0],'discovery_job':parse_qs(parsed.query).get('discovery_job',[''])[0]}))
                except ValueError as exc: self.json_response({'error':str(exc)},400)
                except Exception: self.json_response({'error':'Saved NFT queue unavailable; retry after active local write completes'},503)
                return
            if parsed.path == "/health":
                self.json_response({"ok": True, "service": "mtd-wallet-transfer-analyzer", "network_calls": 0,
                                    "notifications": 0, "llm_tokens": 0})
                return
            if parsed.path == "/status":
                job_id = parse_qs(parsed.query).get("job", [""])[0]
                with jobs_lock:
                    state = dict(jobs.get(job_id) or {})
                if not state:
                    self.json_response({"error": "Unknown or expired analysis"}, 404)
                    return
                state.pop("started_epoch", None)
                self.json_response(state)
                return
            if parsed.path == "/wallet-graph":
                self.html_response(history_module('mtd_wallet_graph_ui').page(csrf))
                return
            if parsed.path == "/nft-history":
                self.nft_page()
                return
            if parsed.path not in ("/", "/wallet-transfers"):
                self.send_error(404)
                return
            self.page()

        def graph_post(self):
            """Saved-only JSON path, deliberately independent of archive writers."""
            if not self.allowed_host() or not self.origin_ok():
                self.json_response({'error': 'Request rejected by local security checks'}, 403)
                return
            try:
                if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                    raise ValueError('Content-Type must be application/json')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise ValueError('Invalid request size')
                try:
                    fields = json.loads(self.rfile.read(length).decode('utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ValueError('Request must contain valid UTF-8 JSON') from None
                if not isinstance(fields, dict):
                    raise ValueError('JSON body must be an object')
                supplied = self.headers.get('X-MTD-ANALYZER-CSRF') or fields.get('csrf', '')
                if not isinstance(supplied, str):
                    raise ValueError('CSRF value must be text')
                if not secrets.compare_digest(supplied, csrf):
                    self.json_response({'error': 'Analyzer session expired; reload the page'}, 403)
                    return
                wallets = fields.get('wallets')
                if not isinstance(wallets, str):
                    raise ValueError('wallets must be multiline or comma-separated text')
                inputs = [value.strip() for value in re.split(r'[\n,]', wallets) if value.strip()]
                if not 2 <= len(inputs) <= 50:
                    raise ValueError('Choose 2–50 wallets or unique saved aliases')
            except (ValueError, TypeError):
                # Never reflect submitted wallet text or malformed JSON back into errors.
                self.json_response({'error': 'Invalid saved graph input: use application/json, a text CSRF value, and 2–50 wallets or unique saved aliases'}, 400)
                return
            try:
                result = history_module('mtd_wallet_graph').build_graph(
                    Path(state_path).with_suffix('.sqlite3'), inputs,
                    aliases=aliases(), catalog=load_catalog(catalog_path))
                self.json_response(result)
            except ValueError:
                self.json_response({'error': 'Wallet input could not be resolved uniquely or validated. Use exact saved aliases or full Polygon wallet addresses; choose 2–50 distinct wallets.'}, 400)
            except Exception:
                self.json_response({'error': 'Saved graph archive unavailable or busy; retry the saved query later. No live lookup was made.'}, 503)

        def do_POST(self):
            if self.path == '/wallet-graph-data':
                self.graph_post()
                return
            async_request = self.path in ('/start','/nft-action','/order-queue')
            if not self.allowed_host() or not self.origin_ok() or self.path not in {"/order-queue", "/nft-action", "/start", "/analyze", "/backfill", "/load", "/enrich", "/retry-orders"}:
                if async_request:
                    self.json_response({"error": "Request rejected by local security checks"}, 403)
                else:
                    self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise ValueError("Invalid request size")
                data = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                supplied = self.headers.get("X-MTD-ANALYZER-CSRF") or data.get("csrf", [""])[0]
                if not secrets.compare_digest(supplied, csrf):
                    if async_request: self.json_response({"error": "Analyzer session expired; reload the page"}, 403)
                    else: self.send_error(403)
                    return
                if self.path == '/order-queue':
                    self.json_response(orders_action({key:values[0] for key,values in data.items()}))
                    return
                if self.path == '/nft-action':
                    self.json_response(nft_saved({key:values[0] for key,values in data.items()}))
                    return
                current_aliases = aliases()
                wallet = resolve_wallet(data.get('wallet', [''])[0], current_aliases)
                other_value = data.get('counterparty', [''])[0].strip()
                other = resolve_wallet(other_value, current_aliases) if other_value else None
                if self.path == '/load':
                    self.page(render_report(saved(wallet, other), aliases()))
                    return
                mode = {'/backfill': 'backfill', '/enrich': 'orders', '/retry-orders': 'retry_orders'}.get(self.path, data.get('mode', ['incremental'])[0])
                max_pages = int(data.get('max_pages', ['3'])[0])
                target = data.get('target', ['wallet'])[0]
                if mode not in ('backfill', 'incremental', 'orders', 'retry_orders') or not 1 <= max_pages <= 5:
                    raise ValueError('Choose incremental or backfill and 1 to 5 pages')
                if mode in ('orders', 'retry_orders') and target != 'wallet':
                    raise ValueError('Order enrichment uses the analyzed wallet only; set live target to analyzed wallet')
                if target not in ('wallet', 'counterparty') or (target == 'counterparty' and not other):
                    raise ValueError('Select a counterparty explicitly before targeting it')
                if self.path == "/start":
                    try:
                        job_id = start_job(wallet, mode, max_pages, other, target)
                    except RuntimeError as exc:
                        self.json_response({"error": str(exc)}, 409)
                        return
                    with jobs_lock:
                        state = dict(jobs[job_id])
                    state.pop("started_epoch", None)
                    self.json_response({"job_id": job_id, **state}, 202)
                    return
                if not active_lock.acquire(blocking=False):
                    raise RuntimeError("Another wallet analysis is already running")
                try:
                    result = live(wallet, lambda _event: None, mode, max_pages, other, target)
                finally:
                    active_lock.release()
                self.page(render_report(result, aliases()))
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                if async_request: self.json_response({"error": str(exc)}, 400)
                else: self.page(error=str(exc), code=400)
            except Exception as exc:
                if async_request: self.json_response({"error": str(exc)[:500] or "Analysis failed"}, 503)
                else: self.page(error=str(exc)[:500] or "Analysis failed", code=503)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8774)
    args = parser.parse_args()
    if not args.serve:
        parser.error("--serve is required")
    server = make_server(port=args.port)
    print(f"http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
