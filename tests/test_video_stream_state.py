import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
import video_stream_state as state


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.raw = dict(version=1, instance='producer-1', updated_monotonic=100.,
                        transport='RTP', codec='H264', rtp_host='127.0.0.1',
                        rtp_port=5601, rtsp_url=None)

    def read(self, **changes):
        self.path.write_text(json.dumps(dict(self.raw, **changes)))
        return state.read_state(self.path, now=101.)

    def test_rtp_and_rtsp(self):
        source = self.read()
        self.assertEqual((source.transport, source.codec, source.rtp_port), ('RTP', 'H264', 5601))
        self.assertEqual(self.read(transport='RTSP', rtp_host=None, rtp_port=None,
                                   rtsp_url='rtsp://127.0.0.1:8554/devvideo0').rtsp_url,
                         'rtsp://127.0.0.1:8554/devvideo0')

    def test_unavailable_state(self):
        self.assertIsNone(state.read_state(self.path, now=101.))
        for raw in ('{', '[]', 'null', '"x"', '{"version":1}'):
            self.path.write_text(raw)
            self.assertIsNone(state.read_state(self.path, now=101.))
        for bad in (dict(version=2), dict(instance=''), dict(codec='VP8'), dict(transport='UDP'),
                    dict(updated_monotonic=97.), dict(updated_monotonic=102.),
                    dict(updated_monotonic=float('nan')), dict(updated_monotonic=True),
                    dict(rtp_port=0), dict(rtp_port=65536), dict(rtp_port=True),
                    dict(rtp_host='0.0.0.0'), dict(rtp_host='192.0.2.1'),
                    dict(rtsp_url='rtsp://127.0.0.1:8554/video')):
            with self.subTest(bad=bad):
                self.assertIsNone(self.read(**bad))
        self.assertIsNotNone(self.read(codec='H265'))
        for url in ('http://127.0.0.1/video', 'rtsp://example.com/video',
                    'rtsp://user:pass@127.0.0.1/video', 'rtsp://127.0.0.1:99999/video'):
            self.assertIsNone(self.read(transport='RTSP', rtp_host=None, rtp_port=None, rtsp_url=url))

    def test_heartbeat_does_not_change_identity(self):
        self.assertEqual(self.read(), self.read(updated_monotonic=100.5))
        self.assertNotEqual(self.read(), self.read(instance='producer-2'))

    def test_atomic_publisher_and_owned_cleanup(self):
        source = self.read()
        first = state.StatePublisher(self.path, source)
        with patch.object(state.time, 'monotonic', return_value=101.):
            self.assertTrue(first.refresh())
        published = state.read_state(self.path, now=101.)
        self.assertNotEqual(published.instance, source.instance)
        second = state.StatePublisher(self.path, source)
        with patch.object(state.time, 'monotonic', return_value=101.):
            second.refresh()
        first.close()
        self.assertEqual(state.read_state(self.path, now=101.).instance, second.source.instance)
        second.close()
        self.assertFalse(self.path.exists())
        self.assertFalse(any(Path(self.tmp.name).glob(".video-*")))

    def test_old_heartbeat_cannot_reclaim_newer_state_even_after_new_owner_closes(self):
        first = state.StatePublisher(self.path, self.read())
        first.refresh()
        second = state.StatePublisher(self.path, first.source)
        second.refresh()
        self.assertFalse(first.refresh())
        self.assertEqual(state.read_state(self.path).instance, second.source.instance)
        second.close()
        self.assertFalse(first.refresh())
        self.assertFalse(self.path.exists())

    def test_new_publication_survives_concurrent_old_cleanup(self):
        first = state.StatePublisher(self.path, self.read())
        first.refresh()
        second = state.StatePublisher(self.path, first.source)
        at_unlink, allow_unlink, new_done = threading.Event(), threading.Event(), threading.Event()
        original = Path.unlink
        errors = []

        def paused_unlink(path, *args, **kwargs):
            if path == self.path:
                at_unlink.set()
                if not allow_unlink.wait(3):
                    raise TimeoutError('cleanup barrier')
            return original(path, *args, **kwargs)

        def cleanup():
            try:
                first.close()
            except Exception as exc:
                errors.append(exc)

        def publish():
            try:
                second.refresh()
            except Exception as exc:
                errors.append(exc)
            finally:
                new_done.set()

        with patch.object(Path, 'unlink', paused_unlink):
            old = threading.Thread(target=cleanup)
            new = threading.Thread(target=publish)
            old.start()
            try:
                self.assertTrue(at_unlink.wait(3))
                new.start()
                new_done.wait(1)  # New publication must serialize behind old cleanup.
            finally:
                allow_unlink.set()
                old.join(3)
                if new.ident is not None:
                    new.join(3)
        self.assertFalse(old.is_alive())
        self.assertFalse(new.is_alive())
        self.assertEqual(errors, [])
        current = state.read_state(self.path)
        self.assertIsNotNone(current, 'old cleanup deleted the new producer state')
        self.assertEqual(current.instance, second.source.instance)

    def test_helpers_and_destination_validation(self):
        self.assertEqual(state.mount_name('/dev/video0'), 'devvideo0')
        self.assertEqual(state.mount_name('rtsp://u:p@192.0.2.1:554/11'), '19202155411')
        self.assertEqual(state.effective_codec('video/x-h264', 'H265'), 'H264')
        self.assertEqual(state.effective_codec('video/x-h265', 'H264'), 'H265')
        self.assertEqual(state.effective_codec('video/x-raw', 'H265'), 'H265')
        self.assertEqual(state.rtp_destinations('192.0.2.1:5600', None), [('192.0.2.1', 5600)])
        self.assertEqual(state.rtp_destinations('127.0.0.1:5601', '5601'), [('127.0.0.1', 5601)])
        self.assertEqual(state.rtp_destinations('[::1]:5600', '5601'), [('::1', 5600), ('127.0.0.1', 5601)])
        for remote, local in [('x ! fakesink:5600', None), ('host:0', None), ('host:foo', None),
                              ('host:5600', ''), ('host:5600', '65536')]:
            with self.subTest(remote=remote, local=local), self.assertRaises(ValueError):
                state.rtp_destinations(remote, local)
