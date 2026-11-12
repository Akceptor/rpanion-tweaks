"""Real GStreamer loopback checks; no camera or remote services required."""
import importlib.util
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from fly_ground_video import source_chain, video_send_chain
from video_stream_state import VideoSource

try:
    import gi
    gi.require_version('Gst', '1.0')
    gi.require_version('GstRtspServer', '1.0')
    from gi.repository import Gst
    Gst.init(None)
    HAVE_GST = all(Gst.ElementFactory.find(name) for name in (
        'x264enc', 'videotestsrc', 'rtph264pay', 'rtph264depay', 'h264parse',
        'multiudpsink', 'udpsrc', 'rtpjitterbuffer', 'appsink'))
except (ImportError, ValueError):
    Gst = None
    HAVE_GST = False


@unittest.skipUnless(HAVE_GST, 'requires real GStreamer, GstRtspServer and x264enc')
class FanoutTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('fanout_video_server',
            Path(__file__).resolve().parents[1] / 'python' / 'video-server.py')
        self.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server)

    def receiver(self, port=0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.bind(('127.0.0.1', port))
        sock.settimeout(3)
        return sock

    def sender(self, remote_port, local_port, remote_host='127.0.0.1'):
        args = SimpleNamespace(videosource='testsrc', height=96, width=128, bitrate=200,
            format='video/x-raw', rotation=0, fps=25, timestamp=False, compression='H264',
            udp=f'{remote_host}:{remote_port}')
        camera = ('videotestsrc is-live=true ! video/x-raw,width=128,height=96,framerate=25/1 '
                  '! x264enc tune=zerolatency key-int-max=10 '
                  '! rtph264pay config-interval=1 pt=96')
        with patch.object(self.server, 'getPipeline', return_value=camera):
            pipeline = self.server.build_rtp_pipeline(args, local_port)
        self.addCleanup(pipeline.set_state, Gst.State.NULL)
        self.assertNotEqual(pipeline.set_state(Gst.State.PLAYING), Gst.StateChangeReturn.FAILURE)
        return pipeline

    def test_same_rtp_packets_and_late_receiver_restart(self):
        remote = self.receiver()
        local = self.receiver()
        port = local.getsockname()[1]
        local.close()  # Sender starts without a local listener.
        pipeline = self.sender(remote.getsockname()[1], port)
        first = remote.recv(65535)
        self.assertEqual(first[0] >> 6, 2)  # RTP version.
        for _ in range(2):
            local = self.receiver(port)
            packet = local.recv(65535)
            # Match an exact datagram, including SSRC, sequence, timestamp and payload.
            deadline = time.monotonic() + 3
            match = False
            while time.monotonic() < deadline:
                if remote.recv(65535) == packet:
                    match = True
                    break
            self.assertTrue(match, 'fanout did not preserve the same RTP packet')
            local.close()
            remote.recv(65535)  # Mission Planner branch continues with no local receiver.
        error = pipeline.get_bus().timed_pop_filtered(0, Gst.MessageType.ERROR)
        self.assertIsNone(error)

    def test_unreachable_remote_does_not_stop_local(self):
        local = self.receiver()
        pipeline = self.sender(5600, local.getsockname()[1], '192.0.2.1')
        self.assertEqual(local.recv(65535)[0] >> 6, 2)
        self.assertIsNone(pipeline.get_bus().timed_pop_filtered(0, Gst.MessageType.ERROR))

    def test_rtp_input_depayloads_late_joined_video(self):
        remote, reserve = self.receiver(), self.receiver()
        port = reserve.getsockname()[1]
        reserve.close()
        self.sender(remote.getsockname()[1], port)
        remote.recv(65535)
        source = VideoSource('test', 'RTP', 'H264', '127.0.0.1', port)
        receiver = Gst.parse_launch(source_chain(source) + video_send_chain() +
                                    '! appsink name=video sync=false max-buffers=100 drop=true')
        element = receiver.get_by_name('source')
        element.set_property('address', source.rtp_host)
        element.set_property('port', source.rtp_port)
        self.addCleanup(receiver.set_state, Gst.State.NULL)
        receiver.set_state(Gst.State.PLAYING)
        sample = receiver.get_by_name('video').emit('try-pull-sample', 4 * Gst.SECOND)
        self.assertIsNotNone(sample, 'RTP input did not produce repayloaded H264')
        self.assertIn('encoding-name=(string)H264', sample.get_caps().to_string())
        self.assertIsNone(receiver.get_bus().timed_pop_filtered(0, Gst.MessageType.ERROR))
