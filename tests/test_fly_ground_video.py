"""Run on a GStreamer host to exercise the production H.264 send chain."""

from collections import Counter
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


if __name__ == "__main__":
    unittest.main()
