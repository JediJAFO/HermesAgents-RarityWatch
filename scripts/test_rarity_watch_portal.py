import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

MODULE = Path(__file__).with_name("rarity_watch_portal.py")


def load_portal():
    spec = importlib.util.spec_from_file_location("rarity_watch_portal", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RarityWatchPortalTests(unittest.TestCase):
    def setUp(self):
        self.portal = load_portal()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.state.write_text(json.dumps({"collections": []}), encoding="utf-8")
        self.calls = []

        def execute(request, **dependencies):
            self.calls.append((request, dependencies))
            if request.get("display") == "explode":
                raise RuntimeError("live onboarding unavailable")
            if request["action"] == "list":
                return {"ok": True, "action": "list", "count": 0, "watches": [], "network_calls": 0, "notifications": 0}
            return {"ok": True, "action": request["action"], "network_calls": 0, "notifications": 0}

        self.server = self.portal.make_server(
            state_path=self.state,
            lock_path=self.root / "lock",
            execute_fn=execute,
            port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://" + f"127.0.0.1:{self.server.server_port}"

    def get(self, path="/", headers=None):
        request = urllib.request.Request(self.url + path, headers=headers or {})
        return urllib.request.urlopen(request)

    def test_render_matches_wallet_tab_contract_and_labels_live_vs_saved(self):
        with self.get() as response:
            body = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("<title>RARITY WATCH MANAGER</title>", body)
        self.assertIn("Exotic / Legendary Watch Manager", body)
        self.assertIn('name="contract"', body)
        self.assertIn('name="rarity"', body)
        self.assertIn('value="Exotic"', body)
        self.assertIn('value="Legendary"', body)
        self.assertIn('name="display"', body)
        self.assertIn('name="category"', body)
        for label in ("Add (Live)", "Disable", "Remove", "List Saved"):
            self.assertIn(label, body)
        self.assertIn("Add completes only after the baseline is verified", body)
        self.assertIn("read-back-verified availability result to Discord", body)
        self.assertIn("saved-state only", body)
        self.assertIn('id="result"', body)
        self.assertRegex(body, r'name="csrf" value="[^"]+"')

    def token(self):
        with self.get() as response:
            body = response.read().decode()
        return re.search(r'name="csrf" value="([^"]+)"', body)[1]

    def post(self, values, *, path="/execute", origin=None, host=None):
        headers = {
            "Origin": origin or self.url,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if host is not None:
            headers["Host"] = host
        request = urllib.request.Request(
            self.url + path,
            data=urllib.parse.urlencode(values).encode(),
            headers=headers,
        )
        return urllib.request.urlopen(request)

    def test_list_saved_maps_to_manager_without_state_write_or_live_effects(self):
        before = hashlib.sha256(self.state.read_bytes()).hexdigest()
        with self.post({"csrf": self.token(), "action": "list"}) as response:
            body = response.read().decode()
        after = hashlib.sha256(self.state.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(self.calls, [(
            {"action": "list"},
            {"state_path": self.state, "lock_path": self.root / "lock"},
        )])
        self.assertIn("0 saved watch(es)", body)
        self.assertIn("network calls: 0", body)
        self.assertIn("notifications: 0", body)

    def test_list_token_remains_valid_after_portal_restart(self):
        secret = self.root / "csrf-secret"
        def execute(request, **_dependencies):
            return {"ok": True, "action": request["action"], "count": 0, "watches": [],
                    "network_calls": 0, "notifications": 0}
        first = self.portal.make_server(state_path=self.state, lock_path=self.root / "lock-2",
                                        execute_fn=execute, port=0, csrf_path=secret)
        thread = threading.Thread(target=first.serve_forever, daemon=True); thread.start()
        url = "http://" + f"127.0.0.1:{first.server_port}"
        body = urllib.request.urlopen(url + "/").read().decode()
        stale_token = re.search(r'name="csrf" value="([^"]+)"', body)[1]
        first.shutdown(); first.server_close(); thread.join()
        second = self.portal.make_server(state_path=self.state, lock_path=self.root / "lock-2",
                                         execute_fn=execute, port=0, csrf_path=secret)
        thread = threading.Thread(target=second.serve_forever, daemon=True); thread.start()
        self.addCleanup(second.server_close); self.addCleanup(second.shutdown)
        url = "http://" + f"127.0.0.1:{second.server_port}"
        request = urllib.request.Request(url + "/execute",
            data=urllib.parse.urlencode({"csrf": stale_token, "action": "list"}).encode(),
            headers={"Origin": url, "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("0 saved watch(es)", response.read().decode())

    def test_rejects_bad_host_origin_csrf_route_and_request_size(self):
        token = self.token()
        cases = [
            urllib.request.Request(self.url + "/", headers={"Host": "evil.invalid"}),
            urllib.request.Request(self.url + "/missing"),
            urllib.request.Request(self.url + "/execute", data=urllib.parse.urlencode({"csrf": token, "action": "list"}).encode(), headers={"Origin": "https://" + "evil.invalid"}),
            urllib.request.Request(self.url + "/execute", data=urllib.parse.urlencode({"csrf": "bad", "action": "list"}).encode(), headers={"Origin": self.url}),
            urllib.request.Request(self.url + "/wrong", data=urllib.parse.urlencode({"csrf": token, "action": "list"}).encode(), headers={"Origin": self.url}),
            urllib.request.Request(self.url + "/execute", data=b"x", headers={"Origin": self.url, "Content-Length": str(self.portal.MAX_REQUEST_BYTES + 1)}),
        ]
        expected = [403, 404, 403, 403, 403, 400]
        for request, status in zip(cases, expected):
            with self.subTest(status=status, url=request.full_url):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, status)
        self.assertEqual(self.calls, [])

    def test_invalid_input_is_rejected_before_manager(self):
        token = self.token()
        invalid = [
            {"csrf": token, "action": "add", "contract": "0x123", "rarity": "Exotic"},
            {"csrf": token, "action": "disable", "contract": "0x" + "a" * 40, "rarity": "Mythic"},
            {"csrf": token, "action": "unexpected", "contract": "0x" + "a" * 40, "rarity": "Exotic"},
        ]
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.post(values)
                self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.calls, [])

    def test_actions_map_exactly_and_optional_fields_are_trimmed(self):
        token = self.token()
        contract = "0x" + "A" * 40
        submissions = [
            ({"csrf": token, "action": "add", "contract": f"  {contract}  ", "rarity": "Legendary", "display": "  Custom Name  ", "category": "  DC  "},
             {"action": "add", "contract": contract, "rarity": "Legendary", "display": "Custom Name", "category": "DC"}),
            ({"csrf": token, "action": "disable", "contract": contract, "rarity": "Exotic", "display": "ignored", "category": "ignored"},
             {"action": "disable", "contract": contract, "rarity": "Exotic"}),
            ({"csrf": token, "action": "remove", "contract": contract, "rarity": "Legendary"},
             {"action": "remove", "contract": contract, "rarity": "Legendary"}),
        ]
        for values, expected in submissions:
            with self.post(values) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(self.calls[-1][0], expected)
            self.assertEqual(self.calls[-1][1], {"state_path": self.state, "lock_path": self.root / "lock"})
        self.assertEqual([call[0]["action"] for call in self.calls], ["add", "disable", "remove"])

    def test_manager_failure_is_visible_without_leaking_a_traceback(self):
        values = {"csrf": self.token(), "action": "add", "contract": "0x" + "a" * 40,
                  "rarity": "Exotic", "display": "explode"}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(values)
        self.assertEqual(error.exception.code, 503)
        body = error.exception.read().decode()
        self.assertIn("live onboarding unavailable", body)
        self.assertNotIn("Traceback", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
