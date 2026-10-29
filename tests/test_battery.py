import importlib.util
import os
import socket
import struct
import sys
import time
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


def gps_payload(fix=3, sats=12, eph=121, lat=501234567, lon=-301234567):
    p = bytearray(30)
    struct.pack_into("<qiiiHHHH", p, 0, 0, lat, lon, 0, eph, 0, 0, 0)
    p[28], p[29] = fix, sats
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
        self.assertEqual(vs.parse_mavlink_frames(frame), [(1, 1, sys_status_payload(), 1)])


class ParserTest(unittest.TestCase):
    def test_v1(self):
        p = sys_status_payload()
        self.assertEqual(vs.parse_mavlink_frames(v1_frame(1, p, 124)), [(1, 1, p, 1)])

    def test_v2_full(self):
        p = sys_status_payload()
        self.assertEqual(vs.parse_mavlink_frames(v2_frame(1, p, 124, truncate=False)), [(1, 1, p, 1)])

    def test_v2_truncated_is_zero_padded(self):
        p = sys_status_payload(rem=0, ca=0)
        frame = v2_frame(1, p, 124)
        self.assertLess(len(frame), 12 + 31)
        self.assertEqual(vs.parse_mavlink_frames(frame), [(1, 1, p, 1)])

    def test_bad_crc_rejected(self):
        frame = bytearray(v1_frame(1, sys_status_payload(), 124))
        frame[-1] ^= 0xFF
        self.assertEqual(vs.parse_mavlink_frames(bytes(frame)), [])

    def test_wrong_crc_extra_rejected(self):
        self.assertEqual(vs.parse_mavlink_frames(v1_frame(1, sys_status_payload(), 125)), [])

    def test_garbage_before_frame(self):
        p = batt_status_payload()
        buf = b"\x00\xFD\x01junk\xFE" + v2_frame(147, p, 154)
        self.assertEqual(vs.parse_mavlink_frames(buf), [(147, 1, p, 1)])

    def test_two_frames(self):
        a, b = sys_status_payload(), batt_status_payload()
        buf = v1_frame(1, a, 124) + v2_frame(147, b, 154)
        self.assertEqual(vs.parse_mavlink_frames(buf), [(1, 1, a, 1), (147, 1, b, 1)])

    def test_signed_v2_skips_signature(self):
        a, b = sys_status_payload(), batt_status_payload()
        buf = v2_frame(1, a, 124, signed=True) + v2_frame(147, b, 154)
        self.assertEqual(vs.parse_mavlink_frames(buf), [(1, 1, a, 1), (147, 1, b, 1)])

    def test_truncated_buffer(self):
        frame = v2_frame(1, sys_status_payload(), 124)
        self.assertEqual(vs.parse_mavlink_frames(frame[:-3]), [])

    def test_unknown_message_ignored(self):
        self.assertEqual(vs.parse_mavlink_frames(v1_frame(0, b"\0" * 9, 50)), [])

    def test_compid_reported(self):
        p = sys_status_payload()
        self.assertEqual(vs.parse_mavlink_frames(v1_frame(1, p, 124, compid=191)), [(1, 191, p, 1)])


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


class FakeSender:
    def __init__(self, connected=True, fail=False):
        self.connected = connected
        self.fail = fail
        self.sent = []

    def send(self, frame):
        if self.fail:
            self.connected = False
            return
        self.sent.append(frame)


def decode_batt2(frame):
    frames = vs.parse_mavlink_frames(frame)
    assert len(frames) == 1
    msgid, compid, payload, sysid = frames[0]
    return msgid, sysid, compid, payload


class EncodeBatteryTest(unittest.TestCase):
    def test_layout(self):
        f = vs.encode_battery_status(7, 1, 9, 7.6, 1.21, 68)
        self.assertEqual(f[0], 0xFD)
        self.assertEqual(f[1], 36)
        self.assertEqual(f[2:4], b"\0\0")
        self.assertEqual(f[4], 9)
        self.assertEqual(f[5], 7)
        self.assertEqual(f[6], 1)
        self.assertEqual(int.from_bytes(f[7:10], "little"), 147)
        p = f[10:46]
        self.assertEqual(struct.unpack_from("<i", p, 0)[0], -1)
        self.assertEqual(struct.unpack_from("<i", p, 4)[0], -1)
        self.assertEqual(struct.unpack_from("<h", p, 8)[0], 32767)
        volts = struct.unpack_from("<10H", p, 10)
        self.assertEqual(volts, (7600,) + (65535,) * 9)
        self.assertEqual(struct.unpack_from("<h", p, 30)[0], 121)
        self.assertEqual(p[32], 1)
        self.assertEqual(p[33], 0)
        self.assertEqual(p[35], 68)
        self.assertEqual(len(f), 48)

    def test_crc_independent(self):
        f = vs.encode_battery_status(7, 1, 9, 7.6, 1.21, 68)
        crc = ref_x25(f[1:-2] + bytes([154]))
        self.assertEqual(struct.unpack("<H", f[-2:])[0], crc)

    def test_round_trip_through_parser(self):
        f = vs.encode_battery_status(42, 191, 200, 7.6, 1.21, 68)
        msgid, sysid, compid, payload = decode_batt2(f)
        self.assertEqual((msgid, sysid, compid), (147, 42, 191))
        self.assertEqual(vs.decode_battery_status(payload), (1, None))

    def test_unknown_pct(self):
        f = vs.encode_battery_status(1, 1, 0, 7.6, 1.0, None)
        self.assertEqual(struct.unpack_from("<b", f, 10 + 35)[0], -1)

    def test_unknown_volts_and_amps(self):
        f = vs.encode_battery_status(1, 1, 0, None, None, None)
        self.assertEqual(struct.unpack_from("<10H", f, 20), (65535,) * 10)
        self.assertEqual(struct.unpack_from("<h", f, 40)[0], -1)

    def test_clamping(self):
        f = vs.encode_battery_status(1, 1, 0, 9999.0, 9999.0, 150)
        self.assertEqual(struct.unpack_from("<H", f, 20)[0], 65534)
        self.assertEqual(struct.unpack_from("<h", f, 40)[0], 32767)
        self.assertEqual(struct.unpack_from("<b", f, 45)[0], 100)
        f = vs.encode_battery_status(1, 1, 0, -5.0, -9999.0, -3)
        self.assertEqual(struct.unpack_from("<H", f, 20)[0], 0)
        self.assertEqual(struct.unpack_from("<h", f, 40)[0], -32768)
        self.assertEqual(struct.unpack_from("<b", f, 45)[0], 0)


class SysidTest(unittest.TestCase):
    def test_learned_from_compid_1(self):
        st = {}
        vs.apply_mavlink(st, 1, 1, sys_status_payload(), 100.0, 5)
        self.assertEqual(st["sysid"], 5)

    def test_not_learned_from_other_compid(self):
        st = {}
        vs.apply_mavlink(st, 1, 191, sys_status_payload(), 100.0, 5)
        self.assertNotIn("sysid", st)

    def test_parser_reports_sysid(self):
        p = sys_status_payload()
        self.assertEqual(vs.parse_mavlink_frames(v1_frame(1, p, 124, sysid=9)), [(1, 1, p, 9)])
        self.assertEqual(vs.parse_mavlink_frames(v2_frame(1, p, 124, sysid=9)), [(1, 1, p, 9)])


class SendBatt2Test(unittest.TestCase):
    READING = (7.6, 1.21, 68)

    def make(self, enabled=True, compid=1, sysid=3, fresh=True, sender=True, connected=True):
        m = vs.MavlinkBattery(0, enabled=enabled, compid=compid)
        if sysid is not None:
            m.state["sysid"] = sysid
        m.state["ts"] = 100.0 if fresh else 50.0
        m.sender = FakeSender(connected=connected) if sender else None
        return m

    def test_sends_frame(self):
        m = self.make()
        m.send_batt2(self.READING, 102.0)
        (data,) = m.sender.sent
        self.assertEqual(decode_batt2(data)[:3], (147, 3, 1))

    def test_disabled(self):
        m = self.make(enabled=False)
        m.send_batt2(self.READING, 102.0)
        self.assertEqual(m.sender.sent, [])

    def test_no_sysid(self):
        m = self.make(sysid=None)
        m.send_batt2(self.READING, 102.0)
        self.assertEqual(m.sender.sent, [])

    def test_not_connected(self):
        m = self.make(connected=False)
        m.send_batt2(self.READING, 102.0)
        self.assertEqual(m.sender.sent, [])

    def test_no_sender(self):
        self.make(sender=False).send_batt2(self.READING, 102.0)

    def test_stale_fc(self):
        m = self.make(fresh=False)
        m.send_batt2(self.READING, 102.0)
        self.assertEqual(m.sender.sent, [])

    def test_no_reading(self):
        m = self.make()
        m.send_batt2(None, 102.0)
        self.assertEqual(m.sender.sent, [])

    def test_seq_increments_and_wraps(self):
        m = self.make()
        m.seq = 254
        for _ in range(3):
            m.send_batt2(self.READING, 102.0)
        self.assertEqual([d[4] for d in m.sender.sent], [254, 255, 0])

    def test_compid_configurable(self):
        m = self.make(compid=191)
        m.send_batt2(self.READING, 102.0)
        self.assertEqual(decode_batt2(m.sender.sent[0])[2], 191)

    def test_env_defaults_and_overrides(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAVLINK_BATT2", None)
            os.environ.pop("MAVLINK_BATT2_COMPID", None)
            os.environ.pop("MAVLINK_BATT2_TCP", None)
            m = vs.MavlinkBattery(0)
            self.assertEqual((m.enabled, m.compid), (True, 1))
            self.assertEqual((m.sender.host, m.sender.port), ("127.0.0.1", 5760))
        env = {"MAVLINK_BATT2": "0", "MAVLINK_BATT2_COMPID": "191", "MAVLINK_BATT2_TCP": "10.0.0.2:6000"}
        with mock.patch.dict(os.environ, env):
            m = vs.MavlinkBattery(0)
            self.assertEqual((m.enabled, m.compid), (False, 191))
            self.assertEqual((m.sender.host, m.sender.port), ("10.0.0.2", 6000))


class ParseHostPortTest(unittest.TestCase):
    DEFAULT = ("127.0.0.1", 5760)

    def test_valid(self):
        self.assertEqual(vs.parse_host_port("192.168.1.5:5761", self.DEFAULT), ("192.168.1.5", 5761))

    def test_bad_values_fall_back(self):
        for bad in (None, "", "nohost", "host:", ":5760", "host:abc", "host:0", "host:70000"):
            self.assertEqual(vs.parse_host_port(bad, self.DEFAULT), self.DEFAULT, bad)


class Batt2SenderTest(unittest.TestCase):
    def test_send_when_disconnected_is_noop(self):
        s = vs.Batt2Sender(("127.0.0.1", 1))
        self.assertFalse(s.connected)
        s.send(b"x")

    def test_oserror_swallowed_and_marks_disconnected(self):
        class Boom:
            def sendall(self, data):
                raise OSError("down")

            def shutdown(self, how):
                pass

            def close(self):
                pass

        s = vs.Batt2Sender(("127.0.0.1", 1))
        s.sock = Boom()
        self.assertTrue(s.connected)
        s.send(b"x")
        self.assertFalse(s.connected)

    def test_real_socket_drains_and_delivers_frame(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(2)
        s = vs.Batt2Sender(("127.0.0.1", srv.getsockname()[1]))
        s.start()
        conn, _ = srv.accept()
        conn.settimeout(2)
        try:
            conn.sendall(b"\x00junk" * 100)
            deadline = time.monotonic() + 1
            while not s.connected and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(s.connected)
            s.send(vs.encode_battery_status(3, 1, 0, 7.6, 1.21, 68))
            buf = b""
            while len(buf) < 48:
                buf += conn.recv(64)
            self.assertEqual(decode_batt2(buf)[:3], (147, 3, 1))
            conn.sendall(b"more")
            time.sleep(0.05)
            self.assertTrue(s.connected)
        finally:
            conn.close()
            srv.close()


class Ina226ReadingTest(unittest.TestCase):
    def make(self, regs):
        ina = vs.Ina226()
        ina.shunt_ohms, ina.cells, ina.fd = 0.01, 2, 99
        ina._read = lambda reg: regs[reg]
        return ina

    def test_last_matches_line(self):
        ina = self.make({1: 4840, 2: 6080})
        self.assertEqual(ina.line(1.0), "PI 7.6V 1.2A 40%")
        volts, amps, pct = ina.last
        self.assertAlmostEqual(volts, 7.6)
        self.assertAlmostEqual(amps, 1.21)
        self.assertEqual(pct, vs.pack_percent(volts, 2))
        self.assertAlmostEqual(pct, 40)

    def test_last_none_on_error(self):
        ina = self.make({1: 4840, 2: 6080})
        ina.line(1.0)
        ina.fd = None
        ina.next_try = 1e9
        self.assertEqual(ina.line(2.0), "")
        self.assertIsNone(ina.last)


class GpsTest(unittest.TestCase):
    def test_decode(self):
        self.assertEqual(vs.decode_gps_raw(gps_payload()), (3, 12, 121, 501234567, -301234567))

    def test_parser_accepts_truncated_v2(self):
        payload = gps_payload(fix=1, sats=0, eph=9999, lat=0, lon=0)
        frame = v2_frame(24, payload, 24)
        self.assertLess(len(frame), 10 + 30 + 2)
        self.assertEqual(vs.parse_mavlink_frames(frame), [(24, 1, payload, 1)])

    def test_other_component_ignored(self):
        st = {}
        vs.apply_mavlink(st, 24, 191, gps_payload(), 100.0)
        self.assertEqual(vs.gps_line(st, 100.0), "")

    def test_no_fix(self):
        st = {}
        vs.apply_mavlink(st, 24, 1, gps_payload(fix=1, sats=0, eph=9999, lat=0, lon=0), 100.0)
        self.assertEqual(vs.gps_line(st, 101.0), "GPS NO FIX 0 sats HDOP 100.0")

    def test_3d_with_coords(self):
        st = {}
        vs.apply_mavlink(st, 24, 1, gps_payload(), 100.0)
        self.assertEqual(vs.gps_line(st, 101.0), "GPS 3D 12 sats HDOP 1.2\n50.123457 -30.123457")

    def test_unknown_sats_and_hdop(self):
        st = {}
        vs.apply_mavlink(st, 24, 1, gps_payload(fix=6, sats=255, eph=65535), 100.0)
        self.assertEqual(vs.gps_line(st, 100.0), "GPS RTK FIX\n50.123457 -30.123457")

    def test_unknown_fix_name(self):
        st = {}
        vs.apply_mavlink(st, 24, 1, gps_payload(fix=9, sats=5, eph=65535), 100.0)
        self.assertTrue(vs.gps_line(st, 100.0).startswith("GPS FIX9 5 sats\n"))

    def test_stale(self):
        st = {}
        vs.apply_mavlink(st, 24, 1, gps_payload(), 100.0)
        self.assertEqual(vs.gps_line(st, 105.0).split("\n")[0], "GPS 3D 12 sats HDOP 1.2")
        self.assertEqual(vs.gps_line(st, 105.1), "")

    def test_empty(self):
        self.assertEqual(vs.gps_line({}, 100.0), "")

    def test_independent_of_sys_status(self):
        st = {}
        vs.apply_mavlink(st, 24, 1, gps_payload(), 100.0)
        self.assertEqual(vs.fc_line(st, 100.0), "")


class UptimeTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(vs.format_uptime(0), "UP 0:00:00")
        self.assertEqual(vs.format_uptime(247.9), "UP 0:04:07")
        self.assertEqual(vs.format_uptime(13 * 3600), "UP 13:00:00")
        self.assertEqual(vs.format_uptime(2 * 86400 + 3 * 3600 + 4 * 60 + 5), "UP 2d 03:04:05")
        self.assertEqual(vs.format_uptime(86400), "UP 1d 00:00:00")
        self.assertEqual(vs.format_uptime(None), "")

    def test_read(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="12345.67 4567.89\n")):
            self.assertEqual(vs.read_uptime(), 12345.67)

    def test_read_missing(self):
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            self.assertIsNone(vs.read_uptime())


if __name__ == "__main__":
    unittest.main()
