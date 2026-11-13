"""Run on a GStreamer host to exercise the production H.264 send chain."""

from collections import Counter
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib
except (ImportError, ValueError):
    Gst = GLib = None


@unittest.skipIf(Gst is None, "requires GStreamer/PyGObject")
class VideoQueueTest(unittest.TestCase):
    def test_early_offer_advertises_actual_encoder_profile_and_packetization(self):
        gi.require_version("GstWebRTC", "1.0")
        gi.require_version("GstSdp", "1.0")
        from gi.repository import GstWebRTC, GstSdp
        from fly_ground_peer import GstPeer
        from fly_ground_video import VIDEO_CAPS, video_send_chain
        Gst.init(None)
        for plugin in ("webrtcbin", "nicesrc", "nicesink", "videotestsrc", "x264enc", "rtph264pay"):
            if Gst.ElementFactory.find(plugin) is None:
                self.skipTest(f"requires {plugin}")
        pipeline = Gst.parse_launch(
            "webrtcbin name=webrtc bundle-policy=max-bundle "
            "videotestsrc is-live=true pattern=snow "
            "! video/x-raw,width=480,height=320,framerate=25/1 "
            "! x264enc tune=zerolatency bitrate=500 speed-preset=superfast "
            "key-int-max=25 ! video/x-h264,profile=high " + video_send_chain() + "! webrtc.")
        peer = GstPeer.__new__(GstPeer)
        peer.pipeline, peer.webrtc, peer.Gst = pipeline, pipeline.get_by_name("webrtc"), Gst
        peer.offer_pending = peer.offer_started = False
        peer.webrtc.emit("get-transceiver", 0).set_property("codec-preferences", Gst.Caps.from_string(VIDEO_CAPS))
        loop, descriptions = GLib.MainLoop(), []
        def offered(promise, *_):
            reply = promise.get_reply()
            if reply and reply.has_field("offer"):
                descriptions.append(reply.get_value("offer").sdp.as_text())
            loop.quit()
        peer._offer_ready = offered
        def poll():
            peer._maybe_offer()
            return True
        timer = GLib.timeout_add(20, poll)
        expired = []
        def timeout():
            expired.append(True); loop.quit(); return False
        timeout_id = GLib.timeout_add_seconds(5, timeout)
        try:
            pipeline.set_state(Gst.State.READY)
            peer.offer()  # Request before there is any encoded format.
            self.assertFalse(peer.offer_started)
            pipeline.set_state(Gst.State.PLAYING)
            loop.run()
            self.assertTrue(descriptions, "offer was not created after encoded caps arrived")
            actual = pipeline.get_by_name("pay").get_static_pad("src").get_current_caps().get_structure(0)
            self.assertIn("packetization-mode=1", descriptions[0])
            self.assertIn("profile-level-id=" + actual.get_string("profile-level-id"), descriptions[0])
        finally:
            pipeline.set_state(Gst.State.NULL)
            GLib.source_remove(timer)
            if not expired: GLib.source_remove(timeout_id)

    def test_backpressure_never_delivers_partial_rtp_frames(self):
        Gst.init(None)
        for plugin in ("videotestsrc", "x264enc", "h264parse", "rtph264pay"):
            if Gst.ElementFactory.find(plugin) is None:
                self.skipTest(f"requires {plugin}")

        # Exercise the production common send chain for both source transports.
        from fly_ground_video import video_send_chain
        chain = video_send_chain().split("! h264parse", 1)[1]
        pipeline = Gst.parse_launch(
            "videotestsrc is-live=true num-buffers=60 pattern=snow "
            "! video/x-raw,width=480,height=320,framerate=25/1 "
            "! x264enc tune=zerolatency bitrate=500 speed-preset=ultrafast key-int-max=25 "
            "! h264parse" + chain + "! identity sleep-time=50000 ! fakesink name=sink sync=false")
        produced, delivered = Counter(), Counter()

        def count_packets(counts):
            def probe(_pad, info):
                if info.type & Gst.PadProbeType.BUFFER_LIST:
                    buffers = info.get_buffer_list()
                    packets = [buffers.get(i) for i in range(buffers.length())]
                else:
                    packets = [info.get_buffer()]
                for buffer in packets:
                    ok, mapped = buffer.map(Gst.MapFlags.READ)
                    if ok:
                        counts[int.from_bytes(mapped.data[4:8], "big")] += 1
                        buffer.unmap(mapped)
                return Gst.PadProbeReturn.OK
            return probe

        pay = pipeline.get_by_name("pay")
        pay.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, count_packets(produced))
        pipeline.get_by_name("sink").get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, count_packets(delivered))
        loop, errors, ended = GLib.MainLoop(), [], []
        bus = pipeline.get_bus()
        bus.add_signal_watch()

        def message(_bus, msg):
            if msg.type == Gst.MessageType.ERROR:
                errors.append(msg.parse_error()[0].message)
                loop.quit()
            elif msg.type == Gst.MessageType.EOS:
                ended.append(True)
                loop.quit()

        handler = bus.connect("message", message)
        timed_out = []

        def timeout():
            timed_out.append(True)
            loop.quit()
            return False

        timer = GLib.timeout_add_seconds(15, timeout)
        try:
            pipeline.set_state(Gst.State.PLAYING)
            loop.run()
        finally:
            pipeline.set_state(Gst.State.NULL)
            if not timed_out:
                GLib.source_remove(timer)
            bus.disconnect(handler)
            bus.remove_signal_watch()
        self.assertFalse(errors, errors)
        self.assertTrue(ended, "synthetic stream timed out")
        self.assertGreater(len(delivered), 0)
        self.assertLess(len(delivered), 60, "test did not apply backpressure")
        partial = {ts: (count, produced[ts]) for ts, count in delivered.items()
                   if count != produced[ts]}
        self.assertEqual(partial, {}, "queue discarded RTP fragments of delivered frames")


@unittest.skipIf(Gst is None, 'requires GStreamer/PyGObject')
class NativeOnlyPeerTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_only_offer_keeps_data_channels_without_video(self):
        from unittest.mock import Mock
        from fly_ground_peer import GstPeer
        from video_stream_state import VideoSource
        Gst.init(None)
        for plugin in ('webrtcbin', 'nicesrc', 'nicesink'):
            if Gst.ElementFactory.find(plugin) is None:
                self.skipTest(f'requires {plugin}')
        source = VideoSource('native', 'RTP', 'H265', '127.0.0.1', None,
                             webrtc_enabled=False, output_mode='rpanion')
        messages = []
        peer = GstPeer(source, [], messages.append, Mock(), lambda: None)
        self.addCleanup(peer.close)
        self.assertIsNone(peer.pipeline.get_by_name('source'))
        self.assertIsNone(peer.pipeline.get_by_name('pay'))
        self.assertEqual(peer.control.get_property('label'), 'control')
        self.assertEqual(peer.session.get_property('label'), 'session')
        peer.offer()
        deadline = asyncio.get_running_loop().time() + 5
        while not any(m.get('type') == 'offer' for m in messages) and asyncio.get_running_loop().time() < deadline:
            context = GLib.MainContext.default()
            for _ in range(20):
                if not context.pending(): break
                context.iteration(False)
            peer.poll_bus()
            await asyncio.sleep(.02)
        offers = [m['sdp'] for m in messages if m.get('type') == 'offer']
        self.assertTrue(offers, 'data-only peer never offered')
        self.assertIn('m=application', offers[0])
        self.assertNotIn('m=video', offers[0])


if __name__ == "__main__":
    unittest.main()
