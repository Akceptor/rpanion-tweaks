import json
import math
import os
import tempfile
import unittest

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from lima_frames import decode, frame
from lima_loader import load_lima

lima = load_lima()

REAL = (47.0, 8.0)
FAKE = (-12.07, -77.0435)


def gpi(lat, lon, **kw):
    f = dict(time_boot_ms=0, lat=round(lat * 1e7), lon=round(lon * 1e7), alt=500000,
             relative_alt=1500, vx=10, vy=-20, vz=3, hdg=9000)
    f.update(kw)
    return mavlink.MAVLink_global_position_int_message(**f)


def gps(lat, lon, fix):
    return mavlink.MAVLink_gps_raw_int_message(
        0, fix, round(lat * 1e7), round(lon * 1e7), 500000, 100, 100, 0, 0, 10)


def heartbeat():
    return mavlink.MAVLink_heartbeat_message(10, 3, 0, 0, 4, 3)


def item_int(cmd, lat, lon, frame_=mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT):
    return mavlink.MAVLink_mission_item_int_message(
        1, 1, 0, frame_, cmd, 0, 1, 0, 0, 0, 0,
        round(lat * 1e7), round(lon * 1e7), 10.0)


class DownlinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "anchor.json")
        self.store = lima.AnchorStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def rewriter(self):
        return lima.Rewriter(self.store, FAKE, 5000.0)

    def anchored(self):
        rw = self.rewriter()
        rw.downlink(frame(gps(*REAL, 3)))
        return rw

    def test_no_anchor_drops_positions(self):
        rw = self.rewriter()
        self.assertEqual(rw.downlink(frame(gpi(*REAL))), [])
        hb = frame(heartbeat())
        self.assertEqual(rw.downlink(hb), [hb])

    def test_anchor_from_first_3d_fix(self):
        rw = self.rewriter()
        self.assertEqual(rw.downlink(frame(gps(*REAL, 2))), [])
        self.assertIsNone(rw.anchor)
        self.assertIsNone(self.store.load())
        out = decode(b"".join(rw.downlink(frame(gps(*REAL, 3)))))
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].lat, round(FAKE[0] * 1e7), delta=1)
        self.assertAlmostEqual(out[0].lon, round(FAKE[1] * 1e7), delta=1)
        self.assertEqual((rw.anchor.real_lat, rw.anchor.real_lon), REAL)
        saved = self.store.load()
        self.assertEqual((saved.fake_lat, saved.fake_lon), FAKE)

    def test_global_position_offset(self):
        rw = self.anchored()
        north = REAL[0] + 100 / (lima.R_EARTH * math.pi / 180)
        out = decode(b"".join(rw.downlink(frame(gpi(north, REAL[1])))))[0]
        d = lima.distance_m(FAKE[0], FAKE[1], out.lat / 1e7, out.lon / 1e7)
        self.assertAlmostEqual(d, 100, delta=0.1)
        self.assertGreater(out.lat / 1e7, FAKE[0])
        self.assertEqual((out.vx, out.vy, out.hdg, out.alt, out.relative_alt),
                         (10, -20, 9000, 500000, 1500))

    def test_mission_item_int_converted_only_for_position_commands(self):
        rw = self.anchored()
        wp = item_int(mavlink.MAV_CMD_NAV_WAYPOINT, *REAL)
        out = decode(b"".join(rw.downlink(frame(wp))))[0]
        self.assertAlmostEqual(out.x, round(FAKE[0] * 1e7), delta=1)
        self.assertAlmostEqual(out.y, round(FAKE[1] * 1e7), delta=1)
        speed = item_int(mavlink.MAV_CMD_DO_CHANGE_SPEED, 0, 0)
        data = frame(speed)
        self.assertEqual(rw.downlink(data), [data])
        other = item_int(mavlink.MAV_CMD_DO_JUMP, *REAL)
        self.assertEqual(rw.downlink(frame(other)), [])

    def test_mission_item_int_local_frame_and_no_anchor(self):
        local = frame(item_int(mavlink.MAV_CMD_NAV_WAYPOINT, 1, 2, mavlink.MAV_FRAME_LOCAL_NED))
        self.assertEqual(self.rewriter().downlink(local), [local])
        wp = frame(item_int(mavlink.MAV_CMD_NAV_WAYPOINT, *REAL))
        self.assertEqual(self.rewriter().downlink(wp), [])

    def test_float_command_messages(self):
        rw = self.anchored()
        item = mavlink.MAVLink_mission_item_message(
            1, 1, 0, mavlink.MAV_FRAME_GLOBAL, mavlink.MAV_CMD_NAV_WAYPOINT,
            0, 1, 0, 0, 0, 0, REAL[0], REAL[1], 10.0)
        out = decode(b"".join(rw.downlink(frame(item))))[0]
        self.assertLess(lima.distance_m(out.x, out.y, *FAKE), 1)
        cmd = mavlink.MAVLink_command_long_message(
            1, 1, mavlink.MAV_CMD_DO_REPOSITION, 0, 0, 0, 0, 0, REAL[0], REAL[1], 10.0)
        out = decode(b"".join(rw.downlink(frame(cmd))))[0]
        self.assertLess(lima.distance_m(out.param5, out.param6, *FAKE), 1)

    def test_zero_latlon_untouched(self):
        rw = self.anchored()
        home = mavlink.MAVLink_home_position_message(0, 0, 100, 0, 0, 0, [1, 0, 0, 0], 0, 0, 0)
        out = decode(b"".join(rw.downlink(frame(home))))[0]
        self.assertEqual((out.latitude, out.longitude), (0, 0))

    def test_v1_frame_stays_v1(self):
        rw = self.anchored()
        data = frame(gpi(*REAL), sysid=1, compid=1, seq=42, v1=True)
        out = rw.downlink(data)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], 0xFE)
        msg = decode(out[0])[0]
        self.assertEqual((msg.get_srcSystem(), msg.get_srcComponent(), msg.get_seq()), (1, 1, 42))
        self.assertAlmostEqual(msg.lat, round(FAKE[0] * 1e7), delta=1)

    def test_v2_header_preserved(self):
        rw = self.anchored()
        out = rw.downlink(frame(gpi(*REAL), sysid=7, compid=190, seq=200))
        self.assertEqual(out[0][0], 0xFD)
        msg = decode(out[0])[0]
        self.assertEqual((msg.get_srcSystem(), msg.get_srcComponent(), msg.get_seq()), (7, 190, 200))

    def test_reuses_stored_anchor(self):
        self.store.save(lima.Anchor(1.0, 2.0, *FAKE))
        rw = self.rewriter()
        self.assertEqual(rw.anchor.real_lat, 1.0)
        rw.downlink(frame(gps(*REAL, 3)))
        self.assertEqual(rw.anchor.real_lat, 1.0)
        out = decode(b"".join(rw.downlink(frame(gpi(1.0, 2.0)))))[0]
        self.assertAlmostEqual(out.lat, round(FAKE[0] * 1e7), delta=1)

    def test_corrupt_store_reanchors(self):
        with open(self.path, "w") as f:
            f.write("{garbage")
        rw = self.rewriter()
        self.assertIsNone(rw.anchor)
        rw.downlink(frame(gps(*REAL, 3)))
        self.assertEqual(self.store.load().real_lat, REAL[0])

    def test_multi_packet_and_garbage(self):
        rw = self.anchored()
        data = frame(heartbeat()) + b"\x00\xff junk" + frame(gpi(*REAL))
        out = rw.downlink(data)
        self.assertEqual(len(out), 2)
        msgs = decode(b"".join(out))
        self.assertEqual(msgs[0].get_type(), "HEARTBEAT")
        self.assertAlmostEqual(msgs[1].lat, round(FAKE[0] * 1e7), delta=1)

    def test_tracks_real_pos(self):
        rw = self.anchored()
        rw.downlink(frame(gpi(47.001, 8.002)))
        self.assertAlmostEqual(rw.real_pos[0], 47.001, places=6)
        self.assertAlmostEqual(rw.real_pos[1], 8.002, places=6)

    def test_unlisted_message_dropped(self):
        rw = self.anchored()
        msg = mavlink.MAVLink_gps_inject_data_message(0, 0, 1, bytes(110))
        self.assertEqual(rw.downlink(frame(msg)), [])


if __name__ == "__main__":
    unittest.main()
