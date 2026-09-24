import unittest
from unittest.mock import patch


class WalletAliasAuditReportTests(unittest.TestCase):
    def test_full_wallet_report_and_empty_success(self):
        import report_mtd_wallet_alias_audit as r
        saved = {'qualifying_wallet_count': 1, 'wallets': [{
            'full_wallet': '0x' + 'a' * 40,
            'distinct_occurrences': 2,
            'sources': {'Tracked-wallet history': 2},
            'roles': {'sender': 2}}],
            'scope': {'active_exotic_listings': 8, 'last_exotic_sales': 22,
                      'recent_completed_sales': 10, 'tracked_wallet_histories': 15}}
        with patch.object(r, 'audit', return_value=saved):
            text = r.render()
        self.assertIn('0x' + 'a' * 40, text)
        self.assertIn('2 distinct occurrences', text)
        with patch.object(r, 'audit', return_value=dict(saved, qualifying_wallet_count=0, wallets=[])):
            self.assertIn('no recurring unnamed wallets', r.render().lower())


if __name__ == '__main__': unittest.main(verbosity=2)
