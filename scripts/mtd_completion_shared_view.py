"""Saved-only Completion observation adapter. No marketplace/network fallback.

The shared reader owns publication, freshness and complete rarity coverage.
This adapter only maps raw provider best orders to the existing engine's input.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import tempfile

HOME = Path(__file__).resolve().parents[1]
RARITIES = {'Exotic', 'Legendary', 'Epic', 'Rare', 'Basic', 'Common'}
CONTRACT = re.compile(r'^0x[0-9a-f]{40}$')


def shared_completion_enabled():
    # Keep direct-node and Python paths on the same trusted local flag.
    config = HOME / 'price-watches' / 'mtd-shared-listings-config.json'
    if not config.exists():
        return False
    from mtd_shared_listing_source import shared_enabled
    return shared_enabled('completion')


def shared_source_preflight(occurrence):
    if not shared_completion_enabled():
        return True
    try:
        from mtd_shared_listing_schedule import completion_preflight
        return bool(completion_preflight(occurrence))
    except (ImportError, OSError, ValueError):
        return False


def _instant(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('source timestamps require an offset')
    return result


def _contract(value):
    return str(value).removeprefix('POLYGON:').lower()


def _listing(raw, observed):
    order = raw.get('bestSellOrder')
    if not isinstance(order, dict):
        return None
    if order.get('cancelled') or order.get('status') != 'ACTIVE':
        return None
    take = order.get('take') or {}
    currency = take.get('type') or {}
    if currency.get('@type') != 'ETH' or currency.get('blockchain') != 'POLYGON':
        return None
    if order.get('endedAt') and _instant(order['endedAt']) <= observed:
        return None
    if order.get('startedAt') and _instant(order['startedAt']) > observed:
        return None
    if Decimal(str(order.get('makeStock', '0'))) <= 0:
        return None
    identity = str(raw.get('id', '')).split(':')
    if len(identity) != 3 or identity[0] != 'POLYGON' or not CONTRACT.fullmatch(identity[1]) or not identity[2].isdigit():
        raise ValueError('invalid item identity')
    token = str(raw.get('tokenId', identity[2]))
    make = (order.get('make') or {}).get('type') or {}
    if make.get('@type') not in {'ERC721', 'ERC1155'} or _contract(make.get('contract')) != identity[1] or str(make.get('tokenId')) != token or token != identity[2]:
        raise ValueError('best order item mismatch')
    price = Decimal(str(take.get('value'))) / Decimal(str((order.get('make') or {}).get('value', '1')))
    if not price.is_finite() or price <= 0:
        raise ValueError('invalid native buy-now price')
    seller = str(order.get('maker', '')).removeprefix('ETHEREUM:').removeprefix('POLYGON:').lower()
    if not CONTRACT.fullmatch(seller):
        raise ValueError('invalid seller')
    name = (raw.get('meta') or {}).get('name') or raw.get('name')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('missing token name')
    return {'token_id':token, 'name':name, 'price':float(price), 'currency':'POLYGON',
            'url':f'https://mcfarlanetoys.digital/token/{raw["id"]}', 'seller_wallet':seller,
            'seller_display':'...' + seller[-6:], 'seller_lookup':'Rarible active sell order maker'}


def completion_results(collections, *, view=None):
    """Return UNAVAILABLE, never invented zeroes, on unknown/incomplete source."""
    watches = [c for c in collections if c.get('enabled') is not False]
    try:
        if view is None:
            from mtd_shared_listing_source import load_view
            view = load_view(required_contracts=sorted({_contract(c['contract']) for c in watches}))
        if not view.get('generation'):
            raise ValueError('missing source generation')
        start, asof = _instant(view['started_at']), _instant(view['observed_at'])
        if asof < start:
            raise ValueError('source timestamps reversed')
        covered = {_contract(c) for c in view['contracts']}
        results = []
        for c in watches:
            contract, rarity = _contract(c['contract']), c.get('rarity', 'Exotic')
            if not CONTRACT.fullmatch(contract) or rarity not in RARITIES or contract not in covered:
                raise ValueError('watch not covered by shared source')
            listings, seen = [], set()
            for raw in view['items']:
                if _contract(str(raw.get('id', '')).rsplit(':', 1)[0]) != contract:
                    continue
                attributes = (raw.get('meta') or {}).get('attributes', raw.get('attributes', []))
                traits = [a.get('value') for a in attributes if a.get('key') == 'Rarity']
                if len(traits) != 1 or traits[0] not in RARITIES | {'Common'}:
                    raise ValueError('unknown exact-token rarity')
                if traits[0] != rarity:
                    continue
                listing = _listing(raw, asof)
                if listing:
                    if listing['token_id'] in seen:
                        raise ValueError('duplicate token observation')
                    seen.add(listing['token_id'])
                    # Saved seller labels remain valid only for the same wallet.
                    # The renderer still resolves current canonical aliases first.
                    prior = next((old for old in (c.get('baseline') or {}).get('listings', [])
                                  if str(old.get('token_id')) == listing['token_id']
                                  and str(old.get('seller_wallet', '')).lower() == listing['seller_wallet']), None)
                    if prior:
                        for field in ('seller_name_tag', 'seller_display', 'seller_lookup'):
                            if prior.get(field):
                                listing[field] = prior[field]
                    listings.append(listing)
            listings.sort(key=lambda x:(int(x['token_id']), x['url']))
            results.append({'name':c.get('name'), 'contract':contract, 'rarity':rarity, 'watch_key':contract+'|'+rarity,
                            'kind':'VERIFIED', 'baseline':{'for_sale':bool(listings),'listing_count':len(listings),'listings':listings},
                            'source_generation':view['generation'], 'source_started_at':view['started_at'], 'source_observed_at':view['observed_at']})
        return {'results':results}
    except (ImportError, OSError, ValueError, KeyError, TypeError, AttributeError, InvalidOperation, ZeroDivisionError) as error:
        return {'results':[{'name':c.get('name'), 'contract':_contract(c.get('contract')), 'rarity':c.get('rarity','Exotic'),
                            'watch_key':_contract(c.get('contract'))+'|'+c.get('rarity','Exotic'), 'kind':'UNAVAILABLE',
                            'reason':'shared listing source not ready: '+str(error)[:180]} for c in watches]}


def saved_environment_ready(bundle, state, occurrence):
    """Gate the exact immutable generation before touching policy/retry state."""
    try:
        from mtd_shared_listing_schedule import request_refresh
        if occurrence:
            from mtd_shared_listing_schedule import source_minimum
        ready = True
        for row in bundle['results']:
            key = row['watch_key']
            prior = (state.get('pending_exotic_candidate_sources') or {}).get(key)
            minimum = source_minimum(occurrence, 'completion') if occurrence else None
            if row.get('kind') != 'VERIFIED':
                request_refresh('completion:coverage:'+key, minimum or datetime.now(timezone.utc).isoformat(),
                                family='completion', occurrence=occurrence)
                ready = False
                continue
            if minimum and _instant(row['source_started_at']) < _instant(minimum):
                request_refresh('completion:'+str(occurrence['execution_id']), minimum,
                                family='completion', occurrence=occurrence)
                ready = False
            if prior and key in (state.get('pending_exotic_change_candidates') or {}):
                independent = (row['source_generation'] != prior['source_generation']
                               and _instant(row['source_started_at']) > _instant(prior['source_started_at'])
                               and _instant(row['source_started_at']) > _instant(prior['source_observed_at'])
                               and _instant(row['source_observed_at']) > _instant(prior['source_observed_at']))
                if not independent:
                    request_refresh('completion:confirmation:'+key, prior['source_observed_at'],
                                    after_generation={'generation':prior['source_generation'], 'started_at':prior['source_started_at'], 'observed_at':prior['source_observed_at']},
                                    family='completion', occurrence=occurrence)
                    ready = False
        return ready
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return False


@contextmanager
def saved_view_environment(collections, environment):
    """One immutable per-invocation scratch artifact; never canonical state."""
    env = dict(environment)
    env.pop('MCFARLANE_SHARED_COMPLETION_VIEW', None)
    if not shared_completion_enabled():
        yield env
        return
    scratch = HOME / 'cache' / 'scratch'
    scratch.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix='completion-shared-', suffix='.json', dir=scratch)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(completion_results(collections), stream)
        env['MCFARLANE_SHARED_COMPLETION_VIEW'] = filename
        yield env
    finally:
        Path(filename).unlink(missing_ok=True)
