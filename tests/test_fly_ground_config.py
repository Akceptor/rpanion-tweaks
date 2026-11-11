import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from fly_ground_config import Settings, normalize_code, room_id_for


CODE = "CRAWLER-7KQ2-M9XF-4N6P"


class CodeTest(unittest.TestCase):
    def test_canonical_code_matches_browser_hash(self):
        self.assertEqual(normalize_code(CODE.lower()), CODE)
        self.assertEqual(room_id_for(CODE.lower()), hashlib.sha256(CODE.encode("ascii")).hexdigest())

    def test_rejects_short_or_ambiguous_code(self):
        for code in ("CRAWLER", "CRAWLER-7KQ2-M9XF", "CRAWLER-7KQ2-M9XF-4N6U"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                normalize_code(code)


class SettingsTest(unittest.TestCase):
    def test_defaults_and_allowlist(self):
        s = Settings.from_env({"FLY_ROOM_CODE": CODE, "FLY_SITE_URL": "https://example.workers.dev"})
        self.assertEqual(s.room_id, hashlib.sha256(CODE.encode("ascii")).hexdigest())
        self.assertEqual(s.channels, (1, 3))
        self.assertEqual((s.gcs_sysid, s.gcs_compid), (255, 191))
        self.assertEqual((s.planner_sysid, s.planner_compid), (255, 190))
        self.assertEqual(s.rtsp_url, "rtsp://127.0.0.1:8554/video")
        self.assertEqual(s.router_url, "udpout:127.0.0.1:14552")
        self.assertEqual(s.monitor_url, "tcp:127.0.0.1:5760")

    def test_explicit_direct_route_and_separate_monitor_are_supported(self):
        s = Settings.from_env({"FLY_ROOM_CODE": CODE, "FLY_SITE_URL": "https://example.workers.dev",
                               "FLY_MAVLINK": "tcp:127.0.0.1:5770", "FLY_MONITOR": "tcp:127.0.0.1:5780"})
        self.assertEqual(s.router_url, "tcp:127.0.0.1:5770")
        self.assertEqual(s.monitor_url, "tcp:127.0.0.1:5780")

    def test_custom_channels_and_limit(self):
        s = Settings.from_env({"FLY_ROOM_CODE": CODE, "FLY_SITE_URL": "https://example.workers.dev/",
                               "FLY_CHANNELS": "1,3,5,8", "FLY_SESSION_SECONDS": "0"})
        self.assertEqual(s.channels, (1, 3, 5, 8))
        self.assertEqual(s.session_seconds, 0)
        self.assertEqual(s.site_url, "https://example.workers.dev")

    def test_rejects_unsafe_settings(self):
        base = {"FLY_ROOM_CODE": CODE, "FLY_SITE_URL": "https://example.workers.dev"}
        for bad in ({"FLY_CHANNELS": "1,5"}, {"FLY_CHANNELS": "1,3,3"},
                    {"FLY_GCS_COMPID": "190"}, {"FLY_SITE_URL": "http://example.workers.dev"},
                    {"FLY_SESSION_SECONDS": "-1"}, {"FLY_MAVLINK": "udpin:0.0.0.0:14552"},
                    {"FLY_MONITOR": "udpout:127.0.0.1:14552"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Settings.from_env({**base, **bad})


if __name__ == "__main__":
    unittest.main()
