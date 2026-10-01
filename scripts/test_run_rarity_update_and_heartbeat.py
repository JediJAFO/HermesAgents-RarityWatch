import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).with_name("run_rarity_update_and_heartbeat.py")


def load_runner():
    spec = importlib.util.spec_from_file_location("run_rarity_update_and_heartbeat", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManualRarityUpdateTests(unittest.TestCase):
    def test_complete_update_delivers_regular_result_then_heartbeat(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / "state.json"; status = root / "status.json"; lock = root / "launch.lock"
            state.write_text(json.dumps({"last_monitor_outcome": {"complete": True, "at": "2026-09-29T14:00:00-04:00"}}))
            lock.write_text("123")
            delivered = []
            result = runner.run(
                state_path=state, status_path=status, launch_lock=lock,
                wrapper_run=lambda: (0, "regular result\n", ""),
                heartbeat_render=lambda: "heartbeat\n",
                deliver=lambda text, batch: delivered.append((text, batch)) or ["message-" + str(len(delivered))])
            self.assertEqual(result["status"], "complete")
            self.assertEqual([item[0] for item in delivered], ["regular result\n", "heartbeat\n"])
            self.assertTrue(result["heartbeat_sent"])
            self.assertFalse(lock.exists())
            self.assertEqual(json.loads(status.read_text())["status"], "complete")

    def test_partial_update_delivers_regular_result_but_withholds_heartbeat(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / "state.json"; status = root / "status.json"; lock = root / "launch.lock"
            state.write_text(json.dumps({"last_monitor_outcome": {"complete": False, "failed_collections": [{"name": "One"}]}}))
            lock.write_text("123")
            delivered = []
            result = runner.run(
                state_path=state, status_path=status, launch_lock=lock,
                wrapper_run=lambda: (0, "partial result\n", ""),
                heartbeat_render=lambda: self.fail("heartbeat must not render after partial update"),
                deliver=lambda text, batch: delivered.append(text) or ["message-1"])
            self.assertEqual(result["status"], "partial")
            self.assertEqual(delivered, ["partial result\n"])
            self.assertFalse(result["heartbeat_sent"])

    def test_busy_collector_sends_nothing(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / "state.json"; status = root / "status.json"; lock = root / "launch.lock"
            state.write_text(json.dumps({"last_monitor_outcome": {"complete": True}})); lock.write_text("123")
            delivered = []
            result = runner.run(
                state_path=state, status_path=status, launch_lock=lock,
                wrapper_run=lambda: (75, "", "busy"), heartbeat_render=lambda: "heartbeat",
                deliver=lambda *args: delivered.append(args))
            self.assertEqual(result["status"], "busy")
            self.assertEqual(delivered, [])
            self.assertFalse(lock.exists())

    def test_confirm_pending_rechecks_only_pending_and_sends_fresh_heartbeat(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / "state.json"; status = root / "status.json"; lock = root / "launch.lock"
            state.write_text(json.dumps({
                "pending_exotic_change_candidates": {"contract-a|Exotic": "one", "contract-b|Legendary": "two"},
                "last_monitor_outcome": {"complete": False},
            }))
            lock.write_text("123")
            checked = []
            delivered = []

            def confirm(keys):
                checked.append(keys)
                state.write_text(json.dumps({
                    "pending_exotic_change_candidates": {},
                    "last_monitor_outcome": {"complete": True, "at": "confirmed"},
                }))
                return 0, "confirmed changes\n", ""

            result = runner.run(
                mode="confirm_pending", state_path=state, status_path=status, launch_lock=lock,
                confirmation_run=confirm, heartbeat_render=lambda: "fresh heartbeat\n",
                deliver=lambda text, batch: delivered.append(text) or ["verified"])
            self.assertEqual(checked, [["contract-a|Exotic", "contract-b|Legendary"]])
            self.assertEqual(delivered, ["confirmed changes\n", "fresh heartbeat\n"])
            self.assertEqual(result["status"], "complete")
            self.assertTrue(result["heartbeat_sent"])

    def test_confirm_pending_with_nothing_queued_makes_no_live_call(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / "state.json"; status = root / "status.json"; lock = root / "launch.lock"
            state.write_text(json.dumps({"pending_exotic_change_candidates": {}})); lock.write_text("123")
            result = runner.run(
                mode="confirm_pending", state_path=state, status_path=status, launch_lock=lock,
                confirmation_run=lambda _keys: self.fail("no marketplace confirmation should run"),
                heartbeat_render=lambda: self.fail("no heartbeat should render"),
                deliver=lambda *_args: self.fail("no Discord message should send"))
            self.assertEqual(result["status"], "no_pending")
            self.assertFalse(lock.exists())

    def test_manual_update_automatically_confirms_purchase_priority_candidates(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / "state.json"; status = root / "status.json"; lock = root / "launch.lock"
            state.write_text(json.dumps({
                "pending_exotic_change_candidates": {"new|Exotic": "candidate"},
                "last_monitor_outcome": {"complete": False, "failed_collections": [{"name": "New"}]},
            }))
            lock.write_text("123")
            delays = []
            delivered = []

            def confirm(keys):
                self.assertEqual(keys, ["new|Exotic"])
                state.write_text(json.dumps({
                    "pending_exotic_change_candidates": {},
                    "last_monitor_outcome": {"complete": True, "at": "confirmed"},
                }))
                return 0, "priority confirmed\n", ""

            result = runner.run(
                state_path=state, status_path=status, launch_lock=lock,
                wrapper_run=lambda: (0, "initial partial\n", ""),
                priority_keys_fn=lambda _state: ["new|Exotic"], confirmation_run=confirm,
                auto_confirmation_delay=10, sleep_fn=delays.append,
                heartbeat_render=lambda: "heartbeat\n",
                deliver=lambda text, batch: delivered.append(text) or ["verified"])
            self.assertEqual(delays, [10])
            self.assertEqual(delivered, ["initial partial\n", "priority confirmed\n", "heartbeat\n"])
            self.assertEqual(result["status"], "complete")
            self.assertTrue(result["automatic_confirmation"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
