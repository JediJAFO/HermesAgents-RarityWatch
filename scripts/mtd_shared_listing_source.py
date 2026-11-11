"""Saved-only raw best-order compatibility view. No transport or state writes."""
import json
import re
from pathlib import Path
from datetime import datetime, timezone
HOME=Path(__file__).resolve().parent.parent
CONFIG=HOME/'price-watches/mtd-shared-listings-config.json'
SNAPSHOT=HOME/'price-watches/mtd-listing-wallets.json'
MAX_AGE_SECONDS=6*3600

def shared_enabled(family):
    if family not in ('exotic','legendary','completion','under100'):return False
    try:return json.loads(CONFIG.read_text(encoding='utf-8')).get(family) is True
    except (OSError,ValueError,AttributeError):return False

def publication_enabled():
    try: producer=json.loads(CONFIG.read_text(encoding='utf-8')).get('producer') is True
    except (OSError,ValueError,AttributeError):producer=False
    return producer or any(shared_enabled(f) for f in ('exotic','legendary','completion','under100'))

def timestamp(value):
    try:
        dt=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
        if not isinstance(dt,datetime) or dt.tzinfo is None:raise ValueError()
        return dt.timestamp()
    except (ValueError,TypeError,AttributeError):raise ValueError('Invalid source timestamp') from None

def validate_items(items,contracts):
    if not isinstance(items,list):raise ValueError('Missing raw items')
    seen=set();unknown=set()
    for item in items:
        if not isinstance(item,dict):raise ValueError('Malformed raw item')
        match=re.fullmatch(r'POLYGON:(0x[0-9a-f]{40}):([0-9]+)',item.get('id',''))
        if not match or match[1] not in contracts or item['id'] in seen:raise ValueError('Invalid/duplicate/out-of-scope raw item')
        seen.add(item['id']);contract,token=match.groups()
        if str(item.get('tokenId'))!=token or item.get('contract')!='POLYGON:'+contract:raise ValueError('Raw item identity mismatch')
        order=item.get('bestSellOrder')
        if not isinstance(order,dict):raise ValueError('Missing provider best order')
        mt=(order.get('make') or {}).get('type') or {}
        if mt.get('contract')!='POLYGON:'+contract or str(mt.get('tokenId'))!=token or not order.get('id'):raise ValueError('Best order identity mismatch')
        attrs=(item.get('meta') or {}).get('attributes') or []
        rarities=[a.get('value') for a in attrs if isinstance(a,dict) and str(a.get('key','')).strip().lower()=='rarity']
        if len(rarities)!=1 or rarities[0] not in ('Basic','Common','Uncommon','Rare','Epic','Legendary','Exotic'):unknown.add(contract)
    return unknown

def load_view(snapshot_path=None, *, now=None, required_contracts=None):
    try:
        doc=json.loads(Path(snapshot_path or SNAPSHOT).read_text(encoding='utf-8'));snap=doc.get('snapshot',doc)
        view=snap.get('shared_listings')
        if not isinstance(view,dict) or view.get('version')!=1 or view.get('complete') is not True:raise ValueError('Incomplete compatibility source')
        contracts=view.get('contracts')
        if not isinstance(contracts,dict) or not contracts or any(not re.fullmatch(r'0x[0-9a-f]{40}',c) or not isinstance(n,str) or not n for c,n in contracts.items()):raise ValueError('Invalid exact contract scope')
        if view.get('started_at')!=snap.get('started_at') or view.get('observed_at')!=snap.get('as_of') or contracts!=snap.get('contracts'):raise ValueError('Compatibility generation not bound to snapshot')
        start=timestamp(view['started_at']);end=timestamp(view['observed_at']);current=timestamp(now or datetime.now(timezone.utc))
        if start>end or not 0<=current-end<=MAX_AGE_SECONDS:raise ValueError('Stale/invalid compatibility generation')
        required=set(contracts if required_contracts is None else required_contracts)
        if not required<=set(contracts):raise ValueError('Contract pending shared producer onboarding')
        unknown=validate_items(view.get('items'),contracts)
        for c in required:
            coverage=(view.get('coverage') or {}).get(c,{})
            if c in unknown or coverage.get('complete') is not True or coverage.get('rarity_complete') is not True:raise ValueError('Incomplete rarity/contract coverage: '+c)
        return dict(view,generation=view['started_at']+'|'+view['observed_at'])
    except (OSError,KeyError,TypeError,AttributeError) as exc:raise ValueError('Unavailable or malformed compatibility source') from exc
