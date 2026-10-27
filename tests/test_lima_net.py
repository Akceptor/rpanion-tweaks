import math
import os
import contextlib
import io
import socket
import tempfile
import threading
import time
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


def gcs_heartbeat():
    return frame(mavlink.MAVLink_heartbeat_message(
        mavlink.MAV_TYPE_GCS, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0, 3), sysid=255, compid=190)


class NetTest(unittest.TestCase):
    allow = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socks = []
        self.now = [100.0]
        self.router = self.sock()
        probe = self.sock()
        self.listen = probe.getsockname()
        probe.close()
        probe = self.sock()
        self.serve = probe.getsockname()
        probe.close()
        self.gcs = self.sock()
        store = lima.AnchorStore(os.path.join(self.tmp.name, "anchor.json"))
        self.rewriter = lima.Rewriter(store, FAKE, 5000.0)
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=lima.run,
            args=(self.listen, self.serve, self.rewriter, self.stop),
            kwargs={"allow": self.allow, "clock": lambda: self.now[0]})
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

    def connect(self, gcs):
        # the proxy may not have bound yet; UDP to an unbound port is silently lost
        gcs.settimeout(0.2)
        for _ in range(25):
            gcs.sendto(gcs_heartbeat(), self.serve)
            time.sleep(0.05)
            self.router.sendto(heartbeat(), self.listen)
            try:
                gcs.recvfrom(4096)
                break
            except socket.timeout:
                pass
        else:
            self.fail("proxy never replied")
        self.drain(gcs)

    def drain(self, sock):
        sock.settimeout(0.2)
        try:
            while True:
                sock.recvfrom(4096)
        except socket.timeout:
            pass

    def assert_silent(self, sock, wait=0.4):
        sock.settimeout(wait)
        with self.assertRaises(socket.timeout):
            sock.recvfrom(4096)

    def router_pump(self):
        for _ in range(3):
            self.router.sendto(heartbeat(), self.listen)
            time.sleep(0.05)

    def test_end_to_end(self):
        self.connect(self.gcs)
        self.router.sendto(gps_fix(), self.listen)
        self.gcs.settimeout(3)
        while True:
            data, _ = self.gcs.recvfrom(4096)
            msgs = decode(data)
            if msgs[0].get_type() == "GPS_RAW_INT":
                break
        got = msgs[0]
        self.assertAlmostEqual(got.lat / 1e7, FAKE[0], places=5)
        self.assertAlmostEqual(got.lon / 1e7, FAKE[1], places=5)

        item = mavlink.MAVLink_mission_item_int_message(
            1, 1, 1, mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            mavlink.MAV_CMD_NAV_WAYPOINT, 0, 1, 0, 0, 0, 0,
            round(FAKE[0] * 1e7), round(FAKE[1] * 1e7), 10.0)
        self.gcs.sendto(frame(item), self.serve)
        self.router.settimeout(3)
        while True:
            data, _ = self.router.recvfrom(4096)
            out = decode(data)[0]
            if out.get_type() == "MISSION_ITEM_INT":
                break
        self.assertAlmostEqual(out.x / 1e7, REAL[0], places=5)
        self.assertAlmostEqual(out.y / 1e7, REAL[1], places=5)

    def test_nothing_sent_before_gcs_packet(self):
        self.router_pump()
        self.assert_silent(self.gcs)

    def test_garbage_does_not_become_peer(self):
        self.router_pump()
        self.gcs.sendto(b"not mavlink at all", self.serve)
        time.sleep(0.1)
        self.router_pump()
        self.assert_silent(self.gcs)

    def test_reply_goes_to_sender(self):
        other = self.sock()
        self.connect(other)
        self.router_pump()
        other.settimeout(3)
        other.recvfrom(4096)
        self.assert_silent(self.gcs)

    def test_new_peer_replaces_old(self):
        other = self.sock()
        self.connect(self.gcs)
        self.connect(other)
        self.drain(self.gcs)
        self.router_pump()
        other.settimeout(3)
        other.recvfrom(4096)
        self.assert_silent(self.gcs)

    def test_peer_expires(self):
        self.connect(self.gcs)
        self.now[0] += 5.5
        self.router_pump()
        self.assert_silent(self.gcs)
        self.gcs.sendto(gcs_heartbeat(), self.serve)
        time.sleep(0.1)
        self.router_pump()
        self.gcs.settimeout(3)
        self.gcs.recvfrom(4096)

    def test_peer_kept_alive_by_traffic(self):
        self.connect(self.gcs)
        for _ in range(3):
            self.now[0] += 3
            self.gcs.sendto(gcs_heartbeat(), self.serve)
            time.sleep(0.1)
        self.router_pump()
        self.gcs.settimeout(3)
        self.gcs.recvfrom(4096)

    def test_uplink_reaches_router(self):
        self.connect(self.gcs)
        self.gcs.sendto(param_request(), self.serve)
        self.router.settimeout(3)
        while True:
            data, _ = self.router.recvfrom(4096)
            if decode(data)[0].get_type() == "PARAM_REQUEST_LIST":
                break

    def test_router_port_change(self):
        self.connect(self.gcs)
        router2 = self.sock()
        router2.sendto(heartbeat(), self.listen)
        time.sleep(0.2)
        self.gcs.sendto(param_request(), self.serve)
        router2.settimeout(3)
        while True:
            data, _ = router2.recvfrom(4096)
            if decode(data)[0].get_type() == "PARAM_REQUEST_LIST":
                break
        self.drain(self.router)
        self.gcs.sendto(param_request(), self.serve)
        time.sleep(0.2)
        self.assert_silent(self.router, 0.3)

    def test_gcs_unreachable_does_not_kill_loop(self):
        self.connect(self.gcs)
        self.gcs.close()
        self.router.sendto(heartbeat(), self.listen)
        self.router.sendto(heartbeat(), self.listen)
        self.stop.wait(1)
        self.assertTrue(self.thread.is_alive())


class AllowedNetTest(NetTest):
    allow = {"127.0.0.1"}


class DisallowedNetTest(unittest.TestCase):
    def test_disallowed_ip_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            router, probe, gcs = udp(), udp(), udp()
            listen = probe.getsockname()
            probe.close()
            probe = udp()
            serve = probe.getsockname()
            probe.close()
            rewriter = lima.Rewriter(lima.AnchorStore(os.path.join(tmp, "a.json")), FAKE, 5000.0)
            stop = threading.Event()
            thread = threading.Thread(
                target=lima.run, args=(listen, serve, rewriter, stop),
                kwargs={"allow": {"10.9.9.9"}})
            thread.start()
            try:
                for _ in range(5):
                    gcs.sendto(gcs_heartbeat(), serve)
                    gcs.sendto(param_request(), serve)
                    router.sendto(heartbeat(), listen)
                    time.sleep(0.1)
                for s in (gcs, router):
                    s.settimeout(0.4)
                    with self.assertRaises(socket.timeout):
                        s.recvfrom(4096)
            finally:
                stop.set()
                thread.join(5)
                for s in (router, gcs):
                    s.close()


class MainTest(unittest.TestCase):
    def test_main_does_not_require_gcs(self):
        with mock.patch.dict(os.environ, clear=True), \
                mock.patch.object(lima, "run") as run:
            lima.main()
        serve, allow = run.call_args.args[1], run.call_args.kwargs["allow"]
        self.assertEqual(serve, ("0.0.0.0", 14551))
        self.assertIsNone(allow)

    def test_main_reads_serve_and_allow(self):
        env = {"LIMA_SERVE": "10.0.0.1:15000", "LIMA_GCS_ALLOW": "10.46.93.5, 10.46.93.6"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(lima, "run") as run:
            lima.main()
        self.assertEqual(run.call_args.args[1], ("10.0.0.1", 15000))
        self.assertEqual(run.call_args.kwargs["allow"], {"10.46.93.5", "10.46.93.6"})

    def test_main_rejects_bad_settings(self):
        for key, val in (("LIMA_FAKE_ANCHOR", "x"), ("LIMA_LISTEN", "x"),
                         ("LIMA_MAX_TARGET_KM", "x"), ("LIMA_SERVE", "nope"),
                         ("LIMA_GCS_ALLOW", ""), ("LIMA_GCS_ALLOW", "1.2.3"),
                         ("LIMA_GCS_ALLOW", "1.2.3.4,x"), ("LIMA_GCS_ALLOW", "::1")):
            with mock.patch.dict(os.environ, {key: val}, clear=True), \
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
