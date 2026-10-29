import json
import math
import os
import struct
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


def bad_gps(lat, lon):
    return mavlink.MAVLink_gps_raw_int_message(0, 3, lat, lon, 500000, 100, 100, 0, 0, 10)


def bad_gpi(lat, lon):
    return mavlink.MAVLink_global_position_int_message(0, lat, lon, 500000, 1500, 0, 0, 0, 9000)


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
        home = mavlink.MAVLink_home_position_message(
            round(REAL[0] * 1e7), round(REAL[1] * 1e7), 100, 0, 0, 0, [1, 0, 0, 0], 0, 0, 0)
        self.assertEqual(rw.downlink(frame(home)), [])
        hb = frame(heartbeat())
        self.assertEqual(rw.downlink(hb), [hb])

    def test_no_anchor_gps_raw_without_fix_passes_with_zero_coords(self):
        rw = self.rewriter()
        data = frame(gps(*REAL, 1))
        out = rw.downlink(data)
        self.assertEqual(len(out), 1)
        m = decode(out[0])[0]
        self.assertEqual((m.fix_type, m.satellites_visible, m.eph, m.epv), (1, 10, 100, 100))
        self.assertEqual((m.lat, m.lon, m.alt), (0, 0, 500000))
        self.assertNotIn(struct.pack("<i", round(REAL[0] * 1e7)), out[0])
        self.assertNotIn(struct.pack("<i", round(REAL[1] * 1e7)), out[0])
        self.assertIsNone(rw.anchor)

    def test_no_anchor_gps2_raw_passes_with_zero_coords(self):
        rw = self.rewriter()
        msg = mavlink.MAVLink_gps2_raw_message(
            0, 2, round(REAL[0] * 1e7), round(REAL[1] * 1e7), 500000, 100, 100, 0, 0, 9, 0, 0)
        out = rw.downlink(frame(msg))
        self.assertEqual(len(out), 1)
        m = decode(out[0])[0]
        self.assertEqual((m.fix_type, m.satellites_visible, m.eph), (2, 9, 100))
        self.assertEqual((m.lat, m.lon), (0, 0))
        self.assertNotIn(struct.pack("<i", round(REAL[0] * 1e7)), out[0])
        self.assertNotIn(struct.pack("<i", round(REAL[1] * 1e7)), out[0])

    def test_no_anchor_gps_raw_fix3_anchors_then_converts(self):
        rw = self.rewriter()
        out = rw.downlink(frame(gps(*REAL, 3)))
        self.assertIsNotNone(rw.anchor)
        m = decode(out[0])[0]
        self.assertLess(lima.distance_m(m.lat / 1e7, m.lon / 1e7, *FAKE), 1)

    def test_no_anchor_gpi_shows_fake_point(self):
        rw = self.rewriter()
        out = rw.downlink(frame(gpi(*REAL), sysid=1, compid=1, seq=7))
        self.assertEqual(len(out), 1)
        m = decode(out[0])[0]
        self.assertEqual((m.lat, m.lon), (round(FAKE[0] * 1e7), round(FAKE[1] * 1e7)))
        self.assertEqual((m.alt, m.relative_alt, m.vx, m.vy, m.vz, m.hdg), (500000, 1500, 10, -20, 3, 9000))
        self.assertEqual((m.get_srcSystem(), m.get_srcComponent(), m.get_seq()), (1, 1, 7))
        self.assertNotIn(struct.pack("<i", round(REAL[0] * 1e7)), out[0])
        self.assertNotIn(struct.pack("<i", round(REAL[1] * 1e7)), out[0])

    def test_no_anchor_gpi_v1_stays_v1(self):
        rw = self.rewriter()
        out = rw.downlink(frame(gpi(*REAL), v1=True))
        self.assertEqual(out[0][0], 0xFE)
        m = decode(out[0])[0]
        self.assertEqual((m.lat, m.lon), (round(FAKE[0] * 1e7), round(FAKE[1] * 1e7)))
        self.assertNotIn(struct.pack("<i", round(REAL[0] * 1e7)), out[0])

    def test_no_anchor_half_unset_gpi_leaks_nothing(self):
        rw = self.rewriter()
        for lat, lon in ((round(REAL[0] * 1e7), 0), (0, round(REAL[1] * 1e7)),
                         (lima.INT32_MAX, round(REAL[1] * 1e7))):
            out = rw.downlink(frame(bad_gpi(lat, lon)))
            m = decode(b"".join(out))[0]
            self.assertEqual((m.lat, m.lon), (round(FAKE[0] * 1e7), round(FAKE[1] * 1e7)))

    def test_no_anchor_zero_gpi_untouched(self):
        rw = self.rewriter()
        data = frame(gpi(0, 0))
        self.assertEqual(rw.downlink(data), [data])

    def test_gpi_never_anchors(self):
        rw = self.rewriter()
        for _ in range(100):
            rw.downlink(frame(gpi(50.0, 30.0)))
        self.assertIsNone(rw.anchor)
        self.assertIsNone(rw.real_pos)
        self.assertFalse(os.path.exists(self.path))

    def test_spoof_then_fix_anchors_on_fix(self):
        rw = self.rewriter()
        for _ in range(3):
            rw.downlink(frame(gpi(50.0, 30.0)))
        rw.downlink(frame(gps(*REAL, 3)))
        self.assertEqual((rw.anchor.real_lat, rw.anchor.real_lon), REAL)
        saved = self.store.load()
        self.assertEqual((saved.real_lat, saved.real_lon), REAL)

    def test_late_spoof_after_fix_does_not_move_anchor_or_guard(self):
        rw = self.rewriter()
        rw.downlink(frame(gps(*REAL, 3)))
        out = decode(b"".join(rw.downlink(frame(gpi(50.0, 30.0)))))[0]
        self.assertEqual((rw.anchor.real_lat, rw.anchor.real_lon), REAL)
        self.assertEqual(rw.real_pos, REAL)
        want = rw.anchor.to_fake(50.0, 30.0)
        self.assertAlmostEqual(out.lat / 1e7, want[0], places=6)
        self.assertAlmostEqual(out.lon / 1e7, want[1], places=6)

    def test_anchor_from_first_3d_fix(self):
        rw = self.rewriter()
        rw.downlink(frame(gps(*REAL, 2)))
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
        other = frame(item_int(mavlink.MAV_CMD_DO_JUMP, *REAL))
        self.assertEqual(rw.downlink(other), [other])
        self.assertNotIn(65000, mavlink.enums["MAV_CMD"])
        self.assertEqual(rw.downlink(frame(item_int(65000, *REAL))), [])

    def test_mission_item_int_local_frame_dropped_and_no_anchor(self):
        local = frame(item_int(mavlink.MAV_CMD_NAV_WAYPOINT, 1, 2, mavlink.MAV_FRAME_LOCAL_NED))
        self.assertEqual(self.rewriter().downlink(local), [])
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

    def test_mission_frame_converted_and_local_dropped(self):
        rw = self.anchored()
        out = decode(b"".join(rw.downlink(frame(
            item_int(mavlink.MAV_CMD_NAV_WAYPOINT, *REAL, mavlink.MAV_FRAME_MISSION)))))
        self.assertLess(lima.distance_m(out[0].x / 1e7, out[0].y / 1e7, *FAKE), 1)
        cmd = lambda fr: mavlink.MAVLink_command_int_message(
            1, 1, fr, mavlink.MAV_CMD_DO_REPOSITION, 0, 0, 0, 0, 0, 0,
            round(REAL[0] * 1e7), round(REAL[1] * 1e7), 10.0)
        out = decode(b"".join(rw.downlink(frame(cmd(mavlink.MAV_FRAME_MISSION)))))
        self.assertLess(lima.distance_m(out[0].x / 1e7, out[0].y / 1e7, *FAKE), 1)
        self.assertEqual(rw.downlink(frame(cmd(mavlink.MAV_FRAME_LOCAL_NED))), [])

    def test_unset_markers_untouched_down(self):
        rw = self.anchored()
        m = lima.INT32_MAX
        for msg in (
            mavlink.MAVLink_command_int_message(
                1, 1, mavlink.MAV_FRAME_GLOBAL, mavlink.MAV_CMD_DO_REPOSITION,
                0, 0, 0, 0, 0, 0, m, m, 10.0),
            mavlink.MAVLink_command_long_message(
                1, 1, mavlink.MAV_CMD_DO_REPOSITION, 0, 0, 0, 0, 0,
                float("nan"), float("nan"), 10.0),
        ):
            data = frame(msg)
            self.assertEqual(rw.downlink(data), [data], msg.get_type())

    def test_adsb_dropped_down(self):
        self.assertEqual(lima.classify("down", "ADSB_VEHICLE"), "drop")
        rw = self.anchored()
        msg = mavlink.MAVLink_adsb_vehicle_message(
            1, round(REAL[0] * 1e7), round(REAL[1] * 1e7), 0, 0, 0, 0, 0, b"CALL", 0, 0, 0, 0)
        self.assertEqual(rw.downlink(frame(msg)), [])

    def test_protocol_version_passes_down(self):
        self.assertEqual(lima.classify("down", "PROTOCOL_VERSION"), "pass")

    def test_changed_fake_anchor_rebuilds_stored(self):
        self.store.save(lima.Anchor(1.0, 2.0, -11.0, -76.0))
        rw = self.rewriter()
        self.assertEqual((rw.anchor.real_lat, rw.anchor.real_lon), (1.0, 2.0))
        self.assertEqual((rw.anchor.fake_lat, rw.anchor.fake_lon), FAKE)
        saved = self.store.load()
        self.assertEqual((saved.real_lat, saved.fake_lat, saved.fake_lon), (1.0, *FAKE))

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

    def test_real_pos_from_gps_only(self):
        rw = self.anchored()
        rw.downlink(frame(gpi(47.001, 8.002)))
        self.assertEqual(rw.real_pos, REAL)
        rw.downlink(frame(gps(47.001, 8.002, 3)))
        self.assertAlmostEqual(rw.real_pos[0], 47.001, places=6)
        self.assertAlmostEqual(rw.real_pos[1], 8.002, places=6)
        rw.downlink(frame(gps(47.5, 8.5, 2)))
        self.assertAlmostEqual(rw.real_pos[0], 47.001, places=6)

    def test_track_ignores_invalid_latlon(self):
        rw = self.rewriter()
        rw.downlink(frame(bad_gps(lima.INT32_MAX, lima.INT32_MAX)))
        rw.downlink(frame(bad_gps(910000000, 0)))
        rw.downlink(frame(bad_gps(0, -1810000000)))
        self.assertIsNone(rw.anchor)
        rw.downlink(frame(gps(*REAL, 3)))
        rw.downlink(frame(bad_gps(lima.INT32_MAX, lima.INT32_MAX)))
        rw.downlink(frame(bad_gps(0, 1810000000)))
        self.assertEqual(rw.real_pos, REAL)

    def test_unlisted_message_dropped(self):
        rw = self.anchored()
        msg = mavlink.MAVLink_gps_inject_data_message(0, 0, 1, bytes(110))
        self.assertEqual(rw.downlink(frame(msg)), [])


if __name__ == "__main__":
    unittest.main()
