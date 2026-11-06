"""Common saved-only rarity client; legacy normalizers remain authoritative."""
from mtd_shared_listing_source import load_view, timestamp
class SavedRarityClient:
    def __init__(self, normalize, sort_key, *, snapshot_path=None, now=None):
        self.normalize=normalize;self.sort_key=sort_key;self.snapshot_path=snapshot_path;self.now=now;self.view=None
    def prepare(self, contracts):
        self.view=load_view(self.snapshot_path,now=self.now,required_contracts=contracts)
        return self.view
    def collection_name(self,contract):
        if self.view is None:self.prepare([contract])
        if contract not in self.view['contracts']:raise ValueError('Contract pending shared producer onboarding')
        return self.view['contracts'][contract]
    def collection(self,contract,name):
        if self.view is None:self.prepare([contract])
        if contract not in self.view['contracts']:raise ValueError('Contract pending shared producer onboarding')
        # Revalidate coverage for each manager request too, without rereading a newer file.
        cov=self.view['coverage'].get(contract,{})
        if cov.get('complete') is not True or cov.get('rarity_complete') is not True:raise ValueError('Incomplete contract coverage')
        rows=[self.normalize(item,contract,name) for item in self.view['items'] if item['contract']=='POLYGON:'+contract]
        return sorted((r for r in rows if r),key=self.sort_key)
    @property
    def provenance(self):
        return {k:self.view[k] for k in ('generation','started_at','observed_at')}

def advances(current, previous):
    return bool(previous and previous.get('generation') and current['generation']!=previous['generation'] and timestamp(current['started_at'])>timestamp(previous['started_at']) and timestamp(current['observed_at'])>timestamp(previous['observed_at']))
