import logging
import os
import tempfile
import unittest

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from agps_loader import load_agps

agps = load_agps()

MANUAL_ENV = {"AGPS_APPROX_LAT": "47.0", "AGPS_APPROX_LON": "8.0"}


def msg(cls, src=(1, 1), **fields):
    m = cls(**fields)
    m._header.srcSystem, m._header.srcComponent = src
    return m


def heartbeat(src=(1, 1), armed=False):
    flags = mavlink.MAV_MODE_FLAG_SAFETY_ARMED if armed else 0
    return msg(mavlink.MAVLink_heartbeat_message, src, type=10, autopilot=3, base_mode=flags,
               custom_mode=0, system_status=4, mavlink_version=3)


def gps(fix, lat=470000000, lon=80000000, src=(1, 1)):
    return msg(mavlink.MAVLink_gps_raw_int_message, src, time_usec=0, fix_type=fix, lat=lat, lon=lon,
               alt=500000, eph=100, epv=100, vel=0, cog=0, satellites_visible=8)


class FakeMav:
    def __init__(self):
        self.calls = []

    def gps_inject_data_send(self, ts, tc, n, data):
        self.calls.append((ts, tc, n, bytes(data[:n])))


class FakeConn:
    def __init__(self):
        self.mav = FakeMav()


class RunnerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_path = os.path.join(self.tmp.name, "last_fix.json")
        self.t = 0.0
        self.ntp_ok = True

    def make(self, env=None):
        e = dict(MANUAL_ENV)
        e["AGPS_CACHE_FILE"] = self.cache_path
        e.update(env or {})
        s = agps.Settings.from_env(e)
        self.conn = FakeConn()
        self.saves = []
        cache = agps.FixCache(self.cache_path)
        orig = cache.save

        def save(*a):
            self.saves.append(self.t)
            orig(*a)

        cache.save = save
        return agps.Runner(self.conn, s, cache, clock=lambda: self.t, wall=lambda: 1_800_000_000.0 + self.t,
                           ntp=lambda: self.ntp_ok, sleep=lambda s: None)

    @property
    def calls(self):
        return self.conn.mav.calls

    def at(self, t, r, *msgs, hb=True):
        # Heartbeats arrive every second, as from a real FC.
        while hb and self.t + 1 < t:
            self.t += 1
            r.handle(heartbeat())
            r.tick()
        self.t = t
        if hb:
            r.handle(heartbeat())
        for m in msgs:
            r.handle(m)
        r.tick()


class RunnerTest(RunnerBase):
    def test_waits_for_gps_then_settles(self):
        r = self.make()
        self.at(0, r, heartbeat())
        self.assertEqual(self.calls, [])
        self.at(1, r, gps(1))
        self.at(15.9, r)
        self.assertEqual(self.calls, [])
        self.at(16, r)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[0][3], agps.valset_use_ana())
        self.assertTrue(all(c[:2] == (1, 1) for c in self.calls))

    def test_already_fixed_sends_valset_only(self):
        r = self.make()
        self.at(0, r, heartbeat(), gps(3))
        self.at(15, r)
        self.assertEqual([c[3] for c in self.calls], [agps.valset_use_ana()])

    def test_resend_once_after_30s_without_fix(self):
        r = self.make()
        self.at(0, r, heartbeat(), gps(1))
        self.at(15, r)
        self.assertEqual(len(self.calls), 3)
        self.at(44.9, r, heartbeat())
        self.assertEqual(len(self.calls), 3)
        self.at(45, r, heartbeat())
        self.assertEqual(len(self.calls), 6)
        self.at(200, r, heartbeat())
        self.assertEqual(len(self.calls), 6)

    def test_no_resend_after_fix(self):
        r = self.make()
        self.at(0, r, heartbeat(), gps(1))
        self.at(15, r)
        self.at(30, r, gps(3))
        self.assertEqual(r.state, "DONE")
        self.at(100, r, heartbeat())
        self.assertEqual(len(self.calls), 3)

    def test_fc_reboot_restarts(self):
        r = self.make()
        self.at(0, r, heartbeat(), gps(1))
        self.at(15, r)
        self.at(20, r, gps(3))
        self.assertEqual(r.state, "DONE")
        self.at(31, r, hb=False)
        self.assertEqual(r.state, "WAIT_GPS")
        self.at(40, r, heartbeat(), gps(1))
        self.at(55, r)
        self.assertEqual(len(self.calls), 6)

    def test_zero_latlon_fix_not_cached(self):
        r = self.make()
        self.at(0, r, heartbeat(), gps(1))
        self.at(15, r)
        self.at(20, r, gps(3, lat=0, lon=0))
        self.assertFalse(os.path.exists(self.cache_path))
        self.assertNotEqual(r.state, "DONE")

    def test_cache_written_first_fix_then_every_900s(self):
        r = self.make()
        self.at(0, r, heartbeat(), gps(1))
        self.at(20, r, gps(3))
        self.at(100, r, gps(3))
        self.at(950, r, gps(3))
        self.assertEqual(self.saves, [20, 950])
        self.assertEqual(r_cache_lat(self.cache_path), 47.0)

    def test_cache_disabled_deletes(self):
        agps.FixCache(self.cache_path).save(47.0, 8.0, 0.0, 1.0)
        r = self.make({"AGPS_CACHE": "0"})
        self.assertFalse(os.path.exists(self.cache_path))
        self.at(0, r, heartbeat(), gps(3))
        self.assertEqual(self.saves, [])
        self.assertFalse(os.path.exists(self.cache_path))

    def test_foreign_source_ignored(self):
        r = self.make()
        self.at(0, r, heartbeat((255, 190)), gps(3, src=(255, 190)), gps(3, src=(1, 193)))
        self.assertEqual(r.state, "WAIT_GPS")
        self.at(100, r)
        self.assertEqual(self.calls, [])

    def test_cache_used_after_save(self):
        r = self.make()
        self.at(0, r, heartbeat(), gps(1))
        self.at(20, r, gps(3))
        r2 = self.make({"AGPS_APPROX_LAT": "10.0", "AGPS_APPROX_LON": "10.0"})
        self.t = 100
        r2.send_bundle(have_fix=False)
        self.assertIn(agps.mga_ini_pos_llh(47.0, 8.0, 500.0, 25000.0), [c[3] for c in self.calls])

    def test_ttff_logged_without_coords(self):
        r = self.make()
        with self.assertLogs(level=logging.INFO) as cm:
            self.at(0, r, heartbeat(), gps(1))
            self.at(15, r)
            self.at(40, r, gps(3))
        text = "\n".join(cm.output)
        self.assertIn("first 3D fix 40s after GPS detected", text)
        self.assertIn("pos=manual", text)
        for bad in ("47.0", "470000000", "8.0"):
            self.assertNotIn(bad, text)


def r_cache_lat(path):
    return agps.FixCache(path).load()["lat"]


if __name__ == "__main__":
    unittest.main()
