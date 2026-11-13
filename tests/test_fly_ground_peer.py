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

    def test_late_pong_for_earlier_ping_still_sets_offset(self):
        self.p.ping(1000)
        self.p.ping(1500)
        self.p.ping(2000)
        self.assertTrue(self.p.message({"t": "pong", "id": 1, "t0": 1000, "tp": 1400}, 1800))
        self.assertAlmostEqual(self.p.offset_ms, 1400 - (1000 + 1800) / 2)

    def test_offset_uses_lowest_rtt_sample_and_rejects_forged_pongs(self):
        self.p.ping(1000)
        self.p.ping(1500)
        self.assertTrue(self.p.message({"t": "pong", "id": 1, "t0": 1000, "tp": 1900}, 1700))
        self.assertTrue(self.p.message({"t": "pong", "id": 2, "t0": 1500, "tp": 1600}, 1520))
        self.assertAlmostEqual(self.p.offset_ms, 1600 - (1500 + 1520) / 2)
        self.assertFalse(self.p.message({"t": "pong", "id": 2, "t0": 1500, "tp": 1}, 1530))
        self.assertFalse(self.p.message({"t": "pong", "id": 9, "t0": 1500, "tp": 1}, 1530))
        self.p.ping(2000)
        self.assertFalse(self.p.message({"t": "pong", "id": 3, "t0": 1999, "tp": 1}, 2010))

    def test_unknown_commands_and_session_expiry(self):
        self.assertFalse(self.p.message({"t": "arm"}, 1000))
        self.p.tick(3_601_001)
        self.assertEqual(self.sent[-1]["phase"], "ended")
        self.assertEqual(self.arbiter.owner, "idle")

    def test_silent_peer_times_out_at_five_seconds_even_with_unlimited_session(self):
        self.p.session_seconds = 0
        self.p.tick(5999)
        self.assertFalse(self.p.ended)
        self.p.tick(6000)
        self.assertTrue(self.p.ended)
        self.assertFalse(self.arbiter.peer_connected)
        self.assertEqual(self.sent[-1]["reason"], "heartbeat timeout")

    def test_valid_pong_extends_idle_session_until_five_seconds_after_reply(self):
        self.p.ping(5000)
        self.assertTrue(self.p.message({"t": "pong", "id": 1, "t0": 5000, "tp": 5500}, 5500))
        self.p.tick(10499)
        self.assertFalse(self.p.ended)
        self.p.tick(10500)
        self.assertTrue(self.p.ended)

    def test_invalid_and_replayed_pongs_do_not_extend_session(self):
        self.p.ping(1000)
        pong = {"t": "pong", "id": 1, "t0": 1000, "tp": 1100}
        self.assertTrue(self.p.message(pong, 1100))
        self.assertFalse(self.p.message(pong, 5900))
        self.assertFalse(self.p.message({**pong, "id": 999}, 5900))
        self.p.tick(6100)
        self.assertTrue(self.p.ended)

    def test_control_traffic_does_not_replace_session_heartbeat(self):
        self.p.ping(1000)
        self.p.message({"t": "pong", "id": 1, "t0": 1000, "tp": 1000}, 1000)
        self.p.control(packet(), 1000)
        self.assertTrue(self.p.message({"t": "resume"}, 1000))
        self.assertEqual(self.arbiter.owner, "browser")
        self.p.control(packet(ts=5999), 5999)
        self.p.tick(6000)
        self.assertTrue(self.p.ended)
        self.assertEqual(self.arbiter.owner, "idle")
        self.assertFalse(self.p.message({"t": "resume"}, 6001))

    def test_malformed_clock_timestamps_cannot_renew_heartbeat(self):
        for tp in (False, True, float("nan"), float("inf"), -float("inf"), None, "5500"):
            with self.subTest(tp=tp):
                protocol = PeerProtocol(Arbiter(), lambda value: None)
                protocol.open(1000)
                protocol.ping(5000)
                self.assertFalse(protocol.message({"t": "pong", "id": 1, "t0": 5000, "tp": tp}, 5500))
                protocol.tick(6000)
                self.assertTrue(protocol.ended)

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


class StallTest(unittest.TestCase):
    def test_plugin_warnings_never_log_turn_credentials_or_raw_debug(self):
        from fly_ground_peer import GstPeer
        from unittest.mock import Mock
        secret = 'turn://relay-user:relay-password@turn.cloudflare.com:3478'
        warning = SimpleNamespace(message=secret, domain=secret, code=5)
        msg = SimpleNamespace(type=4, src=SimpleNamespace(get_name=lambda: secret),
                              parse_warning=lambda: (warning, 'debug ' + secret))
        gst = SimpleNamespace(MessageType=SimpleNamespace(ERROR=1, EOS=2, WARNING=4, STATE_CHANGED=8))
        peer = SimpleNamespace(Gst=gst, _maybe_offer=lambda: None, log_counts=lambda now: None, check_stall=lambda now: None,
                               now=lambda: 0, bus=SimpleNamespace(timed_pop_filtered=Mock(side_effect=[msg, None])))
        with self.assertLogs('fly-ground', level='WARNING') as logs:
            GstPeer.poll_bus(peer)
        text = ' '.join(logs.output)
        self.assertNotIn('relay-user', text)
        self.assertNotIn('relay-password', text)
        self.assertNotIn('debug', text)
        self.assertIn('code 5', text)

    def test_plugin_error_exception_exposes_only_safe_category_and_numeric_code(self):
        from fly_ground_peer import GstPeer
        from unittest.mock import Mock
        secret = 'turn://relay-user:relay-password@turn.cloudflare.com:3478'
        error = SimpleNamespace(message=secret, domain='gst-resource-error-quark', code=5)
        msg = SimpleNamespace(type=1, parse_error=lambda: (error, secret))
        gst = SimpleNamespace(MessageType=SimpleNamespace(ERROR=1, EOS=2, WARNING=4, STATE_CHANGED=8))
        peer = SimpleNamespace(Gst=gst, _maybe_offer=lambda: None, log_counts=lambda now: None, check_stall=lambda now: None,
                               now=lambda: 0, bus=SimpleNamespace(timed_pop_filtered=Mock(return_value=msg)))
        with self.assertRaises(RuntimeError) as cm:
            GstPeer.poll_bus(peer)
        self.assertNotIn('relay-user', str(cm.exception))
        self.assertNotIn('relay-password', str(cm.exception))
        self.assertIn('resource', str(cm.exception))
        self.assertIn('code 5', str(cm.exception))

    def test_buffer_probe_counts_lists_and_corruption_flags(self):
        from fly_ground_peer import GstPeer
        from unittest.mock import Mock
        gst = SimpleNamespace(PadProbeType=SimpleNamespace(BUFFER=1, BUFFER_LIST=2),
            BufferFlags=SimpleNamespace(CORRUPTED=1, DISCONT=2), PadProbeReturn=SimpleNamespace(OK=0))
        peer = SimpleNamespace(counts={}, Gst=gst)
        pad = Mock()
        GstPeer._count(peer, pad, "pay")
        mask, probe = pad.add_probe.call_args.args
        self.assertEqual(mask, 3)
        buffers = [SimpleNamespace(has_flags=lambda flag: flag == 1),
                   SimpleNamespace(has_flags=lambda flag: flag == 2)]
        batch = SimpleNamespace(length=lambda: 2, get=lambda i: buffers[i])
        probe(pad, SimpleNamespace(type=2, get_buffer_list=lambda: batch))
        probe(pad, SimpleNamespace(type=1, get_buffer=lambda: buffers[0]))
        self.assertEqual(peer.counts, {"pay": 3, "pay-corrupted": 2, "pay-discont": 1})

    def test_video_diagnostics_log_caps_and_counts_at_info_without_rtsp_url(self):
        from fly_ground_peer import GstPeer
        caps = SimpleNamespace(to_string=lambda: "application/x-rtp,profile-level-id=42c015")
        pad = SimpleNamespace(get_current_caps=lambda: caps)
        pay = SimpleNamespace(get_static_pad=lambda name: pad)
        peer = SimpleNamespace(counts={"depay": 125, "q": 124, "pay": 600}, counts_logged_ms=0,
                               pipeline=SimpleNamespace(get_by_name=lambda name: pay))
        with self.assertLogs("fly-ground", level="INFO") as logs:
            GstPeer.log_counts(peer, 5000)
        self.assertIn("42c015", " ".join(logs.output))
        self.assertIn("125", " ".join(logs.output))
        self.assertEqual(peer.counts_logged_ms, 5000)

    def test_rtsp_stall_raises_after_five_silent_seconds(self):
        from fly_ground_peer import GstPeer
        peer = SimpleNamespace(counts={"depay": 0}, stall_check=(None, None))
        GstPeer.check_stall(peer, 0)
        peer.counts["depay"] = 10
        GstPeer.check_stall(peer, 4000)
        GstPeer.check_stall(peer, 9000)
        with self.assertRaisesRegex(RuntimeError, "Video input stalled"):
            GstPeer.check_stall(peer, 9001)

class OfferTimingTest(unittest.TestCase):
    def test_data_only_offer_does_not_wait_for_nonexistent_video_caps(self):
        from unittest.mock import Mock
        from fly_ground_peer import GstPeer
        peer = GstPeer.__new__(GstPeer)
        peer.pipeline, peer.webrtc, peer.Gst = Mock(), Mock(), Mock()
        peer.video_enabled = False
        peer.offer_pending = peer.offer_started = False
        peer.offer()
        peer.pipeline.get_by_name.assert_not_called()
        self.assertEqual(peer.webrtc.emit.call_args.args[0], 'create-offer')
        GstPeer.check_stall(peer, 100000)
        GstPeer.log_counts(peer, 100000)

    def test_offer_waits_for_real_encoded_caps_and_runs_once(self):
        from unittest.mock import Mock
        from fly_ground_peer import GstPeer
        peer = GstPeer.__new__(GstPeer)
        peer.pipeline = Mock()
        peer.webrtc = Mock()
        peer.Gst = Mock()
        peer.offer_pending = peer.offer_started = False
        pad = peer.pipeline.get_by_name.return_value.get_static_pad.return_value
        pad.get_current_caps.return_value = None
        peer.offer()
        peer.webrtc.emit.assert_not_called()
        caps = Mock()
        caps.is_fixed.return_value = True
        caps.get_structure.return_value.has_field.return_value = True
        pad.get_current_caps.return_value = caps
        peer._maybe_offer()
        peer._maybe_offer()
        peer.offer()
        peer.webrtc.emit.assert_called_once()
        self.assertEqual(peer.webrtc.emit.call_args.args[0], 'create-offer')

    def test_incomplete_caps_do_not_allow_an_offer_with_browser_defaults(self):
        from unittest.mock import Mock
        from fly_ground_peer import GstPeer
        peer = GstPeer.__new__(GstPeer)
        peer.pipeline = Mock()
        peer.webrtc = Mock()
        peer.Gst = Mock()
        peer.offer_pending = peer.offer_started = False
        caps = peer.pipeline.get_by_name.return_value.get_static_pad.return_value.get_current_caps.return_value
        caps.is_fixed.return_value = True
        caps.get_structure.return_value.has_field.side_effect = lambda name: name != 'profile-level-id'
        peer.offer()
        peer.webrtc.emit.assert_not_called()
