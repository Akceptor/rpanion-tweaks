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

    def test_manual_passthrough(self):
        s = agps.Settings.from_env({"AGPS_APPROX_LAT": "47.0", "AGPS_APPROX_LON": "8.0", "AGPS_APPROX_ACC_KM": "10"})
        self.assertEqual(s.manual, (47.0, 8.0, 0.0))
        self.assertEqual(s.manual_acc_m, 10000)


if __name__ == "__main__":
    unittest.main()
