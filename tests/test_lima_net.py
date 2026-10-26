import math
import os
import contextlib
import io
import socket
import tempfile
import threading
import unittest
from unittest import mock

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from lima_frames import decode, frame
from lima_loader import load_lima

lima = load_lima()

REAL = (47.0, 8.0)
FAKE = (-12.07, -77.0435)
M_DEG = lima.R_EARTH * math.pi / 180


def udp(host="127.0.0.1", port=0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((host, port))
    s.settimeout(3)
    return s


def heartbeat():
    return frame(mavlink.MAVLink_heartbeat_message(
        mavlink.MAV_TYPE_GROUND_ROVER, mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, 0, 3))


def gps_fix():
    return frame(mavlink.MAVLink_gps_raw_int_message(
        0, 3, round(REAL[0] * 1e7), round(REAL[1] * 1e7), 500000, 100, 100, 0, 0, 10))


def param_request():
    return frame(mavlink.MAVLink_param_request_list_message(1, 1))


class NetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socks = []
        self.router = self.sock()
        probe = self.sock()
        self.listen = probe.getsockname()
        probe.close()
        self.gcs = self.sock()
        store = lima.AnchorStore(os.path.join(self.tmp.name, "anchor.json"))
        self.rewriter = lima.Rewriter(store, FAKE, 5000.0)
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=lima.run,
            args=(self.listen, self.gcs.getsockname(), self.rewriter, self.stop))
        self.thread.start()

    def tearDown(self):
        self.stop.set()
        self.thread.join(5)
        for s in self.socks:
            s.close()
        self.tmp.cleanup()
        self.assertFalse(self.thread.is_alive())

    def sock(self, *args):
        s = udp(*args)
        self.socks.append(s)
        return s

    def router_send(self, sock, data):
        # the proxy may not have bound yet; UDP to an unbound port is silently lost
        sock.settimeout(0.2)
        for _ in range(25):
            sock.sendto(data, self.listen)
            try:
                return self.gcs_recv()
            except socket.timeout:
                pass
        self.fail("proxy never forwarded")

    def gcs_recv(self):
        data, addr = self.gcs.recvfrom(4096)
        self.proxy_addr = addr
        return data

    def test_end_to_end(self):
        self.gcs.settimeout(0.2)
        data = self.router_send(self.router, gps_fix())
        got = decode(data)[0]
        self.assertEqual(got.get_type(), "GPS_RAW_INT")
        self.assertAlmostEqual(got.lat / 1e7, FAKE[0], places=5)
        self.assertAlmostEqual(got.lon / 1e7, FAKE[1], places=5)

        item = mavlink.MAVLink_mission_item_int_message(
            1, 1, 1, mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            mavlink.MAV_CMD_NAV_WAYPOINT, 0, 1, 0, 0, 0, 0,
            round(FAKE[0] * 1e7), round(FAKE[1] * 1e7), 10.0)
        self.gcs.sendto(frame(item), self.proxy_addr)
        self.router.settimeout(3)
        data, _ = self.router.recvfrom(4096)
        out = decode(data)[0]
        self.assertEqual(out.get_type(), "MISSION_ITEM_INT")
        self.assertAlmostEqual(out.x / 1e7, REAL[0], places=5)
        self.assertAlmostEqual(out.y / 1e7, REAL[1], places=5)

    def test_router_port_change(self):
        self.gcs.settimeout(0.2)
        self.router_send(self.router, heartbeat())
        router2 = self.sock()
        self.router_send(router2, heartbeat())
        self.gcs.sendto(param_request(), self.proxy_addr)
        router2.settimeout(3)
        data, _ = router2.recvfrom(4096)
        self.assertEqual(decode(data)[0].get_type(), "PARAM_REQUEST_LIST")
        self.router.settimeout(0.3)
        with self.assertRaises(socket.timeout):
            self.router.recvfrom(4096)

    def test_gcs_foreign_ip_ignored(self):
        self.gcs.settimeout(0.2)
        self.router_send(self.router, heartbeat())
        stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socks.append(stranger)
        try:
            stranger.bind(("127.0.0.2", 0))
        except OSError:
            self.skipTest("cannot bind 127.0.0.2")
        stranger.sendto(param_request(), self.proxy_addr)
        self.router.settimeout(0.5)
        with self.assertRaises(socket.timeout):
            self.router.recvfrom(4096)
        self.gcs.sendto(param_request(), self.proxy_addr)
        self.router.settimeout(3)
        data, _ = self.router.recvfrom(4096)
        self.assertEqual(decode(data)[0].get_type(), "PARAM_REQUEST_LIST")

    def test_gcs_unreachable_does_not_kill_loop(self):
        self.gcs.close()
        self.router.sendto(heartbeat(), self.listen)
        self.router.sendto(heartbeat(), self.listen)
        self.stop.wait(1)
        self.assertTrue(self.thread.is_alive())


class MainTest(unittest.TestCase):
    def test_main_requires_gcs(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, clear=True), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                lima.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("LIMA_GCS", err.getvalue())

    def test_main_rejects_bad_settings(self):
        for key, val in (("LIMA_FAKE_ANCHOR", "x"), ("LIMA_LISTEN", "x"),
                         ("LIMA_MAX_TARGET_KM", "x"), ("LIMA_GCS", "nope")):
            env = {"LIMA_GCS": "127.0.0.1:14550", key: val}
            with mock.patch.dict(os.environ, env, clear=True), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    lima.main()
            self.assertEqual(cm.exception.code, 2, key)

    def test_parsers(self):
        self.assertEqual(lima.parse_hostport("10.0.0.5:14550"), ("10.0.0.5", 14550))
        self.assertEqual(lima.parse_latlon("-12.07,-77.0435"), (-12.07, -77.0435))
        for bad in ("", "10.0.0.5", "10.0.0.5:x", ":14550", "10.0.0.5:70000"):
            with self.assertRaises(ValueError):
                lima.parse_hostport(bad)
        for bad in ("", "1", "a,b", "91,0", "0,181", "nan,0", "1,2,3"):
            with self.assertRaises(ValueError):
                lima.parse_latlon(bad)


if __name__ == "__main__":
    unittest.main()
