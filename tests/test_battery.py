import importlib.util
import os
import struct
import sys
import unittest
from unittest import mock

for name in ("gi", "gi.repository"):
    sys.modules.setdefault(name, mock.MagicMock())

_path = os.path.join(os.path.dirname(__file__), "..", "python", "video-server.py")
_spec = importlib.util.spec_from_file_location("video_server", _path)
vs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vs)


def ref_x25(data, crc=0xFFFF):
    # Independent bit-by-bit CRC-16/MCRF4XX (reflected 0x1021 -> 0x8408)
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


def with_crc(body, extra):
    crc = vs.crc_x25(body + bytes([extra]))
    return body + struct.pack("<H", crc)


def v1_frame(msgid, payload, extra, compid=1, sysid=1, seq=0):
    return b"\xFE" + with_crc(bytes([len(payload), seq, sysid, compid, msgid]) + payload, extra)


def v2_frame(msgid, payload, extra, compid=1, sysid=1, seq=0, truncate=True, signed=False):
    if truncate:
        payload = payload.rstrip(b"\0") or payload[:1]
    incompat = 1 if signed else 0
    hdr = bytes([len(payload), incompat, 0, seq, sysid, compid]) + msgid.to_bytes(3, "little")
    frame = b"\xFD" + with_crc(hdr + payload, extra)
    if signed:
        frame += b"\xAA" * 13
    return frame


def sys_status_payload(mv=15800, ca=1230, rem=74):
    p = bytearray(31)
    struct.pack_into("<H", p, 14, mv)
    struct.pack_into("<h", p, 16, ca)
    struct.pack_into("<b", p, 30, rem)
    return bytes(p)


def batt_status_payload(mah=1250, batt_id=0):
    p = bytearray(36)
    struct.pack_into("<i", p, 0, mah)
    p[32] = batt_id
    return bytes(p)


class CrcTest(unittest.TestCase):
    def test_reference_vector(self):
        self.assertEqual(vs.crc_x25(b"123456789"), 0x6F91)

    def test_matches_independent_implementation(self):
        data = bytes(range(256))
        self.assertEqual(vs.crc_x25(data), ref_x25(data))

    def test_hardcoded_frame(self):
        # v1 SYS_STATUS built by hand: crc over body + extra 124 computed with ref_x25
        body = bytes([31, 0, 1, 1, 1]) + sys_status_payload()
        frame = b"\xFE" + body + struct.pack("<H", ref_x25(body + b"\x7c"))
        self.assertEqual(vs.parse_mavlink_frames(frame), [(1, 1, sys_status_payload())])


class ParserTest(unittest.TestCase):
    def test_v1(self):
        p = sys_status_payload()
        self.assertEqual(vs.parse_mavlink_frames(v1_frame(1, p, 124)), [(1, 1, p)])

    def test_v2_full(self):
        p = sys_status_payload()
        self.assertEqual(vs.parse_mavlink_frames(v2_frame(1, p, 124, truncate=False)), [(1, 1, p)])

    def test_v2_truncated_is_zero_padded(self):
        p = sys_status_payload(rem=0, ca=0)
        frame = v2_frame(1, p, 124)
        self.assertLess(len(frame), 12 + 31)
        self.assertEqual(vs.parse_mavlink_frames(frame), [(1, 1, p)])

    def test_bad_crc_rejected(self):
        frame = bytearray(v1_frame(1, sys_status_payload(), 124))
        frame[-1] ^= 0xFF
        self.assertEqual(vs.parse_mavlink_frames(bytes(frame)), [])

    def test_wrong_crc_extra_rejected(self):
        self.assertEqual(vs.parse_mavlink_frames(v1_frame(1, sys_status_payload(), 125)), [])

    def test_garbage_before_frame(self):
        p = batt_status_payload()
        buf = b"\x00\xFD\x01junk\xFE" + v2_frame(147, p, 154)
        self.assertEqual(vs.parse_mavlink_frames(buf), [(147, 1, p)])

    def test_two_frames(self):
        a, b = sys_status_payload(), batt_status_payload()
        buf = v1_frame(1, a, 124) + v2_frame(147, b, 154)
        self.assertEqual(vs.parse_mavlink_frames(buf), [(1, 1, a), (147, 1, b)])

    def test_signed_v2_skips_signature(self):
        a, b = sys_status_payload(), batt_status_payload()
        buf = v2_frame(1, a, 124, signed=True) + v2_frame(147, b, 154)
        self.assertEqual(vs.parse_mavlink_frames(buf), [(1, 1, a), (147, 1, b)])

    def test_truncated_buffer(self):
        frame = v2_frame(1, sys_status_payload(), 124)
        self.assertEqual(vs.parse_mavlink_frames(frame[:-3]), [])

    def test_unknown_message_ignored(self):
        self.assertEqual(vs.parse_mavlink_frames(v1_frame(0, b"\0" * 9, 50)), [])

    def test_compid_reported(self):
        p = sys_status_payload()
        self.assertEqual(vs.parse_mavlink_frames(v1_frame(1, p, 124, compid=191)), [(1, 191, p)])


class DecodeTest(unittest.TestCase):
    def test_sys_status(self):
        self.assertEqual(vs.decode_sys_status(sys_status_payload()), (15.8, 12.3, 74))

    def test_sys_status_unknown_sentinels(self):
        p = sys_status_payload(mv=65535, ca=-1, rem=-1)
        self.assertEqual(vs.decode_sys_status(p), (None, None, None))

    def test_battery_status(self):
        self.assertEqual(vs.decode_battery_status(batt_status_payload()), (0, 1250))

    def test_battery_status_unknown(self):
        self.assertEqual(vs.decode_battery_status(batt_status_payload(mah=-1)), (0, None))


class FcStateTest(unittest.TestCase):
    def test_apply_and_line(self):
        st = {}
        vs.apply_mavlink(st, 1, 1, sys_status_payload(), 100.0)
        vs.apply_mavlink(st, 147, 1, batt_status_payload(), 100.0)
        self.assertEqual(vs.fc_line(st, 102.0), "FC 15.8V 12.3A 74% 1250mAh")

    def test_battery_id_nonzero_ignored(self):
        st = {}
        vs.apply_mavlink(st, 1, 1, sys_status_payload(), 100.0)
        vs.apply_mavlink(st, 147, 1, batt_status_payload(batt_id=1), 100.0)
        self.assertEqual(vs.fc_line(st, 100.0), "FC 15.8V 12.3A 74%")

    def test_other_component_ignored(self):
        st = {}
        vs.apply_mavlink(st, 1, 191, sys_status_payload(), 100.0)
        self.assertEqual(vs.fc_line(st, 100.0), "")

    def test_stale_hides_line(self):
        st = {}
        vs.apply_mavlink(st, 1, 1, sys_status_payload(), 100.0)
        self.assertEqual(vs.fc_line(st, 105.5), "")

    def test_unknown_fields_dropped(self):
        st = {}
        vs.apply_mavlink(st, 1, 1, sys_status_payload(ca=-1, rem=-1), 100.0)
        self.assertEqual(vs.fc_line(st, 100.0), "FC 15.8V")

    def test_empty(self):
        self.assertEqual(vs.fc_line({}, 100.0), "")


class Ina226Test(unittest.TestCase):
    def test_conversion(self):
        volts, amps, sat = vs.ina226_convert(6080, 4840, 0.01)
        self.assertAlmostEqual(volts, 7.6)
        self.assertAlmostEqual(amps, 1.21)
        self.assertFalse(sat)

    def test_negative_shunt(self):
        _, amps, sat = vs.ina226_convert(6080, -400, 0.1)
        self.assertAlmostEqual(amps, -0.01)
        self.assertFalse(sat)

    def test_saturation(self):
        _, amps, sat = vs.ina226_convert(6080, 32767, 0.1)
        self.assertTrue(sat)
        self.assertAlmostEqual(amps, 0.8192, places=3)

    def test_negative_saturation(self):
        self.assertTrue(vs.ina226_convert(6080, -32768, 0.1)[2])


class LipoTest(unittest.TestCase):
    def test_table_points(self):
        self.assertEqual(vs.lipo_percent(4.20), 100)
        self.assertEqual(vs.lipo_percent(3.84), 50)
        self.assertEqual(vs.lipo_percent(3.27), 0)

    def test_interpolation(self):
        self.assertAlmostEqual(vs.lipo_percent(3.825), 46.25)

    def test_clamped(self):
        self.assertEqual(vs.lipo_percent(4.5), 100)
        self.assertEqual(vs.lipo_percent(3.0), 0)

    def test_pack_percent(self):
        self.assertEqual(vs.pack_percent(7.68, 2), 50)
        self.assertIsNone(vs.pack_percent(7.68, 0))


class PiLineTest(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(vs.format_pi_line(7.62, 1.21, False, 68), "PI 7.6V 1.2A 68%")

    def test_saturated(self):
        self.assertEqual(vs.format_pi_line(7.62, 0.8192, True, 68), "PI 7.6V 0.8A+ 68%")

    def test_no_percent(self):
        self.assertEqual(vs.format_pi_line(7.62, 1.21, False, None), "PI 7.6V 1.2A")

    def test_no_data(self):
        self.assertEqual(vs.format_pi_line(None, None, False, None), "")


class LimaLineTest(unittest.TestCase):
    def test_off_before_first_datagram(self):
        self.assertEqual(vs.lima_line({}, 100.0), "Lima: OFF")

    def test_on_off_and_stale(self):
        state = {}
        vs.apply_lima(state, b"LIMA 1", 100.0)
        self.assertEqual(vs.lima_line(state, 102.9), "Lima: ON")
        self.assertEqual(vs.lima_line(state, 103.1), "Lima: OFF")
        vs.apply_lima(state, b"LIMA 0", 104.0)
        self.assertEqual(vs.lima_line(state, 104.5), "Lima: OFF")

    def test_garbage_ignored(self):
        state = {}
        vs.apply_lima(state, b"hello", 100.0)
        self.assertEqual(vs.lima_line(state, 100.0), "Lima: OFF")


if __name__ == "__main__":
    unittest.main()
