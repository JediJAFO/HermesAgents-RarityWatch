"""Silent, bounded last-Exotic-sale enrichment. No listing or delivery mutations."""
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path('__HERMES_HOME__')
STATE = HOME / 'price-watches/mcfarlane-exotics.json'
sys.path.insert(0, str(STATE.parent))
from mcfarlane_wallet_aliases import resolve_buyer

KEY = 'last_exotic_sale_enrichment'
PAGE_SIZE = 20
TERMINAL = {'verified', 'exhausted_no_verified_match', 'retry_exhausted', 'page_limit_reached', 'disabled_by_user'}
API = 'https://api.rarible.org/v0.1'


def number(value):
    n = float(value)
    if not math.isfinite(n) or n < 0:
        raise ValueError('invalid amount')
    return n


def work(state, api, verify, now):
    eligible = [c for c in state['collections'] if not c.get('last_exotic_sale')
                and c.get(KEY, {}).get('status') not in TERMINAL
                and c.get(KEY, {}).get('next_attempt_at', 0) <= now]
    if not eligible:
        return
    collection = min(eligible, key=lambda c: c.get(KEY, {}).get('last_attempt_at', 0))
    progress = collection.setdefault(KEY, {})
    progress['last_attempt_at'] = now
    progress.setdefault('search_started_at', now)
    contract = collection['contract']
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}', contract):
        raise ValueError('invalid exact contract')
    cid = 'POLYGON:' + contract
    body = {'size': PAGE_SIZE, 'sort': 'LATEST', 'filter': {
        'blockchains': ['POLYGON'], 'types': ['SELL'], 'collections': [cid]}}
    if progress.get('cursor'):
        body['cursor'] = progress['cursor']
    data = progress.get('pending_page')
    if data is None:
        data = api('/activities/search', body)
        if not isinstance(data.get('activities'), list):
            raise ValueError('malformed Activity response')
        progress['pending_page'] = data
        progress['page_source'] = {'endpoint': API + '/activities/search', 'request': body, 'retrieved_at': datetime.fromtimestamp(now, timezone.utc).isoformat()}
    if len(data['activities']) > PAGE_SIZE:
        raise ValueError('Activity page exceeds bounded work size')
    pairs = []
    seen = set(progress.get('seen_items', []))
    for a in data['activities']:
        if a.get('reverted'):
            continue
        typ = (a.get('nft') or {}).get('type') or {}
        exact_identity = (typ.get('contract', '').lower() == cid.lower() or
                          (typ.get('contract', '').lower() == contract.lower() and typ.get('blockchain') == 'POLYGON'))
        if a.get('type') not in ('SELL', 'ACCEPT_BID') or not exact_identity or not re.fullmatch(r'\d+', str(typ.get('tokenId', ''))):
            raise ValueError('ambiguous completed Activity identity')
        when = datetime.fromisoformat(a['date'].replace('Z', '+00:00'))
        if not when.tzinfo or not a.get('id'):
            raise ValueError('missing Activity date/id')
        stamp = when.timestamp()
        if progress.get('previous_page_oldest') is not None and stamp > progress['previous_page_oldest']:
            raise ValueError('Activity cursor not newest first')
        if pairs and stamp > pairs[-1][2]:
            raise ValueError('Activity page not newest first')
        item_id = cid + ':' + str(typ['tokenId'])
        if item_id not in seen:
            seen.add(item_id)
            pairs.append((item_id, a, stamp))
    items = {}
    if pairs:
        metadata = progress.get('pending_metadata')
        if metadata is None:
            metadata = api('/items/byIds', {'ids': [i for i, _, _ in pairs]})
            # Cache only complete trait responses; incomplete metadata can retry.
            if all(any(a.get('key') == 'Rarity' for a in (i.get('meta') or {}).get('attributes', [])) for i in metadata.get('items', [])) and len(metadata.get('items', [])) == len(pairs):
                progress['pending_metadata'] = metadata
        items = {i['id']: i for i in metadata.get('items', [])}
    for item_id, a, stamp in pairs:
        item = items.get(item_id, {})
        attrs = (item.get('meta') or {}).get('attributes') or []
        rarities = {str(x.get('value', '')).strip().lower() for x in attrs if x.get('key') == 'Rarity'}
        if len(rarities) != 1 or not next(iter(rarities)):
            raise ValueError('unknown rarity blocks older sale selection')
        if rarities != {'exotic'}:
            continue
        url = 'https://mcfarlanetoys.digital/token/' + item_id
        evidence = verify(url)
        if evidence.get('trait') != 'Exotic' or evidence.get('url') != url:
            raise ValueError('token page Exotic trait not independently verified')
        payment = a.get('payment') or {}
        payment_type = payment.get('type') or {}
        if payment_type.get('@type') not in ('ETH', 'CURRENCY_NATIVE') or payment_type.get('blockchain') != 'POLYGON':
            raise ValueError('unsupported payment asset; do not assume POL')
        usd_field = next((key for key in ('priceUsd', 'amountUsd') if a.get(key) is not None), None)
        sale = {'item_name': (item.get('meta') or {}).get('name') or item_id,
                'token_id': item_id.rsplit(':', 1)[1], 'url': url,
                'price': number(payment['value']), 'currency': 'POLYGON',
                'activity_date': a['date'], 'activity_time': a['date'],
                'activity_id': a['id'], 'buyer_activity_id': a['id'],
                'activity_url': 'https://mcfarlanetoys.digital/explore/' + cid + '/activity',
                'usd_amount': number(a[usd_field]) if usd_field else None,
                'usd_source': 'Rarible completed Activity ' + usd_field if usd_field else 'Not supplied by Activity; no current-rate substitution',
                'rarity_proof': 'Rarible metadata Rarity=Exotic independently confirmed on token page',
                'rarity_evidence': evidence, 'activity_source': progress['page_source'],
                'activity_record': a, 'item_metadata_evidence': {'id': item_id, 'attributes': attrs},
                'search_started_at': datetime.fromtimestamp(progress['search_started_at'], timezone.utc).isoformat(),
                'verified_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                **resolve_buyer(a.get('buyer'))}
        collection['last_exotic_sale'] = sale
        progress.update(status='verified', verified_activity_id=a['id'])
        progress.pop('pending_page', None)
        progress.pop('pending_metadata', None)
        return
    cursor = data.get('continuation') or data.get('cursor')
    if cursor and (cursor == progress.get('cursor') or cursor in progress.get('used_cursors', [])):
        raise ValueError('repeated Activity cursor')
    progress.setdefault('page_audit', []).append({
        'source': progress['page_source'], 'activity_rows': len(data['activities']),
        'traits_source': API + '/items/byIds',
        'traits': [{'item_id': i, 'activity_id': a['id'], 'date': a['date'],
                    'rarities': sorted({str(x.get('value', '')).strip().lower() for x in (items[i].get('meta') or {}).get('attributes', []) if x.get('key') == 'Rarity'})}
                   for i, a, _ in pairs]})
    progress.setdefault('used_cursors', []).append(progress.get('cursor'))
    progress.update(cursor=cursor, pages=progress.get('pages', 0) + 1,
                    seen_items=sorted(seen), next_attempt_at=now + 900,
                    status='pending' if cursor else 'exhausted_no_verified_match')
    if pairs:
        progress['previous_page_oldest'] = pairs[-1][2]
    if progress['pages'] >= 100 and cursor:
        progress['status'] = 'page_limit_reached'
    progress['note'] = 'Not yet verified; exhaustion is not proof of never sold.'
    progress.pop('pending_page', None)
    progress.pop('pending_metadata', None)


import os
import subprocess
import tempfile
import time
import urllib.request
from zoneinfo import ZoneInfo
from exotic_execution_lock import execution_lock
from confirm_priority_exotic_listing_candidates import is_priority_listing_candidate
from retry_mcfarlane_exotic_failures import retryable_names


class Pacer:
    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep, self.last = clock, sleep, None

    def wait(self):
        if self.last is not None:
            pause = 15 - (self.clock() - self.last)
            if pause > 0:
                self.sleep(pause)

    def finish(self):
        self.last = self.clock()


def listing_priority(state, now):
    local = datetime.fromtimestamp(now, ZoneInfo('America/New_York'))
    minute = local.hour * 60 + local.minute
    # Reserve primary starts, their 25-minute budget, confirmations and recovery.
    if any(-3 <= ((minute - hour * 60 + 720) % 1440 - 720) <= 45 for hour in (0, 8, 12, 16, 20)):
        return True
    candidates = state.get('pending_exotic_change_candidates') or {}
    if any(c['name'] in candidates and is_priority_listing_candidate(c, candidates[c['name']]) for c in state.get('collections', [])):
        return True
    retry = state.get('automatic_exotic_retry_state') or {}
    if retry.get('next_retry_at') and not retry.get('exhausted'):
        return True
    outcome = state.get('last_monitor_outcome') or {}
    # Yield even before the minute retry job has established its marker.
    if retryable_names(outcome) and not (retry.get('exhausted') and retry.get('primary_run_id') == state.get('last_primary_exotic_run_id')):
        return True
    return False


def atomic_save(path, state):
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False, suffix='.tmp') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, path)


def run_once(path=STATE, lock=None, api=None, verify=None, now=None, priority=listing_priority):
    now = time.time() if now is None else now
    with execution_lock(lock) as acquired:
        if not acquired:
            return 0
        state = json.loads(path.read_text(encoding='utf-8'))
        if priority(state, now):
            return 0
        before = json.dumps(state, sort_keys=True)
        pacer = Pacer()
        transport = LiveTransport(pacer) if api is None else None
        try:
            work(state, api or transport.api, verify or transport.verify, now)
        except Exception as exc:
            touched = [c[KEY] for c in state['collections'] if c.get(KEY, {}).get('last_attempt_at') == now]
            for p in touched:
                failures = p.get('failures', 0) + 1
                p.update(failures=failures, status='retry_exhausted' if failures >= 3 else 'cooldown',
                         next_attempt_at=now + 3600, error_type=type(exc).__name__)
                # No raw exception text: requests/credentials must stay private.
                if isinstance(exc, ValueError):
                    p['validation_note'] = str(exc)[:160]
        finally:
            # Retain lease through final cooldown, so a listing job cannot follow
            # this background request too closely.
            if transport is not None:
                pacer.wait()
        if json.dumps(state, sort_keys=True) != before:
            atomic_save(path, state)
        return 0


class LiveTransport:
    def __init__(self, pacer):
        self.pacer = pacer

    def api(self, path, body):
        key = os.environ.get('RARIBLE_API_KEY')
        if not key:
            raise RuntimeError('API credential unavailable')
        self.pacer.wait()
        try:
            request = urllib.request.Request(API + path, data=json.dumps(body).encode(), method='POST', headers={
                'X-API-KEY': key, 'Accept': 'application/json', 'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36'})
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        finally:
            self.pacer.finish()

    def verify(self, url):
        self.pacer.wait()
        try:
            # One shortlisted token only; Activity and metadata remain structured.
            code = r"""
const {chromium} = require('__HERMES_HOME__/hermes-agent/node_modules/playwright');
(async()=>{
  const browser=await chromium.connectOverCDP('http://[::1]:9222',{timeout:10000});
  let page;
  try {
    page=await browser.contexts()[0].newPage();
    const url=process.argv[1];
    await page.goto(url,{waitUntil:'domcontentloaded',timeout:20000});
    await page.waitForFunction(()=>/\bRarity\s+Exotic\b/i.test(document.body?.innerText||''),{timeout:10000});
    const text=await page.locator('body').innerText();
    const ok=page.url()===url && /\bRarity\s+Exotic\b/i.test(text) && !/captcha|access denied|verify you are human/i.test(text);
    process.stdout.write(JSON.stringify({url:page.url(),trait:ok?'Exotic':null,source:'Rendered token page Rarity trait',checked_at:new Date().toISOString()}));
  } finally { if(page) await page.close(); await browser.close(); }
})().catch(()=>{process.exitCode=1;});
"""
            result = subprocess.run([str(HOME / 'node/node.exe'), '-e', code, url], capture_output=True, text=True, timeout=45)
            if result.returncode:
                raise RuntimeError('token verification unavailable')
            return json.loads(result.stdout)
        finally:
            self.pacer.finish()


if __name__ == '__main__':
    raise SystemExit(run_once())
