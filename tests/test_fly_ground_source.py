import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from fly_ground_config import Settings
import fly_ground_video as video
from video_stream_state import StatePublisher, VideoSource


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.settings = SimpleNamespace(video_source='auto', video_state_file=str(self.path),
                                        rtsp_url='rtsp://127.0.0.1:8554/old')
        self.source = VideoSource('test', 'RTP', 'H264', '127.0.0.1', 5601)

    def publish(self, source=None):
        publisher = StatePublisher(self.path, source or self.source)
        publisher.refresh()
        return publisher

    def test_settings_preserve_legacy_and_validate_auto(self):
        base = {'FLY_ROOM_CODE': 'CRAWLER-7KQ2-M9XF-4N6P', 'FLY_SITE_URL': 'https://example.com'}
        self.assertEqual(Settings.from_env(base).video_source, 'rtsp')
        s = Settings.from_env(dict(base, FLY_VIDEO_SOURCE='auto'))
        self.assertEqual(s.video_state_file, '/run/rpanion-video/state.json')
        for changes in ({'FLY_VIDEO_SOURCE': 'guess'}, {'FLY_VIDEO_STATE_FILE': 'relative'}):
            with self.assertRaises(ValueError):
                Settings.from_env(dict(base, **changes))

    def test_auto_has_no_fallback_and_h265_does_not_join(self):
        with self.assertRaises(video.VideoUnavailable):
            video.resolve_source(self.settings)
        self.publish(VideoSource('test', 'RTP', 'H265', '127.0.0.1', 5601))
        with self.assertRaisesRegex(video.VideoUnavailable, 'H265'):
            video.resolve_source(self.settings)
        self.settings.video_source = 'rtsp'
        self.assertEqual(video.resolve_source(self.settings).rtsp_url, self.settings.rtsp_url)

    def test_watcher_ignores_heartbeat_detects_restart_mode_and_disappearance(self):
        publisher = self.publish()
        watcher = video.SourceWatcher(self.settings, video.resolve_source(self.settings))
        watcher.poll(0)
        publisher.refresh()
        watcher.poll(500)
        self.publish()
        watcher.poll(999)  # Not due until 1000 ms.
        with self.assertRaises(video.VideoSourceChanged):
            watcher.poll(1000)
        source = video.resolve_source(self.settings)
        watcher = video.SourceWatcher(self.settings, source)
        self.publish(VideoSource('test', 'RTSP', 'H264', rtsp_url='rtsp://127.0.0.1:8554/video'))
        with self.assertRaises(video.VideoSourceChanged):
            watcher.poll(0)
        watcher = video.SourceWatcher(self.settings, video.resolve_source(self.settings))
        self.path.unlink()
        with self.assertRaises(video.VideoSourceChanged):
            watcher.poll(0)

    def test_source_chains_share_complete_frame_queue(self):
        rtp = video.source_chain(self.source)
        rtsp = video.source_chain(VideoSource('test', 'RTSP', 'H264', rtsp_url='rtsp://127.0.0.1:8554/video'))
        self.assertIn('udpsrc name=source', rtp)
        self.assertIn('reuse=false', rtp)
        self.assertIn('rtpjitterbuffer latency=20 do-lost=true', rtp)
        self.assertNotIn('leaky', rtp)
        self.assertIn('rtspsrc name=source protocols=tcp latency=0', rtsp)
        self.assertEqual(rtp.split('! rtph264depay')[1], rtsp.split('! rtph264depay')[1])
        chain = video.video_send_chain()
        self.assertLess(chain.index('alignment=au'), chain.index('leaky=downstream'))
        self.assertLess(chain.index('leaky=downstream'), chain.index('rtph264pay'))


class WaitTest(unittest.IsolatedAsyncioTestCase):
    async def test_waits_locally_until_usable_source(self):
        source = VideoSource('test', 'RTP', 'H264', '127.0.0.1', 5601)
        with patch.object(video, 'resolve_source', side_effect=[video.VideoUnavailable('waiting'), source]), \
                patch.object(video.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
            self.assertEqual(await video.wait_for_source(object()), source)
            sleep.assert_awaited_once_with(1)

    async def test_source_change_cleans_up_existing_session(self):
        import fly_ground
        settings = SimpleNamespace(site_url='https://example.com', room_id='a' * 64,
            channels=(1, 3), throttle_channel=3, session_seconds=3600,
            video_source='auto', video_state_file='/nonexistent')
        source = VideoSource('test', 'RTP', 'H264', '127.0.0.1', 5601)
        writer, monitor, adapter, peer = Mock(), Mock(), Mock(), Mock()
        protocols, released = [], []

        def make_peer(_source, _servers, _signal, protocol, _closed):
            protocol.open(1000)
            protocol.arbiter.owner = 'browser'
            protocols.append(protocol)
            return peer

        adapter.send.side_effect = lambda _now: released.append(protocols[0].arbiter.take_release())
        ws = AsyncMock()
        context = AsyncMock()
        context.__aenter__.return_value = ws
        with patch.dict(sys.modules, {'gi.repository': SimpleNamespace(GLib=Mock())}), \
                patch.object(fly_ground, 'ice_servers', return_value=[]), \
                patch.object(fly_ground, 'mavlink_connections', return_value=(writer, monitor)), \
                patch.object(fly_ground, 'MavlinkAdapter', return_value=adapter), \
                patch.object(fly_ground, 'GstPeer', side_effect=make_peer), \
                patch('websockets.connect', return_value=context):
            with self.assertRaises(video.VideoSourceChanged):
                await fly_ground.run_session(settings, source)
        peer.close.assert_called_once()
        adapter.send.assert_called_once()
        writer.close.assert_called_once()
        monitor.close.assert_called_once()
        self.assertEqual((released[0][0], released[0][2]), (0, 0))
        self.assertFalse(protocols[0].arbiter.peer_connected)
        self.assertEqual(protocols[0].arbiter.owner, 'idle')

    async def test_mode_change_resets_retry_without_bypassing_room_rejection_backoff(self):
        import fly_ground
        source = VideoSource('test', 'RTP', 'H264', '127.0.0.1', 5601)
        with patch.object(fly_ground, 'wait_for_source', new_callable=AsyncMock, return_value=source), \
                patch.object(fly_ground, 'run_session', new_callable=AsyncMock,
                    side_effect=[video.VideoSourceChanged('changed'),
                                 fly_ground.RoomRejected(4001), asyncio.CancelledError()]), \
                patch.object(fly_ground.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await fly_ground.run(object())
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 30])
