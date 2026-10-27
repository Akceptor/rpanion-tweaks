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
M_DEG = lima.R_EARTH * math.pi / 180


def offset(lat, lon, north, east):
    return (lat + north / M_DEG,
            lon + east / (M_DEG * math.cos(math.radians(lat))))


def gps(lat, lon, fix=3):
    return mavlink.MAVLink_gps_raw_int_message(
        0, fix, round(lat * 1e7), round(lon * 1e7), 500000, 100, 100, 0, 0, 10)


def gpi(lat, lon):
    return mavlink.MAVLink_global_position_int_message(
        0, round(lat * 1e7), round(lon * 1e7), 500000, 1500, 0, 0, 0, 9000)


def item_int(lat, lon, seq=1, cmd=mavlink.MAV_CMD_NAV_WAYPOINT,
             frame_=mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT):
    return mavlink.MAVLink_mission_item_int_message(
        1, 1, seq, frame_, cmd, 0, 1,
        0, 0, 0, 0, round(lat * 1e7), round(lon * 1e7), 10.0)


def cmd_long(cmd, p1=0, p5=0, p6=0, p7=0):
    return mavlink.MAVLink_command_long_message(1, 1, cmd, 0, p1, 0, 0, 0, p5, p6, p7)


def cmd_int(cmd, x, y, frame_=mavlink.MAV_FRAME_GLOBAL):
    return mavlink.MAVLink_command_int_message(1, 1, frame_, cmd, 0, 0, 0, 0, 0, 0, x, y, 10.0)


class UplinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = lima.AnchorStore(os.path.join(self.tmp.name, "anchor.json"))
        self.now = 100.0

    def tearDown(self):
        self.tmp.cleanup()

    def anchored(self, with_pos=True):
        rw = lima.Rewriter(self.store, FAKE, 5000.0, clock=lambda: self.now)
        rw.downlink(frame(gps(*REAL)))
        if not with_pos:
            rw.real_pos = None
        return rw

    def fake_point(self, north, east):
        return offset(*FAKE, north, east)

    def test_mission_item_back_to_real(self):
        rw = self.anchored()
        out = decode(b"".join(rw.uplink(frame(item_int(*self.fake_point(14.142, 14.142))))))
        self.assertEqual(len(out), 1)
        d = lima.distance_m(*REAL, out[0].x / 1e7, out[0].y / 1e7)
        self.assertAlmostEqual(d, 20.0, delta=0.05)
        self.assertGreater(out[0].x / 1e7, REAL[0])
        self.assertGreater(out[0].y / 1e7, REAL[1])

    def test_float_forms_within_1m(self):
        rw = self.anchored()
        lat, lon = self.fake_point(30, -40)
        want = offset(*REAL, 30, -40)
        msgs = [
            mavlink.MAVLink_mission_item_message(
                1, 1, 1, mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                mavlink.MAV_CMD_NAV_WAYPOINT, 0, 1, 0, 0, 0, 0, lat, lon, 10.0),
            cmd_long(mavlink.MAV_CMD_DO_SET_HOME, 0, lat, lon, 10.0),
            mavlink.MAVLink_fence_point_message(1, 1, 0, 1, lat, lon),
        ]
        for m in msgs:
            out = decode(b"".join(rw.uplink(frame(m))))
            self.assertEqual(len(out), 1, m.get_type())
            o = out[0]
            if o.get_type() == "MISSION_ITEM":
                got = (o.x, o.y)
            elif o.get_type() == "COMMAND_LONG":
                got = (o.param5, o.param6)
            else:
                got = (o.lat, o.lng)
            self.assertLess(lima.distance_m(*want, *got), 1.0, m.get_type())

    def test_command_long_non_position_untouched(self):
        rw = self.anchored()
        data = frame(cmd_long(mavlink.MAV_CMD_DO_SET_SERVO, 5, 1500, 7))
        self.assertEqual(rw.uplink(data), [data])
        self.assertEqual(rw.take_warnings(), [])

    def test_guard_drops_far_target(self):
        rw = self.anchored()
        self.assertEqual(rw.uplink(frame(item_int(*self.fake_point(6000, 0)))), [])
        w = decode(b"".join(rw.take_warnings()))
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0].get_type(), "STATUSTEXT")
        self.assertEqual(w[0].severity, mavlink.MAV_SEVERITY_WARNING)
        self.assertTrue(w[0].text.startswith("Lima: dropped MISSION_ITEM_INT"))
        self.assertLessEqual(len(w[0].text), 50)
        self.assertEqual(w[0].get_srcSystem(), 1)
        self.assertEqual(w[0].get_srcComponent(), mavlink.MAV_COMP_ID_ONBOARD_COMPUTER)
        self.assertEqual(rw.take_warnings(), [])

    def test_guard_without_real_pos(self):
        rw = self.anchored(with_pos=False)
        self.assertEqual(rw.uplink(frame(item_int(*self.fake_point(10, 0)))), [])
        self.assertEqual(len(rw.take_warnings()), 1)

    def test_guard_without_anchor(self):
        rw = lima.Rewriter(self.store, FAKE, 5000.0, clock=lambda: self.now)
        self.assertEqual(rw.uplink(frame(item_int(*FAKE))), [])
        self.assertEqual(len(rw.take_warnings()), 1)

    def test_mission_frame_converted_and_guarded(self):
        rw = self.anchored()
        near = self.fake_point(14.142, 14.142)
        far = self.fake_point(6000, 0)
        for mk in (
            lambda p: item_int(*p, frame_=mavlink.MAV_FRAME_MISSION),
            lambda p: cmd_int(mavlink.MAV_CMD_DO_REPOSITION, round(p[0] * 1e7),
                              round(p[1] * 1e7), mavlink.MAV_FRAME_MISSION),
        ):
            out = decode(b"".join(rw.uplink(frame(mk(near)))))
            self.assertEqual(len(out), 1)
            d = lima.distance_m(*REAL, out[0].x / 1e7, out[0].y / 1e7)
            self.assertAlmostEqual(d, 20.0, delta=0.05)
            self.assertEqual(rw.uplink(frame(mk(far))), [])
            self.assertEqual(len(rw.take_warnings()), 1)
            self.now += 1.1

    def test_local_frame_position_command_dropped_with_warning(self):
        rw = self.anchored()
        for m in (
            item_int(10.0, 20.0, frame_=mavlink.MAV_FRAME_LOCAL_NED),
            cmd_int(mavlink.MAV_CMD_DO_REPOSITION, 100, 200, mavlink.MAV_FRAME_LOCAL_NED),
        ):
            m.x, m.y = 100, 200
            self.assertEqual(rw.uplink(frame(m)), [])
            self.assertEqual(len(rw.take_warnings()), 1)
            self.assertEqual(rw.take_warnings(), [])
            self.now += 1.1

    def test_known_non_position_command_passes_with_xy(self):
        rw = self.anchored()
        for m in (
            item_int(1.0, 2.0, cmd=mavlink.MAV_CMD_DO_DIGICAM_CONTROL),
            item_int(1.0, 2.0, cmd=mavlink.MAV_CMD_DO_JUMP,
                     frame_=mavlink.MAV_FRAME_LOCAL_NED),
            cmd_long(mavlink.MAV_CMD_DO_DIGICAM_CONTROL, 0, 3, 4, 5),
        ):
            data = frame(m)
            self.assertEqual(rw.uplink(data), [data], m.get_type())
        self.assertEqual(rw.take_warnings(), [])

    def test_unknown_command_with_xy_dropped_with_warning(self):
        self.assertNotIn(65000, mavlink.enums["MAV_CMD"])
        rw = self.anchored()
        self.assertEqual(rw.uplink(frame(item_int(1.0, 2.0, cmd=65000))), [])
        self.assertEqual(len(rw.take_warnings()), 1)
        self.now += 1.1
        self.assertEqual(rw.uplink(frame(cmd_long(65000, 0, 3, 4, 5))), [])
        self.assertEqual(len(rw.take_warnings()), 1)
        data = frame(item_int(0, 0, cmd=65000))
        self.assertEqual(rw.uplink(data), [data])

    def test_unset_markers_pass_untouched(self):
        rw = self.anchored()
        m_max = lima.INT32_MAX
        nan = float("nan")
        for m in (
            cmd_int(mavlink.MAV_CMD_DO_REPOSITION, m_max, m_max),
            cmd_int(mavlink.MAV_CMD_DO_REPOSITION, 0, m_max),
            mavlink.MAVLink_mission_item_int_message(
                1, 1, 0, mavlink.MAV_FRAME_GLOBAL, mavlink.MAV_CMD_NAV_WAYPOINT,
                0, 1, 0, 0, 0, 0, m_max, m_max, 10.0),
            cmd_long(mavlink.MAV_CMD_DO_REPOSITION, 0, nan, nan, 10.0),
            cmd_long(mavlink.MAV_CMD_DO_REPOSITION, 0, 0, nan, 10.0),
            mavlink.MAVLink_mission_item_message(
                1, 1, 0, mavlink.MAV_FRAME_GLOBAL, mavlink.MAV_CMD_NAV_WAYPOINT,
                0, 1, 0, 0, 0, 0, nan, nan, 10.0),
        ):
            data = frame(m)
            self.assertEqual(rw.uplink(data), [data], m.get_type())
        self.assertEqual(rw.take_warnings(), [])

    def test_command_long_guard(self):
        for cmd in (mavlink.MAV_CMD_DO_REPOSITION, mavlink.MAV_CMD_DO_SET_HOME):
            rw = self.anchored()
            lat, lon = self.fake_point(6000, 0)
            self.assertEqual(rw.uplink(frame(cmd_long(cmd, 0, lat, lon, 10))), [])
            self.assertEqual(len(rw.take_warnings()), 1)

    def test_command_int_guard(self):
        rw = self.anchored()
        lat, lon = self.fake_point(6000, 0)
        m = cmd_int(mavlink.MAV_CMD_DO_REPOSITION, round(lat * 1e7), round(lon * 1e7))
        self.assertEqual(rw.uplink(frame(m)), [])
        w = decode(b"".join(rw.take_warnings()))
        self.assertEqual(len(w), 1)
        self.assertTrue(w[0].text.startswith("Lima: dropped COMMAND_INT"))

    def test_v1_uplink_stays_v1(self):
        rw = self.anchored()
        data = frame(item_int(*self.fake_point(14.142, 14.142)), sysid=255, compid=190,
                     seq=77, v1=True)
        out = rw.uplink(data)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], 0xFE)
        msg = decode(out[0])[0]
        self.assertEqual((msg.get_srcSystem(), msg.get_srcComponent(), msg.get_seq()),
                         (255, 190, 77))
        self.assertAlmostEqual(lima.distance_m(*REAL, msg.x / 1e7, msg.y / 1e7), 20.0, delta=0.05)

    def test_adsb_dropped_up(self):
        self.assertEqual(lima.classify("up", "ADSB_VEHICLE"), "drop")

    def test_protocol_version_passes_up(self):
        self.assertEqual(lima.classify("up", "PROTOCOL_VERSION"), "pass")

    def test_mission_upload_partial_drop(self):
        rw = self.anchored()
        data = b"".join(
            frame(item_int(*self.fake_point(100 if i != 5 else 6000, i), seq=i))
            for i in range(10))
        out = decode(b"".join(rw.uplink(data)))
        self.assertEqual([m.seq for m in out], [0, 1, 2, 3, 4, 6, 7, 8, 9])
        self.assertEqual(len(rw.take_warnings()), 1)

    def test_velocity_only_target_passes(self):
        rw = self.anchored()
        m = mavlink.MAVLink_set_position_target_global_int_message(
            0, 1, 1, mavlink.MAV_FRAME_GLOBAL_INT,
            mavlink.POSITION_TARGET_TYPEMASK_X_IGNORE
            | mavlink.POSITION_TARGET_TYPEMASK_Y_IGNORE
            | mavlink.POSITION_TARGET_TYPEMASK_Z_IGNORE,
            1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        data = frame(m)
        self.assertEqual(rw.uplink(data), [data])
        self.assertEqual(rw.take_warnings(), [])
        self.assertEqual(self.anchored(with_pos=False).uplink(data), [data])

    def sp_target(self, mask, lat, lon):
        return mavlink.MAVLink_set_position_target_global_int_message(
            0, 1, 1, mavlink.MAV_FRAME_GLOBAL_INT, mask, lat, lon,
            10, 0, 0, 0, 0, 0, 0, 0, 0)

    def test_zero_latlon_uplink_guarded(self):
        for name, m in (
            ("SET_POSITION_TARGET_GLOBAL_INT", self.sp_target(0, 0, 0)),
            ("SET_HOME_POSITION", mavlink.MAVLink_set_home_position_message(
                1, 0, 0, 0, 0, 0, 0, [1, 0, 0, 0], 0, 0, 0)),
            ("SET_GPS_GLOBAL_ORIGIN", mavlink.MAVLink_set_gps_global_origin_message(1, 0, 0, 0)),
        ):
            rw = self.anchored()
            self.assertEqual(rw.uplink(frame(m)), [], name)
            self.assertEqual(len(rw.take_warnings()), 1, name)

    def test_mask_shortcut_needs_xyz(self):
        rw = self.anchored()
        lat, lon = self.fake_point(6000, 0)
        xy = (mavlink.POSITION_TARGET_TYPEMASK_X_IGNORE
              | mavlink.POSITION_TARGET_TYPEMASK_Y_IGNORE)
        self.assertEqual(rw.uplink(frame(self.sp_target(xy, round(lat * 1e7), round(lon * 1e7)))), [])
        self.assertEqual(len(rw.take_warnings()), 1)
        lat, lon = self.fake_point(10, 0)
        out = decode(b"".join(rw.uplink(frame(self.sp_target(xy, round(lat * 1e7), round(lon * 1e7))))))
        self.assertLess(lima.distance_m(*REAL, out[0].lat_int / 1e7, out[0].lon_int / 1e7), 11)

    def test_mask_shortcut_not_on_downlink(self):
        rw = self.anchored()
        mask = (mavlink.POSITION_TARGET_TYPEMASK_X_IGNORE
                | mavlink.POSITION_TARGET_TYPEMASK_Y_IGNORE
                | mavlink.POSITION_TARGET_TYPEMASK_Z_IGNORE)
        data = frame(self.sp_target(mask, round(REAL[0] * 1e7), round(REAL[1] * 1e7)))
        out = decode(b"".join(rw.downlink(data)))
        self.assertEqual(len(out), 1)
        self.assertLess(lima.distance_m(*FAKE, out[0].lat_int / 1e7, out[0].lon_int / 1e7), 1)

    def test_do_orbit_converted(self):
        rw = self.anchored()
        lat, lon = self.fake_point(30, 0)
        out = decode(b"".join(rw.uplink(frame(
            cmd_long(mavlink.MAV_CMD_DO_ORBIT, 10, lat, lon, 10)))))
        self.assertEqual(len(out), 1)
        self.assertLess(lima.distance_m(*offset(*REAL, 30, 0), out[0].param5, out[0].param6), 1.0)

    def test_position_target_far_dropped(self):
        rw = self.anchored()
        lat, lon = self.fake_point(6000, 0)
        m = mavlink.MAVLink_set_position_target_global_int_message(
            0, 1, 1, mavlink.MAV_FRAME_GLOBAL_INT, 0, round(lat * 1e7), round(lon * 1e7),
            10, 0, 0, 0, 0, 0, 0, 0, 0)
        self.assertEqual(rw.uplink(frame(m)), [])
        self.assertEqual(len(rw.take_warnings()), 1)

    def test_rtcm_passes_up(self):
        rw = self.anchored()
        data = frame(mavlink.MAVLink_gps_rtcm_data_message(0, 5, bytes(180)))
        self.assertEqual(rw.uplink(data), [data])

    def test_ftp_dropped_up(self):
        rw = self.anchored()
        m = mavlink.MAVLink_file_transfer_protocol_message(0, 1, 1, bytes(251))
        self.assertEqual(rw.uplink(frame(m)), [])

    def test_warning_rate_limit(self):
        rw = self.anchored()
        far = frame(item_int(*self.fake_point(6000, 0)))
        rw.uplink(far)
        rw.uplink(far)
        self.assertEqual(len(rw.take_warnings()), 1)
        self.now += 1.1
        rw.uplink(far)
        self.assertEqual(len(rw.take_warnings()), 1)

    def test_warning_sysid_from_vehicle_heartbeat(self):
        rw = self.anchored()
        gcs = mavlink.MAVLink_heartbeat_message(mavlink.MAV_TYPE_GCS, 8, 0, 0, 4, 3)
        rw.downlink(frame(gcs, sysid=255))
        veh = mavlink.MAVLink_heartbeat_message(10, 3, 0, 0, 4, 3)
        rw.downlink(frame(veh, sysid=7))
        rw.uplink(frame(item_int(*self.fake_point(6000, 0))))
        w = decode(b"".join(rw.take_warnings()))
        self.assertEqual(w[0].get_srcSystem(), 7)


if __name__ == "__main__":
    unittest.main()
