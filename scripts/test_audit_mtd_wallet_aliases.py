import json
from pathlib import Path
import tempfile
import unittest


class RecurringUnnamedWalletAuditTests(unittest.TestCase):
    def test_shared_aliases_filter_distinct_saved_occurrences(self):
        import audit_mtd_wallet_aliases as a
        unknown = '0x' + '1' * 31 + '123456789'
        known = '0x' + '2' * 40
        masked = '0xabc12345' + '3' * 25 + 'deadbeef0'
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.write(root, 'mcfarlane-wallet-aliases.json', {
                'exact_aliases': {known: 'Known'},
                'masked_aliases': [{'prefix': '0xabc12345', 'suffix': 'deadbeef0', 'name_tag': 'Masked'}]})
            self.write(root, 'mcfarlane-exotics.json', {'collections': [{
                'contract': '0xcollection',
                'baseline': {'listings': [
                    {'token_id': '1', 'seller_wallet': unknown},
                    {'token_id': '2', 'seller_wallet': known}]},
                'last_exotic_sale': {'buyer_wallet': masked, 'buyer_activity_id': 'sale-masked'}}]})
            self.write(root, 'mcfarlane-dc-sales.json', {'recent_sales': [
                {'activity_id': 'sale-1', 'buyer_wallet': unknown, 'seller_wallet': known}],
                'baseline': {'newest_sale': {'activity_id': 'sale-1', 'buyer_wallet': unknown}}})
            self.write(root, 'holder-activity-state.json', {'wallets': {
                known: {'name': 'Known', 'history': [{'events': [
                    {'activity_ids': ['holder-1'], 'sender': unknown, 'recipient': known}]}]}},
                'aliases': {known: 'Known'}})
            result = a.audit(root)
            self.assertEqual(result['qualifying_wallet_count'], 1)
            row = result['wallets'][0]
            self.assertEqual(row['wallet'], '...' + unknown[-9:])
            self.assertNotIn('full_wallet', row)
            self.assertEqual(row['distinct_occurrences'], 3)
            full = a.audit(root, include_full=True)['wallets'][0]
            self.assertEqual(full['full_wallet'], unknown)

    @staticmethod
    def write(root, name, value):
        (root/name).write_text(json.dumps(value), encoding='utf-8')


if __name__ == '__main__':
    unittest.main(verbosity=2)
