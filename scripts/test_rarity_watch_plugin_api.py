import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

API = Path(__file__).parents[1] / 'plugins' / 'rarity-watch-manager' / 'dashboard' / 'plugin_api.py'


class RarityWatchPluginApiTests(unittest.TestCase):
    def test_list_endpoint_uses_saved_state_without_mutation(self):
        spec = importlib.util.spec_from_file_location('rarity_watch_plugin_api', API)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        contract = '0x' + 'd' * 40
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / 'state.json'
            state.write_text(json.dumps({'collections': [{'name': 'Saved', 'contract': contract}]}), encoding='utf-8')
            before = state.read_bytes()
            result = module.execute_request({'action': 'list'}, state_path=state)
            self.assertEqual(result['count'], 1)
            self.assertEqual(state.read_bytes(), before)
            paths = {route.path for route in module.router.routes}
            self.assertIn('/execute', paths)
            self.assertIn('/health', paths)


if __name__ == '__main__':
    unittest.main(verbosity=2)
