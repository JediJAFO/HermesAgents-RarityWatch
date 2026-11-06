"""Saved-only, read-only wallet graph. No providers, queues or notifications.

Public contract: build_graph(database, wallet_inputs, *, aliases, catalog).
Full addresses are private identifiers; labels are exact aliases or 0x...NNNN.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from mtd_wallet_transfer_analyzer import resolve_wallet
from mtd_nft_history_provider import identities, current_ownership

MAX_RECORDS = 100000
MAX_RAW_BYTES = 1024 * 1024
MAX_TOTAL_RAW_BYTES = 32 * 1024 * 1024
MAX_OBSERVATIONS = 100000
MAX_JOBS = 10000
ZERO = '0x' + '0' * 40


def _key(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _wallet(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:(?:ETHEREUM|POLYGON):)?0x[0-9a-fA-F]{40}', value):
        raise ValueError('Malformed wallet')
    return value.split(':')[-1].lower()


def _selected(wallet_inputs, aliases):
    values = [wallet_inputs] if isinstance(wallet_inputs, str) else wallet_inputs
    if not isinstance(values, (list, tuple)) or not all(isinstance(v, str) for v in values):
        raise ValueError('Wallet inputs must be a string or list of strings')
    selected = []
    for value in values:
        for token in re.split(r'[,\r\n]+', value):
            if not token.strip():
                continue
            token = token.strip()
            if re.match(r'(?i)(?:0x|ethereum:|polygon:)', token) and not re.fullmatch(r'0x[0-9a-fA-F]{40}', token):
                raise ValueError('Malformed literal wallet; it cannot be an alias')
            address = resolve_wallet(token, aliases)
            if address == ZERO:
                raise ValueError('The zero address is not a selected wallet')
            if address not in selected:
                selected.append(address)
    if not 2 <= len(selected) <= 50:
        raise ValueError('Select between 2 and 50 distinct wallets')
    return selected


def _exact_aliases(aliases):
    if not isinstance(aliases, dict):
        raise ValueError('Shared aliases must be a parsed object')
    canonical = aliases.get('aliases', [])
    exact = aliases.get('exact_aliases', {})
    if not isinstance(canonical, list) or not isinstance(exact, dict):
        raise ValueError('Malformed shared aliases')
    def valid(wallet, label):
        return isinstance(wallet, str) and bool(re.fullmatch(r'0x[0-9a-fA-F]{40}', wallet)) and isinstance(label, str) and bool(label.strip())
    return {'aliases': [{'wallet': r['wallet'].lower(), 'alias': r['alias']} for r in canonical if isinstance(r, dict) and valid(r.get('wallet'), r.get('alias'))],
            'exact_aliases': {k.lower(): v for k, v in exact.items() if valid(k, v)}}


def _label(wallet, aliases):
    names = {r.get('alias') for r in aliases.get('aliases', []) if isinstance(r, dict) and r.get('wallet', '').lower() == wallet and isinstance(r.get('alias'), str) and r['alias'].strip()}
    if not names:
        names = {v for k, v in aliases.get('exact_aliases', {}).items() if k.lower() == wallet and isinstance(v, str) and v.strip()}
    return next(iter(names)) if len(names) == 1 else '0x...' + wallet[-4:]


def _asset(raw):
    # Associations may belong to an older revision. Revalidate every redundant
    # identity in the actual current body, including collection versus contract.
    parts = []
    def check(node):
        if not isinstance(node, dict):
            return
        parts.append(node)
        if node.get('tokenId') is not None:
            token = node['tokenId']
            if not isinstance(token, str) or not re.fullmatch(r'[0-9]{1,128}', token):
                raise ValueError('Malformed literal token ID')
        if node.get('contract') and node.get('collection'):
            def contract(value):
                if not isinstance(value, str) or not re.fullmatch(r'(?:POLYGON:)?0x[0-9a-fA-F]{40}', value):
                    raise ValueError('Malformed contract')
                return value.split(':')[-1].lower()
            if contract(node['contract']) != contract(node['collection']):
                raise ValueError('Conflicting contract and collection')
        for field in ('nft', 'type', 'make', 'take'):
            check(node.get(field))
    check(raw)
    assets = identities(raw)
    if len(assets) != 1:
        raise ValueError('Missing exact asset')
    asset = next(iter(assets))
    _, exact_contract, exact_token = asset.split(':')
    if len(exact_token) > 128:
        raise ValueError('Literal token ID is too long')
    for node in parts:
        for field in ('contract', 'collection'):
            if field in node:
                value = node[field]
                if not isinstance(value, str) or not re.fullmatch(r'(?:POLYGON:)?0x[0-9a-fA-F]{40}', value) or value.split(':')[-1].lower() != exact_contract:
                    raise ValueError('Conflicting partial contract identity')
        if node.get('tokenId') is not None and node['tokenId'] != exact_token:
            raise ValueError('Conflicting partial literal token identity')
    return asset


def _time(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,128})?(?:Z|[+-]\d{2}:\d{2})', value):
        raise ValueError('Zoned ISO timestamp required')
    if value[-1] != 'Z' and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise ValueError('Malformed timezone offset')
    date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if date.tzinfo is None:
        raise ValueError('Missing timezone')
    return date


def _time_key(value):
    date = _time(value)
    fraction = re.search(r'\.(\d+)', value)
    # Avoid datetime's microsecond truncation and floating-point epochs.
    seconds = date.toordinal() * 86400 + date.hour * 3600 + date.minute * 60 + date.second - int(date.utcoffset().total_seconds())
    return seconds, Decimal('0.' + fraction.group(1)) if fraction else Decimal(0)


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 128:
        raise ValueError('Invalid decimal')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Invalid decimal') from None
    if not result.is_finite() or result <= 0 or abs(result.as_tuple().exponent) > 128:
        raise ValueError('Nonpositive or nonfinite decimal')
    return result


def _event(row, catalog):
    raw = json.loads(row['raw'])
    if not isinstance(raw, dict) or row['provider'] != 'rarible':
        raise ValueError('Unsupported evidence provider or payload')
    ident = row['id']
    if not isinstance(ident, str) or not ident or ident.strip() != ident or any(ord(c) < 32 for c in ident) or raw.get('id') != ident:
        raise ValueError('Malformed literal activity identity')
    encoded = json.dumps(raw, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    if hashlib.sha256(encoded.encode()).hexdigest() != row['digest']:
        raise ValueError('Digest mismatch')
    asset = _asset(raw)
    _, contract, token = asset.split(':')
    collection = 'polygon:' + contract
    if collection not in catalog:
        raise ValueError('Outside catalog')
    _time(raw['date'])
    kind = {'MINT': 'mint', 'TRANSFER': 'transfer', 'SELL': 'sale', 'BURN': '_burn'}.get(raw['@type'])
    if kind is None:
        raise ValueError('Unsupported event')
    sale = kind == 'sale'
    nft = raw.get('nft') or {}
    quantity = _positive(nft['value'] if sale else raw['value'])
    recipient = '__burn__' if kind == '_burn' else _wallet(raw['buyer'] if sale else raw['owner'])
    sender = (_wallet(raw['owner']) if kind == '_burn' else '__mint__' if kind == 'mint' else _wallet(raw['seller'] if sale else raw['from']))
    if kind == 'mint' and raw.get('from') is not None and _wallet(raw['from']) != ZERO:
        raise ValueError('Nonzero mint sender is not a plain mint')
    if recipient == ZERO or (kind != 'mint' and sender == recipient):
        raise ValueError('Zero recipient or self movement')
    embedded = (raw.get('meta') or {}).get('name') or nft.get('name') or (nft.get('type') or {}).get('name')
    if embedded is not None and not isinstance(embedded, str):
        raise ValueError('Invalid title')
    price = raw.get('price')
    native = ((raw.get('payment') or {}).get('type') or {})
    price_text = _decimal_text(_positive(price)) + (' POL' if native.get('@type') == 'ETH' and native.get('blockchain') == 'POLYGON' else ' (currency unavailable)') if sale and price is not None else None
    hashes = [v for v in (raw.get('transactionHash'), (raw.get('blockchainInfo') or {}).get('transactionHash')) if v is not None]
    if any(not isinstance(v, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', v) for v in hashes) or len({v.lower() for v in hashes}) > 1:
        raise ValueError('Malformed or conflicting transaction identity')
    return {'id': _key(row['provider'], row['id']), 'kind': kind, 'asset': asset,
            'contract': contract, 'token_id': token, 'title': embedded or catalog[collection].get('display_name', 'NFT') + ' #' + token,
            'from': sender, 'to': recipient, 'date': raw['date'], 'quantity': _decimal_text(quantity),
            'price_text': price_text, 'execution': raw.get('type') if sale and isinstance(raw.get('type'), str) else None,
            '_tx': hashes[0].lower() if hashes else None, '_purchase': raw.get('purchase'),
            'evidence': [{k: row[k] for k in ('provider', 'id', 'digest')}]}


def _deduplicate(events):
    groups = {}
    sale_keys = {(e['asset'], e['_tx'], e['from'], e['to']) for e in events if e['kind'] == 'sale' and e['_tx']}
    for event in events:
        if event['kind'] == 'transfer' and (event['asset'], event['_tx'], event['from'], event['to']) in sale_keys:
            event['_sale_seen'] = True
        if event['_tx']:
            sender = ZERO if event['kind'] == 'mint' else event['from']
            key = (event['asset'], event['_tx'], sender, event['to'], event['quantity'])
            groups.setdefault(key, []).append(event)
    removed = set()
    ambiguous = set()
    for key, group in groups.items():
        primary = [e for e in group if e['kind'] in ('mint', 'sale')]
        transfers = [e for e in group if e['kind'] == 'transfer']
        if not primary or not transfers:
            continue
        if len(primary) == len(transfers) == 1 and _time_key(primary[0]['date']) == _time_key(transfers[0]['date']):
            primary[0]['evidence'] = sorted(primary[0]['evidence'] + transfers[0]['evidence'], key=lambda p: (p['provider'], p['id'], p['digest']))
            removed.add(transfers[0]['id'])
        else:
            ambiguous.add(key)
            for event in group:
                event['_ambiguous'] = True
    normalized = [e for e in events if e['id'] not in removed]
    return normalized, {'deduplicated_representations': len(removed), 'ambiguous_representation_groups': len(ambiguous),
                        'unmatched_transfer_representations': sum(e['kind'] == 'transfer' for e in normalized)}

def _decimal_text(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _edges(events):
    groups = {}
    for event in events:
        key = (event['from'], event['to'], event['kind'])
        groups.setdefault(key, []).append(event)
    return [{'id': _key(*key), 'source': key[0], 'target': key[1], 'kind': key[2],
             'count': len(group), 'quantity': _sum_quantities(group),
             'asset_count': len({e['asset'] for e in group}), 'event_ids': [e['id'] for e in group]}
            for key, group in sorted(groups.items())]


def _sum_quantities(events):
    # Integer-scaled addition avoids Decimal's ambient precision rounding.
    values = [Decimal(e['quantity']).as_tuple() for e in events]
    exponent = min(v.exponent for v in values)
    total = sum(int(''.join(map(str, v.digits))) * 10 ** (v.exponent - exponent) for v in values)
    return _decimal_text(Decimal((0, tuple(int(c) for c in str(total)), exponent)))


def _patterns(events, selected, blocked, unknown_gap):
    findings = []
    by_asset = {}
    for event in sorted(events, key=lambda e: (_time_key(e['date']), e['id'])):
        by_asset.setdefault(event['asset'], []).append(event)
    suppressed = set(blocked)
    for asset, timeline in sorted(by_asset.items()):
        times = Counter(_time_key(e['date']) for e in timeline)
        unsafe = (unknown_gap or asset in blocked or any(e.get('_ambiguous') for e in timeline)
                  or any(e['quantity'] != '1' for e in timeline)
                  or sum(e['kind'] == 'mint' for e in timeline) > 1
                  or any(count > 1 for count in times.values()))
        if unsafe:
            suppressed.add(asset)
            continue
        prefix_safe = True
        for index, (first, second) in enumerate(zip(timeline, timeline[1:])):
            if index and timeline[index - 1]['to'] != first['from']:
                prefix_safe = False
            if not prefix_safe:
                suppressed.add(asset)
                continue
            if first['kind'] not in ('mint', 'transfer') or second['kind'] != 'sale':
                continue
            if first['to'] != second['from'] or first['to'] not in selected or second['to'] not in selected:
                continue
            if _time_key(first['date']) >= _time_key(second['date']) or first['quantity'] != second['quantity']:
                continue
            kind = 'mint_then_sale' if first['kind'] == 'mint' else 'transfer_then_sale_back'
            if first['kind'] == 'transfer' and (first.get('_purchase') not in (None, False) or first.get('_sale_seen')):
                continue
            if kind == 'transfer_then_sale_back' and (first['from'] != second['to'] or first['from'] not in selected):
                continue
            wallet_ids = [first['to'], second['to']] if kind == 'mint_then_sale' else [first['from'], first['to']]
            findings.append({'id': _key(kind, asset, first['id'], second['id']), 'kind': kind,
                             'title': second['title'], 'asset': asset, 'contract': second['contract'], 'token_id': second['token_id'],
                             'wallet_ids': wallet_ids, 'event_ids': [first['id'], second['id']],
                             'explanation': ('Review lead: saved evidence records a mint to the later seller, then a direct sale to another selected wallet.' if kind == 'mint_then_sale' else 'Review lead: saved evidence records a transfer, then a sale back to the original sender.'),
                             'caveats': ['Common control and actual XP awarded are unproven; no XP eligibility or rules are asserted.',
                                         'Observed same-asset quantities are not individual-copy lineage, particularly for ERC1155 assets.',
                                         'Saved history can have gaps; the synthetic Mint node is not a beneficial owner.']})
    return findings, len(suppressed)


def _read_rows(db, canonical):
    rows = []
    loaded = oversized = 0
    truncated = False
    if canonical:
        cursor = db.execute('''SELECT c.provider,c.id,c.digest,
            CASE WHEN length(CAST(r.raw AS BLOB)) <= ? THEN r.raw ELSE NULL END AS raw,
            length(CAST(r.raw AS BLOB)) AS raw_bytes,
            EXISTS(SELECT 1 FROM nh_review v WHERE v.provider=c.provider AND v.id=c.id) AS reviewed
            FROM nh_current c LEFT JOIN nh_revisions r USING(provider,id,digest)
            ORDER BY c.provider,c.id LIMIT ?''', (MAX_RAW_BYTES, MAX_RECORDS + 1))
        for row in cursor:
            if len(rows) >= MAX_RECORDS:
                truncated = True
                break
            size = row['raw_bytes'] or 0
            if row['raw'] is not None and loaded + size > MAX_TOTAL_RAW_BYTES:
                truncated = True
                break
            if row['raw'] is None and size > MAX_RAW_BYTES:
                oversized += 1
            loaded += size if row['raw'] is not None else 0
            rows.append(row)
    return rows, {'canonical_records_scanned': len(rows), 'canonical_record_total': len(rows) if canonical and not truncated else None,
                  'records_truncated': truncated, 'oversized_record_count': oversized, 'raw_bytes_loaded': loaded,
                  'record_limit': MAX_RECORDS, 'raw_byte_limit': MAX_TOTAL_RAW_BYTES}


def _safe_date(value):
    try:
        _time(value)
        return value
    except (TypeError, ValueError, OverflowError):
        return None


def _saved_coverage(db, tables, rows, events, selected):
    keys = {(r['provider'], r['id'], r['digest']) for r in rows}
    observations = {'available': 'nh_observations' in tables, 'scope': 'archive-wide bounded saved observations',
                    'scanned_count': 0, 'matched_current_count': 0, 'last_retrieved_at': None, 'truncated': False}
    if observations['available']:
        for row in db.execute('SELECT provider,id,digest,retrieved_at FROM nh_observations ORDER BY rowid LIMIT ?', (MAX_OBSERVATIONS + 1,)):
            if observations['scanned_count'] >= MAX_OBSERVATIONS:
                observations['truncated'] = True
                break
            observations['scanned_count'] += 1
            if (row['provider'], row['id'], row['digest']) in keys:
                observations['matched_current_count'] += 1
                date = _safe_date(row['retrieved_at'])
                if date and (not observations['last_retrieved_at'] or _time_key(date) > _time_key(observations['last_retrieved_at'])):
                    observations['last_retrieved_at'] = date
    assets = {e['asset'] for e in events if e['from'] in selected or e['to'] in selected}
    jobs = {'available': 'nh_jobs' in tables, 'scope': 'selected-wallet discovery and observed selected-participant assets only',
            'scanned_count': 0, 'relevant_count': 0, 'unfinished_count': 0, 'query_exhausted_count': 0,
            'states': {}, 'truncated': False, 'items': []}
    states = Counter()
    if jobs['available']:
        cursor = db.execute('''SELECT id,kind,subject,state,pages,rows,calls,
            CASE WHEN length(coverage)<=65536 THEN coverage ELSE NULL END AS coverage FROM nh_jobs ORDER BY id LIMIT ?''', (MAX_JOBS + 1,))
        for row in cursor:
            if jobs['scanned_count'] >= MAX_JOBS:
                jobs['truncated'] = True
                break
            jobs['scanned_count'] += 1
            if not ((row['kind'] == 'item' and row['subject'] in assets) or (row['kind'] == 'discovery' and row['subject'] in selected)):
                continue
            jobs['relevant_count'] += 1
            state = row['state'] if row['state'] in ('queued', 'running', 'deferred', 'paused', 'complete', 'failed', 'cancelled', 'blocked_auth') else 'unknown'
            states[state] += 1
            jobs['unfinished_count'] += state not in ('complete', 'cancelled')
            try:
                cov = json.loads(row['coverage'])
                if not isinstance(cov, dict): cov = {}
            except (TypeError, ValueError, RecursionError):
                cov = {}
            exhausted = cov.get('query_exhausted') is True
            jobs['query_exhausted_count'] += exhausted
            item = {'id': row['id'], 'kind': row['kind'], 'state': state, 'query_exhausted': exhausted,
                    'blockchain_complete': False}
            for field in ('pages', 'rows', 'calls'):
                item[field] = row[field] if isinstance(row[field], int) and row[field] >= 0 else None
            for field in ('oldest_event', 'newest_event', 'last_observed', 'query_upper_fence'):
                item[field] = _safe_date(cov.get(field))
            jobs['items'].append(item)
    jobs['states'] = dict(sorted(states.items()))
    return {'observations': observations, 'saved_jobs': jobs}


def _normalize_rows(rows, catalog):
    events = []
    excluded = Counter()
    blocked = set()
    unknown_gap = False
    for row in rows:
        raw = None
        try:
            raw = json.loads(row['raw'])
            if row['reviewed']:
                excluded['review'] += 1
                try:
                    blocked.add(_asset(raw))
                except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
                    unknown_gap = True
                continue
            if isinstance(raw, dict) and raw.get('reverted') is True:
                excluded['reverted'] += 1
                continue
            if isinstance(raw, dict) and 'reverted' in raw and raw['reverted'] is not False:
                raise ValueError('Malformed reverted status')
            if isinstance(raw, dict) and raw.get('@type') in ('LIST', 'BID', 'CANCEL_LIST', 'CANCEL_BID'):
                excluded['non_movement'] += 1
                continue
            events.append(_event(row, catalog))
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            excluded['invalid'] += 1
            try:
                blocked.add(_asset(raw))
            except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
                unknown_gap = True
    return events, excluded, blocked, unknown_gap



def _activity_summary(events, wallet, canonical, readstats, excluded):
    involved = [e for e in events if wallet in (e['from'], e['to'])]
    available = canonical and bool(involved)
    groups = {key: [] for key in ('minted', 'transferred_in', 'transferred_out', 'bought', 'sold')}
    uncertain = set()
    for event in involved:
        kind = event['kind']
        if kind == 'transfer' and event.get('_sale_seen'):
            uncertain.add('transferred_in' if event['to'] == wallet else 'transferred_out')
        if kind == 'mint' and event['to'] == wallet:
            groups['minted'].append(event)
        elif kind == 'sale':
            groups['bought' if event['to'] == wallet else 'sold'].append(event)
        elif kind == 'transfer' and not event.get('_sale_seen'):
            groups['transferred_in' if event['to'] == wallet else 'transferred_out'].append(event)
    return {'status': 'saved_subset' if available else 'unavailable',
            'scope': 'All saved wallet activity', 'lifetime_complete': False,
            'quantities': {key: (_sum_quantities(values) if values else '0') if available and key not in uncertain else None for key, values in groups.items()},
            'event_count': len(involved), 'history_truncated': readstats['records_truncated'],
            'excluded_evidence_count': sum(excluded.values()),
            'ambiguous_representations': bool(uncertain) or any(e.get('_ambiguous') for e in involved),
            'oldest_event': min((e['date'] for e in involved),key=_time_key,default=None),
            'newest_event': max((e['date'] for e in involved),key=_time_key,default=None),
            'instruction': 'Saved bounded history only, including counterparties outside this graph. Missing events are not zero lifetime activity. Run Wallet / Transfers or NFT History separately to collect missing history; this graph starts nothing.'}


def _holdings_summary(db, tables, wallet, catalog, events, budget):
    result = {'status': 'unavailable', 'items': [], 'quantity_total': None,
              'observed_quantity_total': None, 'as_of': None, 'job_id': None,
              'latest_scan': None, 'older_saved_scan': False, 'live_current': False,
              'scanned_rows': 0, 'excluded_rows': 0, 'oversized_rows': 0,
              'oversized_job_metadata': 0, 'jobs_scanned': 0, 'job_limit': MAX_JOBS,
              'metadata_record_byte_limit': 65536, 'raw_record_byte_limit': MAX_RAW_BYTES,
              'raw_bytes_loaded': 0, 'truncated': False, 'jobs_truncated': False,
              'record_limit': MAX_RECORDS, 'raw_byte_limit': MAX_TOTAL_RAW_BYTES,
              'budget_scope': 'shared across selected wallets, separate from canonical activity budget',
              'scope': 'Saved ownership discovery scan only; never inferred from receipts or net flows',
              'instruction': 'Run wallet current-holdings discovery separately to save missing ownership data. This graph starts no collection.'}
    required = {'nh_jobs': {'id','kind','subject','state','created','request','coverage'},
                'nh_ownerships': {'job','id','digest','raw','observed_at'}}
    for table, columns in required.items():
        if table not in tables or not columns <= {r['name'] for r in db.execute('PRAGMA table_info('+table+')')}:
            result['reason'] = 'Saved ownership schema unavailable'
            return result
    jobs = []
    cursor = db.execute('''SELECT id,state,
        CASE WHEN length(CAST(coverage AS BLOB))<=65536 THEN coverage END AS coverage,
        CASE WHEN length(CAST(request AS BLOB))<=65536 THEN request END AS request,
        length(CAST(request AS BLOB)) AS request_bytes, length(CAST(coverage AS BLOB)) AS coverage_bytes
        FROM nh_jobs WHERE kind='discovery' AND subject=? ORDER BY created DESC,id DESC LIMIT ?''',
        (wallet, max(0, MAX_JOBS-budget['jobs'])+1))
    for row in cursor:
        size = len((row['coverage'] or '').encode()) + len((row['request'] or '').encode())
        if budget['jobs'] >= MAX_JOBS or budget['bytes'] + size > MAX_TOTAL_RAW_BYTES:
            result['jobs_truncated'] = True
            break
        budget['jobs'] += 1
        result['jobs_scanned'] += 1
        result['oversized_job_metadata'] += (row['request_bytes'] or 0)>65536 or (row['coverage_bytes'] or 0)>65536
        budget['bytes'] += size
        try:
            coverage = json.loads(row['coverage'])
            request = json.loads(row['request'])
            if not isinstance(coverage, dict) or not isinstance(request, dict): raise ValueError('Invalid scan metadata')
            allowed = request['filter']['collections']
            if not isinstance(allowed, list) or not allowed or not all(isinstance(c,str) and re.fullmatch(r'POLYGON:0x[0-9a-fA-F]{40}',c) for c in allowed):
                raise ValueError('Invalid saved scan allowlist')
            allowed = {c.lower() for c in allowed}
        except (ValueError, KeyError, TypeError, RecursionError):
            coverage, allowed = {}, set()
        jobs.append({'id':row['id'], 'state':row['state'], 'coverage':coverage, 'allowed':allowed})
    if not jobs:
        result['reason'] = ('Saved ownership job scan truncated before a usable discovery could be read' if result['jobs_truncated'] else 'No saved ownership discovery scan for this wallet')
        return result
    latest = jobs[0]
    complete = next((j for j in jobs if j['state']=='complete' and j['coverage'].get('query_exhausted') is True and j['allowed']),None)
    chosen = complete or latest
    result.update(job_id=chosen['id'], older_saved_scan=chosen['id']!=latest['id'],
                  as_of=_safe_date(chosen['coverage'].get('last_observed')),
                  latest_scan={'job_id':latest['id'],'state':latest['state'],'query_exhausted':latest['coverage'].get('query_exhausted') is True})
    if not chosen['allowed']:
        result['reason'] = 'Saved scan allowlist or metadata unavailable'
        return result
    titles = {e['asset']: e['title'] for e in sorted(events,key=lambda e:(_time_key(e['date']),e['id']))}
    seen = set()
    assets = {}
    conflicts = set()
    cursor = db.execute('''SELECT id,digest,observed_at,
        CASE WHEN length(CAST(raw AS BLOB))<=? THEN raw END AS raw,
        length(CAST(raw AS BLOB)) AS raw_bytes FROM nh_ownerships
        WHERE job=? ORDER BY observed_at DESC,rowid DESC LIMIT ?''',
        (MAX_RAW_BYTES,chosen['id'],max(0,MAX_RECORDS-budget['rows'])+1))
    for row in cursor:
        size = row['raw_bytes'] or 0
        if budget['rows'] >= MAX_RECORDS or (row['raw'] is not None and budget['bytes']+size>MAX_TOTAL_RAW_BYTES):
            result['truncated'] = True
            break
        budget['rows'] += 1
        result['scanned_rows'] += 1
        if row['raw'] is not None:
            budget['bytes'] += size
            result['raw_bytes_loaded'] += size
        if row['id'] in seen: continue
        seen.add(row['id'])
        if row['raw'] is None:
            result['oversized_rows'] += 1
            result['excluded_rows'] += 1
            continue
        observed = _safe_date(row['observed_at'])
        if observed and (not result['as_of'] or _time_key(observed)>_time_key(result['as_of'])):
            result['as_of'] = observed
        try:
            raw = json.loads(row['raw'])
            if not isinstance(raw,dict) or not isinstance(row['id'],str) or not row['id'] or raw.get('id')!=row['id']:
                raise ValueError('Invalid saved ownership identity')
            encoded = json.dumps(raw,sort_keys=True,separators=(',',':'),ensure_ascii=False)
            if hashlib.sha256(encoded.encode()).hexdigest()!=row['digest']: raise ValueError('Ownership digest mismatch')
            asset = _asset(raw)
            _, contract, token = asset.split(':')
            if not isinstance(raw.get('owner'),str) or raw['owner'].lower()!='ethereum:'+wallet:
                raise ValueError('Unexpected exact ownership namespace/owner')
            if 'polygon:'+contract not in catalog or 'polygon:'+contract not in chosen['allowed']:
                raise ValueError('Outside catalog or saved scan allowlist')
            if not isinstance(raw.get('value'),str) or not re.fullmatch(r'[0-9]{1,128}',raw['value']):
                raise ValueError('Invalid ownership quantity')
            pending = raw.get('pending')
            if pending is not False and not isinstance(pending,list):
                raise ValueError('Unknown pending ownership state')
            if not current_ownership(raw): continue
            title = titles.get(asset) or catalog['polygon:'+contract].get('display_name','NFT')+' · Token '+token
            item = {'asset':asset,'contract':contract,'token_id':token,'title':title,'quantity':_decimal_text(_positive(raw['value']))}
            if asset in assets and assets[asset]['quantity']!=item['quantity']: conflicts.add(asset)
            assets[asset] = item
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            result['excluded_rows'] += 1
    result['excluded_rows'] += len(conflicts)
    result['items'] = [assets[a] for a in sorted(assets) if a not in conflicts]
    result['status'] = ('complete_saved_scan' if complete and not (result['truncated'] or result['jobs_truncated'] or result['excluded_rows']) else 'partial_saved_scan')
    observed_total = _sum_quantities(result['items']) if result['items'] else '0'
    result['observed_quantity_total'] = observed_total
    result['quantity_total'] = observed_total if result['status']=='complete_saved_scan' else None
    return result


def build_graph(database, wallet_inputs, *, aliases, catalog):
    """Return a JSON-serializable graph from one bounded SQLite read snapshot."""
    aliases = _exact_aliases(aliases)
    selected = _selected(wallet_inputs, aliases)
    path = Path(database).resolve()
    if not path.is_file():
        raise ValueError('Saved evidence database is missing; no archive was created')
    warnings = ['Saved evidence is bounded; missing events do not prove they did not occur.']
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        snapshot = datetime.now(timezone.utc).isoformat()
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        canonical = {'nh_current', 'nh_revisions', 'nh_review'} <= tables
        if not canonical:
            warnings.append('Canonical evidence tables unavailable; legacy rows were not read or migrated.')
        rows, readstats = _read_rows(db, canonical)
        events, excluded, blocked, unknown_gap = _normalize_rows(rows, catalog)
        events, dedup = _deduplicate(events)
        saved = _saved_coverage(db, tables, rows, events, selected)
        ownership_budget = {'rows': 0, 'bytes': 0, 'jobs': 0}
        wallet_summaries = {w: {'activity': _activity_summary(events, w, canonical, readstats, excluded),
                                'holdings': _holdings_summary(db, tables, w, catalog, events, ownership_budget)} for w in selected}
    detail_budget = 5000
    for wallet, summary in wallet_summaries.items():
        summary['status'] = ('no_saved_wallet_data' if summary['activity']['status']=='unavailable' and summary['holdings']['job_id'] is None else 'saved_data_available')
        involved = sorted((e for e in events if e['kind'] in ('mint', 'transfer', 'sale') and wallet in (e['from'], e['to'])), key=lambda e: (_time_key(e['date']), e['id']), reverse=True)
        retained = involved[:min(500, detail_budget)]
        detail_budget -= len(retained)
        fields = ('id', 'kind', 'asset', 'contract', 'token_id', 'title', 'from', 'to', 'date', 'quantity', 'price_text', 'execution', 'evidence')
        summary['transactions'] = {'status': 'saved_subset' if canonical and involved else 'unavailable',
            'scope': 'All saved wallet activity, including counterparties outside this graph; bounded saved history only',
            'lifetime_complete': False, 'observed_total': len(involved), 'returned_count': len(retained),
            'truncated': len(retained)<len(involved) or readstats['records_truncated'],
            'wallet_limit': 500, 'response_limit': 5000,
            'events': [{**{k: e[k] for k in fields}, 'from_label': 'Mint' if e['from']=='__mint__' else _label(e['from'], aliases), 'to_label': _label(e['to'], aliases)} for e in retained]}
    unknown_gap = unknown_gap or readstats['records_truncated'] or not canonical
    if readstats['records_truncated']:
        warnings.append('Canonical read truncated by a record or byte budget; totals are unknown and pattern flags are suppressed.')
    if saved['observations']['truncated'] or saved['saved_jobs']['truncated']:
        warnings.append('Saved provenance/coverage scan truncated; displayed diagnostic counts are scanned subsets, not exact archive totals.')
    if saved['saved_jobs']['unfinished_count']:
        warnings.append('Relevant saved collection jobs are unfinished; no work was resumed or queued.')
    if not saved['observations']['available'] or not saved['saved_jobs']['available']:
        warnings.append('Saved observation or collection-job coverage is unavailable.')
    non_graph = sum(e['kind'] == '_burn' for e in events)
    outside = sum(e['kind'] != '_burn' and not (e['to'] in selected and (e['from'] in selected or e['kind'] == 'mint')) for e in events)
    findings, suppressed = _patterns(events, selected, blocked, unknown_gap)
    events = [e for e in events if e['to'] in selected and (e['from'] in selected or e['kind'] == 'mint')]
    if dedup['ambiguous_representation_groups']:
        warnings.append('Ambiguous sale/transfer representations retained separately; excluded from pattern conclusions.')
    if excluded['invalid']:
        warnings.append('Invalid or unsupported canonical evidence excluded: ' + str(excluded['invalid']) + '.')
    events.sort(key=lambda e: (_time_key(e['date']), e['id']))
    connected = {w for e in events if e['kind'] in ('transfer', 'sale') and e['from'] != e['to'] and e['from'] in selected and e['to'] in selected for w in (e['from'], e['to'])}
    nodes = [{'id': w, 'address': w, 'label': _label(w, aliases), 'kind': 'wallet', 'is_isolated': w not in connected} for w in selected]
    if any(e['kind'] == 'mint' for e in events):
        nodes.append({'id': '__mint__', 'label': 'Mint', 'kind': 'mint'})
    coverage = {'snapshot_at': snapshot, 'asset_count': len({e['asset'] for e in events}),
                'event_count': len(events), 'selected_wallet_count': len(selected),
                'summary': 'Saved canonical evidence only; collection exhaustion is query-specific, not lifetime completeness.',
                'lifetime_complete': False, 'canonical_available': canonical,
                'oldest_event': events[0]['date'] if events else None, 'newest_event': events[-1]['date'] if events else None,
                'excluded_outside_selected': outside, **dedup, **readstats, **saved,
                'excluded_review': excluded['review'], 'excluded_reverted': excluded['reverted'],
                'excluded_invalid': excluded['invalid'], 'excluded_non_graph_movements': non_graph,
                'excluded_non_movement': excluded['non_movement'], 'pattern_suppressed_assets': suppressed, 'pattern_global_gap': unknown_gap}
    return {'nodes': nodes, 'edges': _edges(events), 'events': [{k: v for k, v in e.items() if not k.startswith('_')} for e in events], 'patterns': findings,
            'wallet_summaries': wallet_summaries, 'coverage': coverage, 'warnings': warnings, 'network_calls': 0, 'notifications': 0, 'llm_tokens': 0}
