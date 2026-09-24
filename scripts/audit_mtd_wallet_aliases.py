"""Saved-state-only audit of recurring wallets without a shared Name Tag."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1] / 'price-watches'
ADDRESS = re.compile(r'^(?:(?:ETHEREUM|POLYGON):)?(0x[0-9a-fA-F]{40})$')


def address(value):
    if not isinstance(value, str):
        return None
    match = ADDRESS.fullmatch(value.strip())
    return match.group(1).lower() if match else None


def load(root, name):
    return json.loads((Path(root) / name).read_text(encoding='utf-8'))


def audit(root=ROOT, include_full=False):
    root = Path(root)
    aliases = load(root, 'mcfarlane-wallet-aliases.json')
    exact = {address(k): v for k, v in aliases.get('exact_aliases', {}).items()
             if address(k) and isinstance(v, str) and v.strip()}
    masked = aliases.get('masked_aliases', [])
    holder = load(root, 'holder-activity-state.json')
    for wallet, name in holder.get('aliases', {}).items():
        normalized = address(wallet)
        if normalized and isinstance(name, str) and name.strip():
            exact.setdefault(normalized, name.strip())
    for wallet, saved in holder.get('wallets', {}).items():
        normalized = address(wallet)
        name = saved.get('name') if isinstance(saved, dict) else None
        if normalized and isinstance(name, str) and name.strip():
            exact.setdefault(normalized, name.strip())

    record_tags = defaultdict(set)
    occurrences = defaultdict(dict)

    def remember_tag(wallet, name):
        normalized = address(wallet)
        if normalized and isinstance(name, str) and name.strip():
            record_tags[normalized].add(name.strip())

    def add(wallet, key, source, role):
        normalized = address(wallet)
        if normalized:
            occurrences[normalized][key] = (source, role)

    exotic = load(root, 'mcfarlane-exotics.json')
    active_listing_count = 0
    last_exotic_sale_count = 0
    for collection in exotic.get('collections', []):
        contract = str(collection.get('contract') or '').lower()
        listings = collection.get('baseline', {}).get('listings', [])
        active_listing_count += len(listings)
        for listing in listings:
            remember_tag(listing.get('seller_wallet'), listing.get('seller_name_tag'))
            add(listing.get('seller_wallet'), ('listing', contract, str(listing.get('token_id'))),
                'Exotic active listing', 'seller')
        sale = collection.get('last_exotic_sale') or {}
        if sale:
            last_exotic_sale_count += 1
        remember_tag(sale.get('buyer_wallet'), sale.get('buyer_name_tag'))
        event = sale.get('buyer_activity_id') or (
            contract, str(sale.get('token_id')),
            str(sale.get('activity_date') or sale.get('activity_time')))
        add(sale.get('buyer_wallet'), ('sale', str(event), 'buyer'), 'Exotic last sale', 'buyer')

    sales = load(root, 'mcfarlane-dc-sales.json')
    sale_rows = list(sales.get('recent_sales', []))
    baseline_sale = sales.get('baseline', {}).get('newest_sale')
    if baseline_sale:
        sale_rows.append(baseline_sale)
    for sale in sale_rows:
        event = str(sale.get('activity_id') or sale.get('id') or sale.get('transactionHash'))
        for role in ('buyer', 'seller'):
            wallet = sale.get(role + '_wallet') or sale.get(role)
            remember_tag(wallet, sale.get(role + '_name_tag') or sale.get(role + '_display'))
            add(wallet, ('sale', event, role), 'Completed-sales history', role)

    for saved in holder.get('wallets', {}).values():
        for transaction in saved.get('history', []):
            for event in transaction.get('events', []):
                identity = tuple(sorted(map(str, event.get('activity_ids') or []))) or (
                    str(event.get('transaction_hash')), str(event.get('collection')),
                    str(event.get('token_id')), str(event.get('type')))
                for role in ('sender', 'recipient'):
                    add(event.get(role), ('holder-event', identity, role),
                        'Tracked-wallet history', role)

    def resolved(wallet):
        if wallet in exact or record_tags.get(wallet):
            return True
        matches = {
            item.get('name_tag') for item in masked if isinstance(item, dict)
            and isinstance(item.get('prefix'), str) and item.get('prefix')
            and isinstance(item.get('suffix'), str) and item.get('suffix')
            and isinstance(item.get('name_tag'), str) and item.get('name_tag')
            and wallet.startswith(item['prefix'].lower())
            and wallet.endswith(item['suffix'].lower())}
        return len(matches) == 1

    rows = []
    for wallet, items in occurrences.items():
        if len(items) < 2 or resolved(wallet):
            continue
        sources = Counter(value[0] for value in items.values())
        roles = Counter(value[1] for value in items.values())
        row = {
            'wallet': '...' + wallet[-9:],
            'distinct_occurrences': len(items),
            'sources': dict(sorted(sources.items())),
            'roles': dict(sorted(roles.items()))}
        if include_full:
            row['full_wallet'] = wallet
        rows.append(row)
    rows.sort(key=lambda row: (-row['distinct_occurrences'], row['wallet']))
    return {
        'mode': 'saved-data-only; no marketplace/API/Discord calls',
        'qualifying_wallet_count': len(rows),
        'wallets': rows,
        'scope': {
            'active_exotic_listings': active_listing_count,
            'last_exotic_sales': last_exotic_sale_count,
            'recent_completed_sales': len(sales.get('recent_sales', [])),
            'tracked_wallet_histories': len(holder.get('wallets', {}))}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--full', action='store_true', help='Include full private wallet addresses')
    args = parser.parse_args()
    print(json.dumps(audit(args.root, args.full), indent=2))


if __name__ == '__main__':
    main()
