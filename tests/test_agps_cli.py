import contextlib
import io
import logging
import threading
import unittest
from unittest import mock

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

    def test_bad_map_tcp_exits_2(self):
        rc, _, err, connects = run_main([], {"AGPS_MAP_TCP": "nope"})
        self.assertEqual(rc, 2)
        self.assertIn("AGPS_MAP_TCP", err)
        self.assertEqual(connects, [])

    def run_with_fake_sender(self, env):
        built, runners = [], []

        class FakeSender:
            def __init__(self, host, port):
                built.append((host, port, self))
                self.started = False

            def start(self):
                self.started = True

            def send(self, frame):
                pass

        stop = threading.Event()
        conn = QueueConn([], on_empty=stop.set)
        orig_runner = agps.Runner

        def runner_factory(*a, **k):
            r = orig_runner(*a, **k)
            runners.append(r)
            return r

        with mock.patch.object(agps, "TcpFrameSender", FakeSender), \
                mock.patch.object(agps, "Runner", runner_factory), \
                mock.patch.object(agps.threading, "Event", lambda: stop):
            rc, _, _, connects = run_main([], dict(env, AGPS_CACHE="0"), conn)
        self.assertEqual(rc, 0)
        return built, runners, connects

    def test_run_builds_icon_on_separate_connection(self):
        built, runners, connects = self.run_with_fake_sender({})
        (host, port, sender), = built
        self.assertEqual((host, port), ("127.0.0.1", 5760))
        self.assertTrue(sender.started)
        self.assertIsNotNone(runners[0].icon)
        (args, kwargs), = connects
        self.assertEqual(kwargs["source_component"], 193)

    def test_map_icon_off_builds_nothing(self):
        built, runners, connects = self.run_with_fake_sender({"AGPS_MAP_ICON": "0"})
        self.assertEqual(built, [])
        self.assertIsNone(runners[0].icon)
        self.assertEqual(len(connects), 1)

    def test_serve_reconnects_after_reset(self):
        stop = threading.Event()
        events = []

        class Dead:
            def recv_match(self, **k):
                raise ConnectionResetError(104, "Connection reset by peer")

        new = QueueConn([gps(3)], on_empty=stop.set)

        class R:
            conn = None

            def handle(self, m):
                events.append(m.get_type())

            def tick(self):
                pass

            def link_lost(self):
                events.append("lost")

        r = R()
        waits = []
        with self.assertLogs(level=logging.WARNING) as cm:
            agps.serve(Dead(), r, stop, reconnect=lambda: new, wait=waits.append)
        self.assertEqual(events, ["lost", "GPS_RAW_INT"])
        self.assertIs(r.conn, new)
        self.assertEqual(len(waits), 1)
        self.assertEqual(len(cm.output), 1)
        self.assertNotIn("Traceback", cm.output[0])

    def test_serve_backs_off_while_reconnect_fails(self):
        stop = threading.Event()

        class Dead:
            def recv_match(self, **k):
                raise BrokenPipeError()

        class R:
            conn = None

            def handle(self, m):
                pass

            def tick(self):
                pass

            def link_lost(self):
                pass

        attempts = []
        new = QueueConn([], on_empty=stop.set)

        def reconnect():
            attempts.append(1)
            if len(attempts) < 4:
                raise ConnectionRefusedError()
            return new

        waits = []
        with self.assertLogs(level=logging.WARNING):
            agps.serve(Dead(), R(), stop, reconnect=reconnect, wait=waits.append)
        self.assertEqual(len(attempts), 4)
        self.assertEqual(waits, sorted(waits))
        self.assertLessEqual(max(waits), 10)

    def test_serve_tick_error_also_reconnects(self):
        stop = threading.Event()
        new = QueueConn([], on_empty=stop.set)
        lost = []

        class R:
            conn = None

            def handle(self, m):
                pass

            def tick(self):
                if not lost:
                    raise BrokenPipeError()

            def link_lost(self):
                lost.append(1)

        with self.assertLogs(level=logging.WARNING):
            agps.serve(QueueConn([heartbeat()]), R(), stop, reconnect=lambda: new, wait=lambda s: None)
        self.assertEqual(lost, [1])

    def test_serve_without_reconnect_propagates(self):
        class Dead:
            def recv_match(self, **k):
                raise ConnectionResetError()

        with self.assertRaises(ConnectionResetError):
            agps.serve(Dead(), object(), threading.Event())


if __name__ == "__main__":
    unittest.main()
