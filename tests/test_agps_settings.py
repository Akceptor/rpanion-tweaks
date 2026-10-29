import unittest

from agps_loader import load_agps

agps = load_agps()


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        s = agps.Settings.from_env({})
        self.assertTrue(s.enable)
        self.assertEqual(s.mavlink, "tcp:127.0.0.1:5760")
        self.assertEqual(s.settle_s, 15)
        self.assertTrue(s.cache_enabled)
        self.assertEqual(s.cache_file, "/var/lib/agps/last_fix.json")
        self.assertEqual(s.cache_max_age_s, 14 * 86400)
        self.assertEqual(s.cache_acc_m, 25000)
        self.assertIsNone(s.manual)
        self.assertEqual(s.manual_acc_m, 50000)

    def test_status_default_and_override(self):
        self.assertEqual(agps.Settings.from_env({}).status, ("127.0.0.1", 14572))
        self.assertEqual(agps.Settings.from_env({"AGPS_STATUS": "10.0.0.2:9"}).status, ("10.0.0.2", 9))

    def test_status_empty_disables(self):
        self.assertIsNone(agps.Settings.from_env({"AGPS_STATUS": ""}).status)

    def test_status_invalid(self):
        for bad in ("nope", "host:", ":1", "h:0", "h:70000", "h:x"):
            with self.assertRaises(ValueError, msg=bad) as cm:
                agps.Settings.from_env({"AGPS_STATUS": bad})
            self.assertIn("AGPS_STATUS", str(cm.exception))

    def test_disable(self):
        self.assertFalse(agps.Settings.from_env({"AGPS_ENABLE": "0"}).enable)

    def test_cache_off(self):
        self.assertFalse(agps.Settings.from_env({"AGPS_CACHE": "0"}).cache_enabled)

    def test_bad_number(self):
        with self.assertRaises(ValueError) as cm:
            agps.Settings.from_env({"AGPS_SETTLE_S": "x"})
        self.assertIn("AGPS_SETTLE_S", str(cm.exception))

    def test_nonfinite_rejected(self):
        with self.assertRaises(ValueError):
            agps.Settings.from_env({"AGPS_CACHE_MAX_DAYS": "nan"})

    def test_ignores_unknown_vars(self):
        agps.Settings.from_env({"AGPS_ZTP_TOKEN": "x", "AGPS_OCID_TOKEN": "y"})

    def test_approx_acc_override(self):
        self.assertEqual(agps.Settings.from_env({"AGPS_APPROX_ACC_KM": "120.5"}).manual_acc_m, 120500)

    def test_approx_acc_invalid_falls_back(self):
        for bad in ("x", "0.5", "501", "nan", "-3"):
            with self.assertLogs(level="WARNING") as cm:
                s = agps.Settings.from_env({"AGPS_APPROX_ACC_KM": bad, "AGPS_APPROX_LAT": "47.5", "AGPS_APPROX_LON": "8.5"})
            self.assertEqual(s.manual_acc_m, 50000, bad)
            self.assertNotIn("47.5", "\n".join(cm.output))

    def test_manual_passthrough(self):
        s = agps.Settings.from_env({"AGPS_APPROX_LAT": "47.0", "AGPS_APPROX_LON": "8.0", "AGPS_APPROX_ACC_KM": "10"})
        self.assertEqual(s.manual, (47.0, 8.0, 0.0))
        self.assertEqual(s.manual_acc_m, 10000)

    def test_map_icon_defaults(self):
        s = agps.Settings.from_env({})
        self.assertIs(s.map_icon, True)
        self.assertEqual(s.map_tcp, ("127.0.0.1", 5760))

    def test_map_icon_off(self):
        self.assertIs(agps.Settings.from_env({"AGPS_MAP_ICON": "0"}).map_icon, False)

    def test_map_tcp_invalid(self):
        for bad in ("nope", "h:0", "h:x", ":5760"):
            with self.assertRaises(ValueError) as cm:
                agps.Settings.from_env({"AGPS_MAP_TCP": bad})
            self.assertIn("AGPS_MAP_TCP", str(cm.exception))

    def test_map_tcp_custom(self):
        self.assertEqual(agps.Settings.from_env({"AGPS_MAP_TCP": "10.0.0.2:5761"}).map_tcp, ("10.0.0.2", 5761))


if __name__ == "__main__":
    unittest.main()
