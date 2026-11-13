"""Exercise producer self-reexec with a synthetic camera and isolated UDP/files."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from video_output import VideoOutputStore
from video_stream_state import read_state

try:
    import gi
    gi.require_version('Gst', '1.0')
    gi.require_version('GstRtspServer', '1.0')
    from gi.repository import Gst
    Gst.init(None)
    HAVE_GST = all(Gst.ElementFactory.find(name) for name in ('videotestsrc', 'x264enc', 'udpsink'))
except (ImportError, ValueError):
    HAVE_GST = False


@unittest.skipUnless(HAVE_GST, 'requires real GStreamer synthetic video plugins')
class ProducerRestartTest(unittest.TestCase):
    def test_output_changes_reexec_same_process_and_publish_new_instance(self):
        with tempfile.TemporaryDirectory(prefix='video-output-process-') as folder:
            folder = Path(folder)
            output = VideoOutputStore(folder / 'output.json')
            state_path = folder / 'state.json'
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as remote, \
                    socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as local, \
                    (folder / 'producer.log').open('w+') as log:
                for sock in (local, remote):
                    sock.bind(('127.0.0.1', 0))
                    sock.settimeout(.2)
                env = dict(os.environ, RPANION_VIDEO_OUTPUT_FILE=str(output.path),
                    RPANION_VIDEO_STATE_FILE=str(state_path),
                    RPANION_WEBRTC_RTP_PORT=str(local.getsockname()[1]),
                    MAVLINK_BATT2='0', STATS_MAVLINK_PORT='0', STATS_LIMA_PORT='0',
                    STATS_AGPS_PORT='0', PI_MAH_FILE=str(folder / 'mah.json'))
                script = Path(__file__).resolve().parents[1] / 'python' / 'video-server.py'
                process = subprocess.Popen([sys.executable, str(script), '--videosource=testsrc',
                    '--width=128', '--height=96', '--fps=25', '--bitrate=200',
                    '--transport=RTP', '--udp=127.0.0.1:' + str(remote.getsockname()[1])],
                    env=env, stdout=log, stderr=subprocess.STDOUT)
                original_pid = process.pid

                def wait_applied(mode, previous=None):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        source = read_state(state_path)
                        if source and source.output_mode == mode and source.instance != previous:
                            self.assertIsNone(process.poll())
                            self.assertEqual(process.pid, original_pid)
                            return source
                        if process.poll() is not None:
                            log.seek(0)
                            self.fail('synthetic producer exited: ' + log.read()[-3000:])
                        time.sleep(.05)
                    self.fail('producer did not apply ' + mode)

                def drain(sock):
                    sock.setblocking(False)
                    try:
                        while sock.recv(65535): pass
                    except BlockingIOError:
                        pass
                    finally:
                        sock.settimeout(.2)

                try:
                    previous = wait_applied('both').instance
                    for mode, selected, muted in (('webrtc', local, remote), ('rpanion', remote, local)):
                        output.save(mode, output.read()['revision'])
                        source = wait_applied(mode, previous)
                        previous = source.instance
                        self.assertEqual(source.webrtc_enabled, mode != 'rpanion')
                        # Remove packets buffered before the old pipeline stopped.
                        drain(muted)
                        self.assertEqual(selected.recv(65535)[0] >> 6, 2)
                        with self.assertRaises(socket.timeout):
                            muted.recv(65535)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                self.assertFalse(state_path.exists(), 'producer did not retire its state on shutdown')
