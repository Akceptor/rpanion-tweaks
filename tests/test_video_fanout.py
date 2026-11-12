"""Real GStreamer loopback checks; no camera or remote services required."""
import importlib.util
import re
from pathlib import Path
import socket
import sys
import threading
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
        'udpsink', 'udpsrc', 'rtpjitterbuffer', 'appsink'))
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
        camera = ('videotestsrc is-live=true pattern=snow ! video/x-raw,width=128,height=96,framerate=25/1 '
                  '! x264enc tune=zerolatency bitrate=500 key-int-max=10')
        def source(*_args, payload=True):
            return camera + (' ! rtph264pay config-interval=1 name=pay0 pt=96' if payload else '')
        with patch.object(self.server, 'getPipeline', side_effect=source):
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

    def test_production_synthetic_source_keeps_wall_clock_frame_rate(self):
        remote, local = self.receiver(), self.receiver()
        args = SimpleNamespace(videosource='testsrc', height=96, width=128, bitrate=200,
            format='video/x-raw', rotation=0, fps=25, timestamp=False, compression='H264',
            udp=f'127.0.0.1:{remote.getsockname()[1]}')
        with patch.object(self.server, 'is_pi_5_or_later', return_value=False):
            pipeline = self.server.build_rtp_pipeline(args, local.getsockname()[1])
        self.addCleanup(pipeline.set_state, Gst.State.NULL)
        pipeline.set_state(Gst.State.PLAYING)
        started, frames = time.monotonic(), 0
        while time.monotonic() - started < 1.1:
            packet = local.recv(65535)
            frames += bool(packet[1] & 0x80)
        self.assertGreaterEqual(frames, 20)
        self.assertLessEqual(frames, 32, 'synthetic source ran faster than its configured frame rate')

    def test_software_encoder_emits_browser_constrained_baseline_h264(self):
        with patch.object(self.server, 'is_pi_5_or_later', return_value=False):
            chain = self.server.getPipeline('testsrc', 480, 640, 500, 'video/x-raw',
                                            0, 25, False, 'H264', payload=False)
        pipeline = Gst.parse_launch(chain + ' ! h264parse '
            '! video/x-h264,stream-format=byte-stream,alignment=au '
            '! appsink name=encoded sync=false')
        self.addCleanup(pipeline.set_state, Gst.State.NULL)
        pipeline.set_state(Gst.State.PLAYING)
        sample = pipeline.get_by_name('encoded').emit('try-pull-sample', 4 * Gst.SECOND)
        self.assertIsNotNone(sample)
        buffer = sample.get_buffer()
        data = buffer.extract_dup(0, buffer.get_size())
        nals = [nal for nal in re.split(b'\x00\x00\x00?\x01', data) if nal]
        sps = next(nal for nal in nals if nal[0] & 31 == 7)
        self.assertEqual(sps[1], 66, 'software encoder emitted optional High/Main H264 profile')
        self.assertTrue(sps[2] & 0x40, 'baseline stream is missing constraint_set1_flag')
        self.assertEqual(sample.get_caps().get_structure(0).get_string('profile'), 'constrained-baseline')

    def test_unreachable_remote_does_not_stop_local(self):
        local = self.receiver()
        pipeline = self.sender(5600, local.getsockname()[1], '192.0.2.1')
        self.assertEqual(local.recv(65535)[0] >> 6, 2)
        self.assertIsNone(pipeline.get_bus().timed_pop_filtered(0, Gst.MessageType.ERROR))

    def test_blocked_remote_keeps_local_at_camera_frame_rate(self):
        self.assert_blocked_output_isolated('output', 'local_output')

    def test_blocked_local_keeps_remote_at_camera_frame_rate(self):
        self.assert_blocked_output_isolated('local_output', 'output')

    def assert_blocked_output_isolated(self, blocked_name, healthy_name):
        remote, local = self.receiver(), self.receiver()
        pipeline = self.sender(remote.getsockname()[1], local.getsockname()[1])
        remote.recv(65535)
        local.recv(65535)
        receivers = {'output': remote, 'local_output': local}
        healthy = receivers[healthy_name]
        pad = pipeline.get_by_name(blocked_name).get_static_pad('sink')
        blocked = threading.Event()

        def hold_remote(_pad, _info):
            blocked.set()
            return Gst.PadProbeReturn.OK

        probe = pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM, hold_remote)
        self.addCleanup(pad.remove_probe, probe)
        self.assertTrue(blocked.wait(2), 'remote branch did not enter the blocked send')
        healthy.settimeout(.1)
        started = time.monotonic()
        frames = []
        while time.monotonic() - started < 2:
            try:
                packet = healthy.recv(65535)
            except socket.timeout:
                continue
            if packet[1] & 0x80:
                frames.append(time.monotonic())
        self.assertGreaterEqual(len(frames), 40, 'blocked remote stalled local video')
        self.assertLess(max(b - a for a, b in zip(frames, frames[1:])), .15)
        self.assertIsNone(pipeline.get_bus().timed_pop_filtered(0, Gst.MessageType.ERROR))

    def test_slow_remote_drops_whole_frames_and_preserves_fragments(self):
        remote, local = self.receiver(), self.receiver()
        pipeline = self.sender(remote.getsockname()[1], local.getsockname()[1])
        # Slow each complete AU before packetization, forcing its branch queue
        # to drop frames. Record and check the real UDP fragment stream.
        pad = pipeline.get_by_name('remote_pay').get_static_pad('sink')
        def slow(_pad, _info):
            time.sleep(.12)
            return Gst.PadProbeReturn.OK
        probe = pad.add_probe(Gst.PadProbeType.BUFFER, slow)
        self.addCleanup(pad.remove_probe, probe)
        remote.settimeout(.3)
        started = time.monotonic()
        packets = []
        while time.monotonic() - started < 2:
            try:
                packets.append(remote.recv(65535))
            except socket.timeout:
                continue
        # Ignore the last frame if capture ended during a send.
        markers = [i for i, p in enumerate(packets) if p[1] & 0x80]
        self.assertGreater(len(markers), 5)
        self.assertLess(len(markers), 40, 'remote branch did not experience backpressure')
        complete = packets[:markers[-1] + 1]
        timestamps = [int.from_bytes(packets[i][4:8], 'big') for i in markers]
        self.assertTrue(any((b-a) % 2**32 > 7200 for a, b in zip(timestamps, timestamps[1:])))
        sequence = [int.from_bytes(p[2:4], 'big') for p in complete]
        self.assertTrue(all((b-a) % 65536 == 1 for a, b in zip(sequence, sequence[1:])),
                        'RTP packets were discarded after fragmentation')
        fragments = [p[13] for p in complete if p[12] & 31 == 28]
        self.assertGreater(len(fragments), 0, 'test must exercise fragmented H264')
        open_fragment = False
        for header in fragments:
            if header & 0x80:
                self.assertFalse(open_fragment, 'previous fragmented NAL was truncated')
                open_fragment = True
            self.assertTrue(open_fragment, 'fragmented NAL is missing its start')
            if header & 0x40:
                open_fragment = False
        self.assertFalse(open_fragment, 'fragmented NAL is missing its end')
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
