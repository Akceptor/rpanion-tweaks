import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from test_battery import vs


class TransportTest(unittest.TestCase):
    def args(self, **changes):
        return SimpleNamespace(**dict(dict(videosource='/dev/video0', height=480, width=640,
            bitrate=2000, format='video/x-raw', rotation=0, fps=25, timestamp=True,
            compression='H264', transport='RTP', udp='192.0.2.1:5600', multirtsp=''), **changes))

    def test_configuration_and_descriptor(self):
        args = self.args()
        self.assertEqual(vs.video_options(args, {}), (None, None))
        env = {'RPANION_WEBRTC_RTP_PORT': '5601', 'RPANION_VIDEO_STATE_FILE': '/run/video/state.json'}
        self.assertEqual(vs.video_options(args, env), (5601, '/run/video/state.json'))
        source = vs.describe_video(args, 5601)
        self.assertEqual((source.transport, source.rtp_host, source.rtp_port), ('RTP', '127.0.0.1', 5601))
        source = vs.describe_video(self.args(transport='RTSP', format='video/x-h265'), None)
        self.assertEqual((source.codec, source.rtsp_url), ('H265', 'rtsp://127.0.0.1:8554/devvideo0'))
        with self.assertRaises(ValueError):
            vs.video_options(args, {'RPANION_VIDEO_STATE_FILE': '/run/state.json'})
        with self.assertRaises(ValueError):
            vs.video_options(args, {'RPANION_WEBRTC_RTP_PORT': '0'})
        self.assertEqual(vs.video_options(self.args(multirtsp='a;b'), env), (5601, None))

    def test_fanout_uses_one_camera_pipeline_and_element_destinations(self):
        sink, pipeline = Mock(), Mock()
        pipeline.get_by_name.return_value = sink
        with patch.object(vs, 'getPipeline', return_value='camera ! rtph264pay') as camera, \
                patch.object(vs.Gst, 'parse_launch', return_value=pipeline) as launch:
            self.assertIs(vs.build_rtp_pipeline(self.args(), 5601), pipeline)
        camera.assert_called_once()
        launch.assert_called_once_with('camera ! rtph264pay ! multiudpsink name=output')
        self.assertEqual(sink.emit.call_args_list, [unittest.mock.call('add', '192.0.2.1', 5600),
                                                  unittest.mock.call('add', '127.0.0.1', 5601)])

    def test_legacy_sink_and_multicast(self):
        sink, pipeline = Mock(), Mock()
        pipeline.get_by_name.return_value = sink
        with patch.object(vs, 'getPipeline', return_value='camera'), \
                patch.object(vs.Gst, 'parse_launch', return_value=pipeline) as launch:
            vs.build_rtp_pipeline(self.args(udp='239.1.2.3:5600'), None)
        launch.assert_called_once_with('camera ! udpsink name=output')
        sink.set_property.assert_any_call('host', '239.1.2.3')
        sink.set_property.assert_any_call('port', 5600)
        sink.set_property.assert_any_call('auto-multicast', True)


class LifecycleTest(unittest.TestCase):
    def test_failed_heartbeat_stops_pipeline_and_removes_state(self):
        pipeline, publisher, loop = Mock(), Mock(), Mock()
        publisher.refresh.side_effect = [True, OSError('read-only runtime directory')]
        callbacks = []
        glib = Mock()
        glib.timeout_add_seconds.side_effect = lambda _seconds, callback: callbacks.append(callback) or 4
        loop.run.side_effect = lambda: callbacks[0]()
        with patch.object(vs, 'GLib', glib), patch.object(vs, 'attach_stats'):
            with self.assertRaisesRegex(OSError, 'read-only'):
                vs.run_video(loop, pipeline, publisher)
        loop.quit.assert_called_once()
        publisher.close.assert_called_once()
        self.assertEqual(pipeline.set_state.call_args_list[-1], unittest.mock.call(vs.Gst.State.NULL))

    def test_failed_start_does_not_publish_and_still_cleans_up(self):
        pipeline, publisher = Mock(), Mock()
        pipeline.set_state.return_value = vs.Gst.StateChangeReturn.FAILURE
        with patch.object(vs, 'GLib', Mock()), patch.object(vs, 'attach_stats'):
            with self.assertRaisesRegex(RuntimeError, 'failed to start'):
                vs.run_video(Mock(), pipeline, publisher)
        publisher.refresh.assert_not_called()
        publisher.close.assert_called_once()
