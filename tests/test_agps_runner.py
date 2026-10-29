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
        self.statuses = []

    def gps_inject_data_send(self, ts, tc, n, data):
        self.calls.append((ts, tc, n, bytes(data[:n])))

    def statustext_send(self, severity, text):
        self.statuses.append((severity, text))


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

    def make(self, env=None, status=True, icon=False):
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
        self.status = []
        send = (lambda data: self.status.append((self.t, data))) if status else None
        self.icon_frames = []
        ic = agps.ApproxIcon(lambda f: self.icon_frames.append((self.t, f)), clock=lambda: self.t) if icon else None
        return agps.Runner(self.conn, s, cache, clock=lambda: self.t, wall=lambda: 1_800_000_000.0 + self.t,
                           ntp=lambda: self.ntp_ok, sleep=lambda s: None, status_send=send, icon=ic)

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


class StatusTest(RunnerBase):
    def settle(self, r):
        self.at(0, r, heartbeat(), gps(1))
        self.at(15, r)

    def test_manual_datagram_format(self):
        r = self.make({"AGPS_APPROX_LAT": "47.123456", "AGPS_APPROX_LON": "-8.5", "AGPS_APPROX_ACC_KM": "50"})
        self.settle(r)
        self.assertEqual(self.status[0][1], b"AGPS manual 50000 47.12346 -8.50000")

    def test_cache_datagram_format(self):
        r = self.make()
        self.at(0, r, heartbeat(), gps(3))
        self.at(1, r, gps(3))
        self.assertTrue(os.path.exists(self.cache_path))
        r2 = self.make({"AGPS_CACHE_ACC_KM": "25.4"})
        self.at(0, r2, heartbeat(), gps(1))
        self.at(15, r2)
        self.assertEqual(self.status[0][1], b"AGPS cache 25400 47.00000 8.00000")

    def test_one_hz_while_sent(self):
        r = self.make()
        self.settle(r)
        for t in (15.4, 15.9):
            self.at(t, r, hb=False)
        self.at(16, r, hb=False)
        self.at(16.5, r, hb=False)
        self.at(17, r, hb=False)
        self.assertEqual([t for t, _ in self.status], [15, 16, 17])

    def test_stops_after_fix(self):
        r = self.make()
        self.settle(r)
        self.at(16, r, gps(3))
        self.assertEqual(r.state, "DONE")
        n = len(self.status)
        self.at(30, r, hb=False)
        self.at(40, r, hb=False)
        self.assertEqual(len(self.status), n)

    def test_stops_after_restart(self):
        r = self.make()
        self.settle(r)
        self.at(40, r, hb=False)
        self.assertEqual(r.state, "WAIT_GPS")
        n = len(self.status)
        self.at(50, r, hb=False)
        self.assertEqual(len(self.status), n)

    def test_nothing_without_position(self):
        r = self.make(env={"AGPS_CACHE": "0"})
        r.settings = r.settings._replace(manual=None)
        self.settle(r)
        self.at(20, r, hb=False)
        self.assertEqual(self.status, [])

    def test_nothing_when_fix_already_present(self):
        r = self.make()
        self.at(0, r, heartbeat(), gps(3))
        self.at(15, r)
        self.assertEqual(r.state, "SENT")
        self.at(20, r, hb=False)
        self.assertEqual(self.status, [])

    def test_disabled_without_sender(self):
        r = self.make(status=False)
        self.settle(r)
        self.at(20, r, hb=False)
        self.assertEqual(self.status, [])

    def test_logs_have_no_coordinates(self):
        r = self.make({"AGPS_APPROX_LAT": "47.123456", "AGPS_APPROX_LON": "8.654321"})
        with self.assertLogs(level=logging.INFO) as cm:
            self.settle(r)
            self.at(20, r, hb=False)
            self.at(40, r, gps(3))
        self.assertTrue(self.status)
        text = "\n".join(cm.output)
        for bad in ("47.1", "8.65", "471234", "86543"):
            self.assertNotIn(bad, text)


def gpi(lat=0, lon=0, src=(1, 1), alt=0):
    return msg(mavlink.MAVLink_global_position_int_message, src, time_boot_ms=0, lat=lat, lon=lon, alt=alt,
               relative_alt=0, vx=0, vy=0, vz=0, hdg=0)


def decoded(frames):
    from lima_frames import decode
    return [decode(f)[0] for _, f in frames]


class IconTest(RunnerBase):
    def sent(self, r):
        self.at(0, r, gps(1))
        self.at(15, r)
        self.assertEqual(r.state, "SENT")

    def test_icon_while_sent_only(self):
        r = self.make(icon=True)
        self.at(0, r, gps(1))
        self.at(14, r)
        self.assertEqual(self.icon_frames, [])
        self.at(15, r)
        m = decoded(self.icon_frames)[0]
        self.assertEqual((m.lat, m.lon), (470000000, 80000000))
        self.assertEqual(self.icon_frames[0][0], 15)

    def test_event_send_on_fc_messages(self):
        r = self.make(icon=True)
        self.sent(r)
        self.at(20, r)
        n = len(self.icon_frames)
        self.at(20.5, r, gps(1))
        self.assertEqual(self.icon_frames[-1][0], 20.5)
        self.assertEqual(len(self.icon_frames), n + 1)
        self.at(20.55, r, gpi())
        self.assertEqual(len(self.icon_frames), n + 1)
        self.at(20.7, r, gpi())
        self.assertEqual(self.icon_frames[-1][0], 20.7)

    def test_stops_on_first_fix(self):
        r = self.make(icon=True)
        self.sent(r)
        self.at(40, r, gps(3))
        self.assertEqual(r.state, "DONE")
        self.assertTrue(all(t < 40 for t, _ in self.icon_frames))
        self.at(60, r, gps(3), gpi())
        self.assertTrue(all(t < 40 for t, _ in self.icon_frames))

    def test_stops_on_heartbeat_loss_then_resumes_after_reboot(self):
        r = self.make(icon=True)
        self.sent(r)
        self.at(20, r)
        last = self.icon_frames[-1][0]
        self.at(22.9, r, gps(1), hb=False)
        self.assertEqual(self.icon_frames[-1][0], 22.9)
        before = len(self.icon_frames)
        self.at(23.1, r, gps(1), hb=False)
        self.at(25, r, hb=False)
        self.at(29, r, hb=False)
        self.assertEqual(len(self.icon_frames), before)
        self.at(40, r, hb=False)
        self.assertEqual(r.state, "WAIT_GPS")
        self.at(41, r, gps(1))
        self.at(56, r)
        self.assertEqual(r.state, "SENT")
        self.assertGreater(self.icon_frames[-1][0], 55.9)

    def test_no_icon_without_position(self):
        r = self.make(env={"AGPS_CACHE": "0"}, icon=True)
        r.settings = r.settings._replace(manual=None)
        self.sent(r)
        self.at(20, r, gps(1))
        self.assertEqual(self.icon_frames, [])
        self.assertEqual(self.conn.mav.statuses, [])

    def test_no_icon_when_fix_already_present(self):
        r = self.make(icon=True)
        self.at(0, r, gps(3))
        self.at(15, r)
        self.at(20, r, gps(3))
        self.assertEqual(self.icon_frames, [])

    def test_runner_without_icon(self):
        r = self.make(icon=False)
        self.sent(r)
        self.at(20, r, gps(1), gpi())
        self.at(40, r, gps(3))
        self.assertEqual(self.conn.mav.statuses, [])

    def test_foreign_gpi_ignored(self):
        r = self.make(icon=True)
        self.sent(r)
        self.at(20, r, gpi(src=(1, 193), alt=777), gpi(src=(255, 190), alt=777), gps(1))
        self.assertNotEqual(decoded(self.icon_frames)[-1].alt, 777)
        self.at(22, r, gpi(alt=555))
        self.at(23, r, gps(1))
        self.assertEqual(decoded(self.icon_frames)[-1].alt, 555)

    def test_statustext_once_per_sent(self):
        r = self.make(icon=True)
        self.sent(r)
        self.at(20, r, gps(1))
        self.at(30, r, gps(1))
        self.assertEqual(self.conn.mav.statuses,
                         [(mavlink.MAV_SEVERITY_NOTICE, agps.STATUSTEXT_ICON)])
        self.at(50, r, hb=False)
        self.at(51, r, gps(1))
        self.at(66, r)
        self.assertEqual(r.state, "SENT")
        self.assertEqual(len(self.conn.mav.statuses), 2)

    def test_link_lost_restarts(self):
        r = self.make(icon=True)
        self.sent(r)
        r.link_lost()
        self.assertEqual(r.state, "WAIT_GPS")
        n = len(self.icon_frames)
        self.at(16, r, gps(1))
        self.assertEqual(len(self.icon_frames), n)

    def test_no_coordinates_logged(self):
        r = self.make({"AGPS_APPROX_LAT": "47.123456", "AGPS_APPROX_LON": "8.654321"}, icon=True)
        with self.assertLogs(level=logging.INFO) as cm:
            self.sent(r)
            self.at(20, r, gps(1))
            self.at(40, r, gps(3))
        self.assertTrue(self.icon_frames)
        text = "\n".join(cm.output)
        for bad in ("47.1", "8.65", "471234", "86543"):
            self.assertNotIn(bad, text)
        for _, text in self.conn.mav.statuses:
            self.assertNotRegex(text.decode(), r"\d\d\.")

    def test_nothing_but_gpi_and_statustext_reaches_fc(self):
        r = self.make(icon=True)
        self.sent(r)
        self.at(20, r, gps(1), gpi())
        for _, f in self.icon_frames:
            self.assertEqual(decoded([(0, f)])[0].get_type(), "GLOBAL_POSITION_INT")
        for ts, tc, n, data in self.conn.mav.calls:
            self.assertEqual(data[:2], b"\xb5\x62")


def r_cache_lat(path):
    return agps.FixCache(path).load()["lat"]


if __name__ == "__main__":
    unittest.main()
