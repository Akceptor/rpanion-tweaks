import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from fly_ground_control import Arbiter
from fly_ground_peer import PeerProtocol, require_webrtc_plugins, signal_url, turn_uris
from test_fly_ground_control import packet


class SignalTest(unittest.TestCase):
    def test_missing_nice_plugin_has_install_hint(self):
        gst = SimpleNamespace(ElementFactory=SimpleNamespace(find=lambda name: object() if name == "nicesrc" else None))
        with self.assertRaisesRegex(RuntimeError, "gstreamer1.0-nice"):
            require_webrtc_plugins(gst)
        gst.ElementFactory.find = lambda name: object()
        require_webrtc_plugins(gst)

    def test_host_url_uses_hashed_room_only(self):
        s = SimpleNamespace(site_url="https://site.example", room_id="a" * 64, code="SECRET")
        self.assertEqual(signal_url(s), "wss://site.example/room/" + "a" * 64 + "?role=host")

    def test_turn_urls_include_credentials(self):
        servers = [{"urls": ["stun:stun.cloudflare.com:3478"]},
                   {"urls": ["turn:turn.cloudflare.com:3478?transport=udp",
                             "turns:turn.cloudflare.com:443?transport=tcp"],
                    "username": "user", "credential": "pass"}]
        self.assertEqual(turn_uris(servers), ["turn://user:pass@turn.cloudflare.com:3478",
                                                   "turns://user:pass@turn.cloudflare.com:443"])


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.arbiter = Arbiter()
        self.sent = []
        self.p = PeerProtocol(self.arbiter, self.sent.append, session_seconds=3600)
        self.p.open(1000)

    def test_open_autoaccepts_but_does_not_drive(self):
        self.assertEqual(self.arbiter.owner, "idle")
        self.assertEqual(self.sent[-1]["source"], "pi")
        self.assertEqual(self.sent[-1]["phase"], "accepted")
        self.assertEqual(self.sent[-1]["owner"], "idle")

    def test_resume_needs_fresh_neutral_control(self):
        self.assertFalse(self.p.message({"t": "resume"}, 1000))
        self.p.ping(1000)
        self.assertTrue(self.p.message({"t": "pong", "id": 1, "t0": 1000, "tp": 1000}, 1000))
        self.assertTrue(self.p.control(packet(), 1000))
        self.assertTrue(self.p.message({"t": "resume"}, 1000))
        self.assertEqual(self.sent[-1]["owner"], "browser")
        self.arbiter.planner_seen(1001)
        self.p.tick(1001)
        self.assertEqual(self.sent[-1]["owner"], "mission-planner")
        self.assertFalse(self.p.message({"t": "resume"}, 1002))

    def test_clock_pong_computes_pilot_minus_pi_offset(self):
        self.p.ping(1000)
        ping = self.sent[-1]
        self.assertEqual(ping["t"], "ping")
        self.assertTrue(self.p.message({"t": "pong", "id": ping["id"], "t0": 1000, "tp": 1150}, 1100))
        self.assertAlmostEqual(self.p.offset_ms, 100)
        self.assertTrue(self.p.control(packet(ts=1150), 1050))

    def test_unknown_commands_and_session_expiry(self):
        self.assertFalse(self.p.message({"t": "arm"}, 1000))
        self.p.tick(3_601_001)
        self.assertEqual(self.sent[-1]["phase"], "ended")
        self.assertEqual(self.arbiter.owner, "idle")

    def test_close_before_channel_opens_has_no_session_state(self):
        arbiter = Arbiter()
        sent = []
        protocol = PeerProtocol(arbiter, sent.append)
        protocol.tick(999)
        self.assertEqual(sent, [])
        protocol.close(1000)
        self.assertTrue(protocol.ended)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
