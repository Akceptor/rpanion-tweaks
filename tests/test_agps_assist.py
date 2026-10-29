import datetime
import json
import logging
import os
import stat
import tempfile
import unittest

from agps_loader import load_agps

agps = load_agps()

NOW = 1_800_000_000.0
DAY = 86400.0
UTC_NOW = datetime.datetime(2026, 10, 29, 12, 0, 0, tzinfo=datetime.timezone.utc)


def cache_entry(age_s):
    return {"lat": 47.0, "lon": 8.0, "alt_m": 400.0, "t": NOW - age_s}


MANUAL = (47.0, 8.0, 0.0)


def choose(cache, manual):
    return agps.choose_position(cache, manual, NOW, 14 * DAY, 25000.0, 50000.0)


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "last_fix.json")

    def test_cache_round_trip_rounds_and_mode(self):
        c = agps.FixCache(self.path)
        c.save(47.0123, 8.0456, 512.3, NOW)
        d = c.load()
        self.assertEqual((d["lat"], d["lon"]), (47.01, 8.05))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_cache_bad_files(self):
        c = agps.FixCache(self.path)
        self.assertIsNone(c.load())
        for text in ("", "{", '{"lat": "x"}', '{"lat": 95, "lon": 8, "alt_m": 0, "t": 1}',
                     '{"lat": 47, "lon": 8, "alt_m": 0}', '[]'):
            with open(self.path, "w") as f:
                f.write(text)
            self.assertIsNone(c.load(), text)

    def test_cache_nonfinite(self):
        c = agps.FixCache(self.path)
        with open(self.path, "w") as f:
            f.write('{"lat": NaN, "lon": 8, "alt_m": 0, "t": 1}')
        self.assertIsNone(c.load())

    def test_cache_delete_missing_ok(self):
        c = agps.FixCache(self.path)
        c.delete()
        c.save(47.0, 8.0, 0.0, NOW)
        c.delete()
        self.assertFalse(os.path.exists(self.path))


class ChooseTest(unittest.TestCase):
    def test_choose_fresh_cache_wins(self):
        p = choose(cache_entry(DAY), MANUAL)
        self.assertEqual(p.source, "cache")
        self.assertEqual(p.acc_m, 25000)

    def test_choose_stale_cache_falls_to_manual(self):
        p = choose(cache_entry(15 * DAY), MANUAL)
        self.assertEqual(p.source, "manual")
        self.assertEqual(p.acc_m, 50000)

    def test_choose_future_cache_ignored(self):
        self.assertEqual(choose(cache_entry(-3600), MANUAL).source, "manual")

    def test_choose_none(self):
        self.assertIsNone(choose(None, None))


class ManualTest(unittest.TestCase):
    def test_parse_manual(self):
        env = {"AGPS_APPROX_LAT": "47.0", "AGPS_APPROX_LON": "8.0"}
        self.assertEqual(agps.parse_manual(env), (47.0, 8.0, 0.0))
        env["AGPS_APPROX_ALT_M"] = "300"
        self.assertEqual(agps.parse_manual(env), (47.0, 8.0, 300.0))
        self.assertIsNone(agps.parse_manual({}))

    def test_parse_manual_invalid_warns_without_values(self):
        for env in ({"AGPS_APPROX_LAT": "abc", "AGPS_APPROX_LON": "8.123"},
                    {"AGPS_APPROX_LAT": "91", "AGPS_APPROX_LON": "8.123"}):
            with self.assertLogs(level=logging.WARNING) as cm:
                self.assertIsNone(agps.parse_manual(env))
            text = "\n".join(cm.output)
            self.assertNotIn("8.123", text)
            self.assertNotIn("abc", text)
            self.assertNotIn("91", text)


class NtpTest(unittest.TestCase):
    def test_ntp_synced(self):
        class R:
            def __init__(self, out, rc=0):
                self.stdout, self.returncode = out, rc

        self.assertTrue(agps.ntp_synced(lambda *a, **k: R("yes\n")))
        self.assertFalse(agps.ntp_synced(lambda *a, **k: R("no\n")))
        self.assertFalse(agps.ntp_synced(lambda *a, **k: R("yes\n", 1)))

        def boom(*a, **k):
            raise FileNotFoundError

        self.assertFalse(agps.ntp_synced(boom))


class BuildAssistTest(unittest.TestCase):
    POS = agps.Position(47.0, 8.0, 0.0, 25000.0, "cache")

    def test_build_assist_order(self):
        frames = agps.build_assist(UTC_NOW, True, self.POS, False)
        self.assertEqual([f[2:4].hex() for f in frames], ["068a", "1340", "1340"])
        self.assertEqual(frames[1][6], 0x10)
        self.assertEqual(frames[2][6], 0x01)

    def test_build_assist_have_fix_valset_only(self):
        frames = agps.build_assist(UTC_NOW, True, self.POS, True)
        self.assertEqual([f[2:4].hex() for f in frames], ["068a"])

    def test_build_assist_time_only(self):
        self.assertEqual(len(agps.build_assist(UTC_NOW, False, None, False)), 1)
        self.assertEqual(len(agps.build_assist(UTC_NOW, True, None, False)), 2)


if __name__ == "__main__":
    unittest.main()
