from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime
from pathlib import Path


def load_guard_module():
    flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, _name):
            self.config = {}
            self.json = types.SimpleNamespace(ensure_ascii=True)

        def before_request(self, function):
            return function

        def route(self, *_args, **_kwargs):
            return lambda function: function

    flask.Flask = FakeFlask
    flask.Response = object
    flask.jsonify = lambda value=None, **kwargs: value if value is not None else kwargs
    flask.request = types.SimpleNamespace(headers={}, path="", method="GET")
    flask.stream_with_context = lambda iterable: iterable
    sys.modules["flask"] = flask

    requests = types.ModuleType("requests")
    requests.RequestException = Exception
    requests.Session = object
    requests.get = lambda *_args, **_kwargs: None
    requests.request = lambda *_args, **_kwargs: None
    sys.modules["requests"] = requests

    background = types.ModuleType("apscheduler.schedulers.background")

    class FakeScheduler:
        def __init__(self, **_kwargs):
            pass

        def add_job(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

    background.BackgroundScheduler = FakeScheduler
    sys.modules["apscheduler"] = types.ModuleType("apscheduler")
    sys.modules["apscheduler.schedulers"] = types.ModuleType("apscheduler.schedulers")
    sys.modules["apscheduler.schedulers.background"] = background

    module_path = Path(__file__).parents[1] / "guard.py"
    spec = importlib.util.spec_from_file_location("guard_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GuardLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = load_guard_module()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.guard.STATE_FILE = str(Path(self.temp_dir.name) / "state.json")
        self.now = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        self.guard._now = lambda: self.now
        self.guard.QUOTA_SAFETY_MARGIN_BYTES = 1000
        self.guard.get_torrents = lambda _ids=None: []
        self.guard.torrent_stop_all = lambda: True
        self.guard.torrent_start = lambda _ids=None: True

    def save_state(self, **changes):
        state = self.guard.load_state()
        state.update(changes)
        self.guard.save_state(state)

    def test_month_rollover_uses_current_session_total_as_new_baseline(self):
        self.save_state(
            month_key="2026-06",
            day_key="2026-06-30",
            monthly_uploaded_bytes=8000,
            cumulative_uploaded_bytes=5000,
            quota_bytes=10000,
        )
        self.guard.get_session_stats = lambda: {"uploadedBytes": 7000}

        result = self.guard.check_quota()
        state = self.guard.load_state()

        self.assertEqual(result["uploaded_bytes"], 0)
        self.assertEqual(state["cumulative_uploaded_bytes"], 7000)
        self.assertEqual(state["month_key"], "2026-07")

    def test_month_rollover_resumes_only_torrents_paused_by_quota(self):
        self.save_state(
            month_key="2026-06",
            quota_paused=True,
            quota_paused_torrent_ids=[3, 8],
            cumulative_uploaded_bytes=5000,
            quota_bytes=10000,
        )
        started = []
        self.guard.get_session_stats = lambda: {"uploadedBytes": 6000}
        self.guard.torrent_start = lambda ids=None: started.append(ids) or True

        self.guard.check_quota()
        state = self.guard.load_state()

        self.assertEqual(started, [[3, 8]])
        self.assertFalse(state["quota_paused"])
        self.assertEqual(state["quota_paused_torrent_ids"], [])

    def test_manual_pause_is_not_automatically_resumed(self):
        self.save_state(
            month_key="2026-07",
            manual_paused=True,
            cumulative_uploaded_bytes=5000,
            monthly_uploaded_bytes=1000,
            quota_bytes=10000,
        )
        started = []
        self.guard.get_session_stats = lambda: {"uploadedBytes": 5100}
        self.guard.torrent_start = lambda ids=None: started.append(ids) or True

        self.guard.check_quota()
        state = self.guard.load_state()

        self.assertEqual(started, [])
        self.assertTrue(state["manual_paused"])
        self.assertTrue(state["is_paused"])

    def test_transmission_restart_counts_new_session_total(self):
        self.save_state(
            month_key="2026-07",
            cumulative_uploaded_bytes=5000,
            monthly_uploaded_bytes=2000,
            quota_bytes=10000,
        )
        self.guard.get_session_stats = lambda: {"uploadedBytes": 300}

        self.guard.check_quota()
        state = self.guard.load_state()

        self.assertEqual(state["monthly_uploaded_bytes"], 2300)
        self.assertEqual(state["cumulative_uploaded_bytes"], 300)

    def test_raised_quota_resumes_recorded_torrents(self):
        self.save_state(
            month_key="2026-07",
            quota_paused=True,
            quota_paused_torrent_ids=[4, 9],
            cumulative_uploaded_bytes=5000,
            monthly_uploaded_bytes=8000,
            quota_bytes=20000,
        )
        started = []
        self.guard.get_session_stats = lambda: {"uploadedBytes": 5000}
        self.guard.torrent_start = lambda ids=None: started.append(ids) or True

        self.guard.check_quota()
        state = self.guard.load_state()

        self.assertEqual(started, [[4, 9]])
        self.assertFalse(state["quota_paused"])

    def test_quota_pause_records_only_running_torrent_ids(self):
        self.save_state(
            month_key="2026-07",
            cumulative_uploaded_bytes=5000,
            monthly_uploaded_bytes=8500,
            quota_bytes=10000,
        )
        self.guard.get_session_stats = lambda: {"uploadedBytes": 5600}
        self.guard.get_torrents = lambda _ids=None: [
            {"id": 11, "status": 6},
            {"id": 12, "status": 0},
        ]

        self.guard.check_quota()
        state = self.guard.load_state()

        self.assertTrue(state["quota_paused"])
        self.assertEqual(state["quota_paused_torrent_ids"], [11])

    def test_legacy_pause_state_is_migrated_as_manual_pause(self):
        Path(self.guard.STATE_FILE).write_text(
            json.dumps({"is_paused": True}), encoding="utf-8"
        )

        state = self.guard.load_state()

        self.assertTrue(state["manual_paused"])
        self.assertFalse(state["quota_paused"])

    def test_vnstat_uses_configured_interface_and_current_month(self):
        self.guard.VNSTAT_INTERFACE = "eth0"
        payload = {
            "interfaces": [
                {
                    "name": "docker0",
                    "traffic": {
                        "month": [
                            {"date": {"year": 2026, "month": 7}, "rx": 900, "tx": 900}
                        ]
                    },
                },
                {
                    "name": "eth0",
                    "traffic": {
                        "month": [
                            {"date": {"year": 2026, "month": 6}, "rx": 10, "tx": 20},
                            {"date": {"year": 2026, "month": 7}, "rx": 30, "tx": 40},
                        ]
                    },
                },
            ]
        }

        self.assertEqual(self.guard.get_vnstat_monthly_total(payload), 70)

    def test_quota_threshold_caps_reserve_at_ten_percent(self):
        self.guard.QUOTA_SAFETY_MARGIN_BYTES = 5000

        self.assertEqual(self.guard._quota_stop_threshold(10000), 9000)
        self.assertEqual(self.guard._quota_stop_threshold(5), 5)


if __name__ == "__main__":
    unittest.main()
