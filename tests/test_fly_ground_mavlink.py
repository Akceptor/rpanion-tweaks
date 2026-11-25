import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from fly_ground_control import Arbiter
from fly_ground_mavlink import MavlinkAdapter, is_planner_heartbeat
from test_fly_ground_control import packet


class Message:
    def __init__(self, kind, sysid, compid, **fields):
        self.kind, self.sysid, self.compid = kind, sysid, compid
        self.__dict__.update(fields)

    def get_type(self):
        return self.kind

    def get_srcSystem(self):
        return self.sysid

    def get_srcComponent(self):
        return self.compid


class FakeMav:
    def __init__(self):
        self.heartbeats = []
        self.overrides = []

    def heartbeat_send(self, *args):
        self.heartbeats.append(args)

    def rc_channels_override_send(self, *args):
        self.overrides.append(args)


class FakeConn:
    def __init__(self, messages=()):
        self.mav = FakeMav()
        self.messages = list(messages)

    def recv_match(self, blocking=False):
        return self.messages.pop(0) if self.messages else None


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(planner_sysid=255, planner_compid=190,
                                        gcs_sysid=255, gcs_compid=191)
        self.a = Arbiter()
        self.a.peer_connected = True
        self.writer = FakeConn()
        self.monitor = FakeConn([Message("HEARTBEAT", 42, 1, type=10, autopilot=3)])
        self.adapter = MavlinkAdapter(self.writer, self.monitor, self.a, self.settings)

    def drive(self):
        self.assertTrue(self.a.accept_packet(packet(), 1000, 0))
        self.assertTrue(self.a.resume(1000))

    def test_planner_filter_ignores_own_and_fc_heartbeat(self):
        self.assertTrue(is_planner_heartbeat(Message("HEARTBEAT", 255, 190, type=6), self.settings))
        self.assertFalse(is_planner_heartbeat(Message("HEARTBEAT", 255, 191, type=6), self.settings))
        self.assertFalse(is_planner_heartbeat(Message("HEARTBEAT", 42, 1, type=10), self.settings))

    def test_targets_discovered_fc_and_sends_only_when_browser_owns(self):
        self.adapter.poll(1000)
        self.assertEqual(self.adapter.target, (42, 1))
        self.adapter.send(1000)
        self.assertEqual(len(self.writer.mav.heartbeats), 1)
        self.assertEqual(self.writer.mav.overrides, [])
        self.drive()
        self.adapter.send(1000)
        self.assertEqual(len(self.writer.mav.heartbeats), 1)
        self.assertEqual(self.writer.mav.overrides[0][:2], (42, 1))
        self.assertEqual(len(self.writer.mav.overrides[0]), 20)

    def test_browser_telemetry_comes_only_from_lima_writer_not_raw_monitor(self):
        received = []
        self.adapter.on_telemetry = received.append
        raw = Message("GLOBAL_POSITION_INT", 42, 1, lat=470000000, lon=80000000)
        masked = Message("GLOBAL_POSITION_INT", 42, 1, lat=-120700000, lon=-770435000)
        self.monitor.messages.append(raw)
        self.writer.messages.append(masked)
        self.adapter.poll(1000)
        self.assertEqual(received, [masked])

    def test_idle_connected_browser_registers_lima_before_target_discovery(self):
        self.adapter.send(1000)
        self.assertEqual(len(self.writer.mav.heartbeats), 1)
        self.assertEqual(self.writer.mav.overrides, [])
        self.adapter.send(1500)
        self.assertEqual(len(self.writer.mav.heartbeats), 1)
        self.adapter.send(2000)
        self.assertEqual(len(self.writer.mav.heartbeats), 2)
        self.a.peer_connected = False
        self.adapter.send(3000)
        self.assertEqual(len(self.writer.mav.heartbeats), 2)

    def test_planner_heartbeat_releases_override_before_any_browser_send(self):
        self.drive()
        self.adapter.poll(1000)
        self.adapter.send(1000)
        self.monitor.messages.append(Message("HEARTBEAT", 255, 190, type=6))
        self.adapter.poll(1001)
        self.adapter.send(1001)
        self.assertEqual(self.a.owner, "mission-planner")
        self.assertEqual(len(self.writer.mav.overrides), 2)
        self.assertEqual(self.writer.mav.overrides[-1][2], 0)
        self.assertEqual(self.writer.mav.overrides[-1][4], 0)
        self.assertEqual(len(self.writer.mav.heartbeats), 1)
        self.adapter.send(1020)
        self.assertEqual(len(self.writer.mav.overrides), 2)

    def test_writer_reconnect_resets_browser_ownership(self):
        self.drive()
        self.adapter.writer_reconnected()
        self.assertEqual(self.a.owner, "idle")
        self.assertIsNone(self.adapter.target)
        self.adapter.send(1000)
        self.assertEqual(self.writer.mav.overrides, [])


if __name__ == "__main__":
    unittest.main()
