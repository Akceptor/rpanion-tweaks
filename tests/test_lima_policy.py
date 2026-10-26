import unittest

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from lima_loader import load_lima

lima = load_lima()

LATLON = {"lat", "lon", "lng", "latitude", "longitude", "lat_int", "lon_int"}
OPAQUE = [
    "FILE_TRANSFER_PROTOCOL", "LOG_ENTRY", "LOG_DATA", "LOG_REQUEST_LIST",
    "LOG_REQUEST_DATA", "LOG_REQUEST_END", "LOG_ERASE", "SERIAL_CONTROL",
    "FOLLOW_TARGET",
]


def dialect_messages():
    return {c.msgname: c for c in mavlink.mavlink_map.values()}


def carries_position(cls):
    f = set(cls.fieldnames)
    return bool(f & LATLON) or {"frame", "x", "y"} <= f or {"param5", "param6"} <= f


def has_big_byte_array(cls):
    return any(t == "uint8_t" and n >= 32 for t, n in zip(cls.fieldtypes, cls.array_lengths))


class PolicyTest(unittest.TestCase):
    def test_no_position_message_passes(self):
        for name, cls in dialect_messages().items():
            if carries_position(cls):
                for d in ("down", "up"):
                    self.assertNotEqual(lima.classify(d, name), "pass", (d, name))

    def test_no_big_byte_array_passes(self):
        for name, cls in dialect_messages().items():
            if has_big_byte_array(cls):
                for d in ("down", "up"):
                    if d == "up" and name in ("GPS_RTCM_DATA", "GPS_INJECT_DATA"):
                        continue
                    self.assertNotEqual(lima.classify(d, name), "pass", (d, name))

    def test_big_byte_array_detector_sees_known_messages(self):
        msgs = dialect_messages()
        self.assertTrue(has_big_byte_array(msgs["FILE_TRANSFER_PROTOCOL"]))
        self.assertFalse(has_big_byte_array(msgs["HEARTBEAT"]))

    def test_convert_fields_exist(self):
        msgs = dialect_messages()
        for name, (lat, lon, scale) in lima.CONVERT.items():
            cls = msgs[name]
            for f in (lat, lon):
                self.assertIn(f, cls.fieldnames, (name, f))
                units = cls.fieldunits_by_name.get(f)
                if units == "degE7":
                    self.assertEqual(scale, 1e7, (name, f))
                elif units == "deg":
                    self.assertEqual(scale, 1.0, (name, f))

    def test_position_commands_and_frames(self):
        self.assertIn(mavlink.MAV_CMD_NAV_WAYPOINT, lima.POSITION_COMMANDS)
        self.assertIn(mavlink.MAV_CMD_DO_REPOSITION, lima.POSITION_COMMANDS)
        self.assertIn(mavlink.MAV_FRAME_GLOBAL_INT, lima.GLOBAL_FRAMES)
        self.assertNotIn(mavlink.MAV_FRAME_LOCAL_NED, lima.GLOBAL_FRAMES)

    def test_classes(self):
        for d in ("down", "up"):
            self.assertEqual(lima.classify(d, "GPS_RAW_INT"), "convert")
            self.assertEqual(lima.classify(d, "MISSION_ITEM_INT"), "command")
            self.assertEqual(lima.classify(d, "COMMAND_LONG"), "command")
            self.assertEqual(lima.classify(d, "HEARTBEAT"), "pass")

    def test_opaque_dropped(self):
        for name in OPAQUE:
            for d in ("down", "up"):
                self.assertEqual(lima.classify(d, name), "drop", (d, name))
        self.assertEqual(lima.classify("up", "GPS_RTCM_DATA"), "pass")
        self.assertEqual(lima.classify("down", "GPS_RTCM_DATA"), "drop")
        self.assertEqual(lima.classify("up", "GPS_INJECT_DATA"), "pass")
        self.assertEqual(lima.classify("down", "GPS_INJECT_DATA"), "drop")

    def test_unknown_dropped(self):
        self.assertEqual(lima.classify("down", "NOT_A_MESSAGE"), "drop")
        self.assertEqual(lima.classify("up", "NOT_A_MESSAGE"), "drop")


if __name__ == "__main__":
    unittest.main()
