import contextlib
import io
import threading
import unittest

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from agps_loader import load_agps
from test_agps_runner import FakeMav, gps, heartbeat, msg

agps = load_agps()


def statustext(text, src=(1, 1)):
    return msg(mavlink.MAVLink_statustext_message, src, severity=6, text=text.encode())


class QueueConn:
    def __init__(self, queue, on_empty=None):
        self.mav = FakeMav()
        self.queue = list(queue)
        self.on_empty = on_empty

    def recv_match(self, type=None, blocking=False, timeout=None):
        if self.queue:
            return self.queue.pop(0)
        if self.on_empty:
            self.on_empty()
        return None


def run_main(argv, env, conn=None):
    connects = []

    def connect(*a, **k):
        connects.append((a, k))
        return conn

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = agps.main(argv, env=env, connect=connect)
    return rc, out.getvalue(), err.getvalue(), connects


class CliTest(unittest.TestCase):
    def test_disabled_exits_0(self):
        rc, _, _, connects = run_main([], {"AGPS_ENABLE": "0"})
        self.assertEqual(rc, 0)
        self.assertEqual(connects, [])

    def test_bad_setting_exits_2(self):
        rc, _, err, connects = run_main([], {"AGPS_SETTLE_S": "x"})
        self.assertEqual(rc, 2)
        self.assertIn("AGPS_SETTLE_S", err)
        self.assertEqual(connects, [])

    def test_bad_status_exits_2(self):
        rc, _, err, connects = run_main([], {"AGPS_STATUS": "nope"})
        self.assertEqual(rc, 2)
        self.assertIn("AGPS_STATUS", err)
        self.assertEqual(connects, [])

    def test_connect_arguments(self):
        rc, _, _, connects = run_main(["probe"], {}, QueueConn([heartbeat(), statustext("u-blox 1 HW: 000A0000")]))
        (args, kwargs), = connects
        self.assertEqual(args, ("tcp:127.0.0.1:5760",))
        self.assertEqual(kwargs, {"source_system": 1, "source_component": 193, "autoreconnect": True})

    def test_probe_sends_mon_ver(self):
        conn = QueueConn([heartbeat(), statustext("noise", src=(255, 190)), statustext("u-blox 1 HW: 000A0000 SW: ROM SPG 5.10")])
        rc, out, _, _ = run_main(["probe"], {}, conn)
        self.assertEqual(rc, 0)
        self.assertEqual([c[3] for c in conn.mav.calls], [agps.mon_ver_poll()])
        self.assertIn("HW: 000A0000", out)
        self.assertNotIn("noise", out)

    def test_reset_refuses_when_armed(self):
        conn = QueueConn([heartbeat(armed=True)])
        rc, _, _, _ = run_main(["reset", "warm"], {}, conn)
        self.assertEqual(rc, 3)
        self.assertEqual(conn.mav.calls, [])

    def test_reset_warm_sends_cfg_rst(self):
        conn = QueueConn([heartbeat()])
        rc, _, _, _ = run_main(["reset", "warm"], {}, conn)
        self.assertEqual(rc, 0)
        self.assertEqual([c[3] for c in conn.mav.calls], [agps.cfg_rst(0x0001)])

    def test_reset_unknown_mode_exits_2(self):
        rc, _, _, connects = run_main(["reset", "lukewarm"], {})
        self.assertEqual(rc, 2)
        self.assertEqual(connects, [])

    def test_inject_sends_bundle(self):
        conn = QueueConn([heartbeat()])
        rc, _, _, _ = run_main(["inject"], {"AGPS_APPROX_LAT": "47.0", "AGPS_APPROX_LON": "8.0",
                                              "AGPS_CACHE": "0"}, conn)
        self.assertEqual(rc, 0)
        self.assertEqual(conn.mav.calls[0][3], agps.valset_use_ana())
        self.assertEqual(conn.mav.calls[-1][3][2:4].hex(), "1340")

    def test_serve_loop_drives_runner(self):
        stop = threading.Event()
        conn = QueueConn([heartbeat(), gps(3)], on_empty=stop.set)
        calls = []

        class R:
            def handle(self, m):
                calls.append(m.get_type())

            def tick(self):
                calls.append("tick")

        agps.serve(conn, R(), stop)
        self.assertEqual(calls, ["HEARTBEAT", "tick", "GPS_RAW_INT", "tick", "tick"])


if __name__ == "__main__":
    unittest.main()
