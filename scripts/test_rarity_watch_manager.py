import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).with_name('rarity_watch_manager.py')


def load_manager():
    spec = importlib.util.spec_from_file_location('rarity_watch_manager', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RarityWatchManagerTests(unittest.TestCase):
    def test_list_saved_migrates_in_memory_without_writing(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as d:
            state_path = Path(d) / 'state.json'
            state_path.write_text(json.dumps({'collections': [{
                'name': 'Legacy',
                'contract': '0x' + 'a' * 40,
                'source_url': 'https://mcfarlanetoys.digital/explore/POLYGON:0x' + 'a' * 40 + '/?traits%5BRarity%5D%5B0%5D=Exotic',
                'baseline': {'for_sale': False, 'listing_count': 0, 'listings': []},
            }]}), encoding='utf-8')
            before = hashlib.sha256(state_path.read_bytes()).hexdigest()
            result = manager.execute({'action': 'list'}, state_path=state_path)
            after = hashlib.sha256(state_path.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            self.assertEqual(result['watches'][0]['rarity'], 'Exotic')
            self.assertEqual(result['watches'][0]['watch_key'], '0x' + 'a' * 40 + '|Exotic')
            self.assertTrue(result['watches'][0]['enabled'])

    def test_add_allows_same_contract_for_other_rarity_and_seeds_without_alert(self):
        manager = load_manager()
        contract = '0x' + 'a' * 40
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state_path = root / 'state.json'
            state_path.write_text(json.dumps({'collections': [{
                'name': 'Existing Exotic', 'contract': contract, 'rarity': 'Exotic',
                'baseline': {'for_sale': False, 'listing_count': 0, 'listings': []},
            }], 'pending_exotic_change_candidates': {contract + '|Exotic': 'keep'}}), encoding='utf-8')
            calls = []
            def resolve(value):
                calls.append(('metadata', value))
                return {'name': 'Exact Collection'}
            def baseline(entry):
                calls.append(('baseline', entry['watch_key'], entry['source_url']))
                return {'kind': 'VERIFIED', 'baseline': {'for_sale': True, 'listing_count': 1, 'listings': [
                    {'token_id': '7', 'price': 40, 'currency': 'POL',
                     'seller_wallet': '0x' + '0' * 36 + 'bd4c'}]}}
            delivered = []
            def deliver(text, batch_id):
                delivered.append((text, batch_id))
                return ['discord-message-1']
            result = manager.execute(
                {'action': 'add', 'contract': contract.upper().replace('0X', '0x'), 'rarity': 'Legendary', 'category': 'DC'},
                state_path=state_path, lock_path=root / 'lock', metadata_resolver=resolve,
                baseline_runner=baseline, notice_deliverer=deliver)
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertTrue(result['ok'])
            self.assertEqual(result['watch']['watch_key'], contract + '|Legendary')
            self.assertEqual(len(saved['collections']), 2)
            self.assertEqual(saved['collections'][1]['baseline']['listing_count'], 1)
            self.assertEqual(saved['collections'][1]['rarity'], 'Legendary')
            self.assertEqual(saved['collections'][1]['category'], 'DC')
            self.assertEqual(saved['pending_exotic_change_candidates'], {contract + '|Exotic': 'keep'})
            self.assertEqual(result['notifications'], 1)
            self.assertEqual(result['discord_status'], 'delivered')
            self.assertIn('[Legendary] Exact Collection', delivered[0][0])
            self.assertIn('1 active BUY NOW listing verified', delivered[0][0])
            self.assertIn('40 POL', delivered[0][0])
            self.assertIn('...bd4c', delivered[0][0])
            self.assertNotIn(contract, delivered[0][0])
            self.assertNotIn('token_id', delivered[0][0])
            self.assertEqual(saved['rarity_onboarding_notices'][contract + '|Legendary']['status'], 'delivered')
            self.assertEqual(calls[0], ('metadata', contract))
            self.assertIn('traits%5BRarity%5D%5B0%5D=Legendary', calls[1][2])

    def test_unverified_add_is_not_saved_or_reported_as_no_listings(self):
        manager = load_manager(); contract = '0x' + 'd' * 40
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); state_path = root / 'state.json'
            state_path.write_text(json.dumps({'collections': []}), encoding='utf-8')
            delivered = []
            with self.assertRaisesRegex(RuntimeError, 'baseline was not verified'):
                manager.execute(
                    {'action': 'add', 'contract': contract, 'rarity': 'Exotic'},
                    state_path=state_path, lock_path=root / 'lock',
                    metadata_resolver=lambda _: {'name': 'Unverified'},
                    baseline_runner=lambda _: {'kind': 'UNAVAILABLE', 'reason': 'loading'},
                    notice_deliverer=lambda *args: delivered.append(args))
            self.assertEqual(json.loads(state_path.read_text())['collections'], [])
            self.assertEqual(delivered, [])

    def test_pending_discord_notice_retries_without_marketplace_calls(self):
        manager = load_manager(); contract = '0x' + 'e' * 40; key = contract + '|Legendary'
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); state_path = root / 'state.json'
            state_path.write_text(json.dumps({'collections': [], 'current_pol_usd': {'rate': 0.25}}), encoding='utf-8')
            def fail_delivery(_text, _batch_id):
                raise RuntimeError('discord offline')
            first = manager.execute(
                {'action': 'add', 'contract': contract, 'rarity': 'Legendary'},
                state_path=state_path, lock_path=root / 'lock',
                metadata_resolver=lambda _: {'name': 'No Listings'},
                baseline_runner=lambda _: {'kind': 'VERIFIED', 'baseline': {'for_sale': False, 'listing_count': 0, 'listings': []}},
                notice_deliverer=fail_delivery)
            self.assertFalse(first['ok'])
            self.assertEqual(first['discord_status'], 'pending')
            calls = []
            retry = manager.execute(
                {'action': 'add', 'contract': contract, 'rarity': 'Legendary'},
                state_path=state_path, lock_path=root / 'lock',
                metadata_resolver=lambda _: self.fail('metadata must not run'),
                baseline_runner=lambda _: self.fail('baseline must not run'),
                notice_deliverer=lambda text, batch: calls.append((text, batch)) or ['discord-message-2'])
            self.assertTrue(retry['ok'])
            self.assertEqual(retry['discord_status'], 'delivered')
            self.assertIn('No active [Legendary] BUY NOW listings verified', calls[0][0])
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved['rarity_onboarding_notices'][key]['status'], 'delivered')

    def test_add_rejects_bad_contract_and_exact_duplicate_before_dependencies(self):
        manager = load_manager()
        contract = '0x' + 'b' * 40
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); state_path = root / 'state.json'
            state_path.write_text(json.dumps({'collections': [{'name': 'One', 'contract': contract, 'rarity': 'Exotic'}]}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Polygon contract'):
                manager.execute({'action': 'add', 'contract': '0x123', 'rarity': 'Exotic'}, state_path=state_path, lock_path=root / 'lock')
            with self.assertRaisesRegex(ValueError, 'already saved'):
                manager.execute({'action': 'add', 'contract': contract, 'rarity': 'Exotic'}, state_path=state_path, lock_path=root / 'lock', metadata_resolver=lambda _: self.fail('no metadata'), baseline_runner=lambda _: self.fail('no baseline'))

    def test_disable_and_remove_are_exact_and_clear_only_target_work(self):
        manager = load_manager(); contract = '0x' + 'c' * 40
        exotic = contract + '|Exotic'; legendary = contract + '|Legendary'
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); state_path = root / 'state.json'
            original = {
                'collections': [
                    {'name': 'E', 'contract': contract, 'rarity': 'Exotic', 'enabled': True},
                    {'name': 'L', 'contract': contract, 'rarity': 'Legendary', 'enabled': True},
                ],
                'pending_exotic_change_candidates': {exotic: 'drop', legendary: 'keep'},
                'automatic_exotic_retry_state': {
                    'keys': [exotic, legendary],
                    'targets': [
                    {'watch_key': exotic, 'reason': 'drop'}, {'watch_key': legendary, 'reason': 'keep'}]},
            }
            state_path.write_text(json.dumps(original), encoding='utf-8')
            disabled = manager.execute({'action': 'disable', 'contract': contract, 'rarity': 'Exotic'}, state_path=state_path, lock_path=root / 'lock')
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertFalse(next(x for x in saved['collections'] if x['watch_key'] == exotic)['enabled'])
            self.assertTrue(next(x for x in saved['collections'] if x['watch_key'] == legendary)['enabled'])
            self.assertNotIn(exotic, saved['pending_exotic_change_candidates'])
            self.assertIn(legendary, saved['pending_exotic_change_candidates'])
            self.assertEqual([x['watch_key'] for x in saved['automatic_exotic_retry_state']['targets']], [legendary])
            self.assertEqual(saved['automatic_exotic_retry_state']['keys'], [legendary])
            self.assertEqual(disabled['notifications'], 0)
            removed = manager.execute({'action': 'remove', 'contract': contract, 'rarity': 'Exotic'}, state_path=state_path, lock_path=root / 'lock')
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual([x['watch_key'] for x in saved['collections']], [legendary])
            self.assertEqual(removed['notifications'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
