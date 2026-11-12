"""Run the real async orchestration with fake media and signaling transports."""

import asyncio
import base64
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
import fly_ground as ground
from fly_ground_config import Settings


TOKEN = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('=')


class RuntimeTest(unittest.TestCase):
    def exercise(self, messages, close_code=1000, clock_step=None):
        import websockets
        from websockets.frames import Close
        events, sent = [], []
        settings = Settings.from_env({'FLY_ROOM_CODE': 'CRAWLER-1234-5678-9012',
                                      'FLY_SITE_URL': 'https://site.example'})
        close = (websockets.ConnectionClosedOK(Close(close_code, ''), Close(close_code, ''), True)
                 if close_code is not None else None)

        class Socket:
            async def recv(self):
                if not messages:
                    if close is None:
                        await asyncio.sleep(10)
                    raise close
                msg = messages.pop(0)
                events.append(msg['type'])
                return json.dumps(msg)
            async def send(self, raw): sent.append(json.loads(raw))

        class Connection:
            async def __aenter__(self): return Socket()
            async def __aexit__(self, *args): events.append('socket-exit')

        class Peer:
            def __init__(self, _rtsp, servers, _queue, protocol, _closed):
                events.append('peer-created')
                self.protocol = protocol
                self.servers = servers
            def poll_bus(self): pass
            def offer(self):
                events.append('offer')
                self.protocol.open(ground.now_ms())
            def on_signal(self, msg): events.append('signal:' + msg['type'])
            def send_session(self, msg): pass
            def close(self): events.append('peer-closed')

        context = SimpleNamespace(pending=lambda: False)
        repository = ModuleType('gi.repository')
        repository.GLib = SimpleNamespace(MainContext=SimpleNamespace(default=lambda: context))
        gi = ModuleType('gi')
        gi.repository = repository
        writer, monitor = mock.Mock(), mock.Mock()
        adapter = mock.Mock()
        clock = iter(range(0, 1_000_000, clock_step or 1))
        real_clock = ground.now_ms
        with (mock.patch.dict(sys.modules, {'gi': gi, 'gi.repository': repository}),
              mock.patch.object(ground, 'now_ms', side_effect=(lambda: next(clock)) if clock_step else real_clock),
              mock.patch('websockets.connect', return_value=Connection()) as connect,
              mock.patch.object(ground, 'mavlink_connections', return_value=(writer, monitor)),
              mock.patch.object(ground, 'MavlinkAdapter', return_value=adapter),
              mock.patch.object(ground, 'GstPeer', Peer)):
            try:
                asyncio.run(ground.run_session(settings, TOKEN))
            except Exception as exc:
                error = exc
            else:
                error = None
        return SimpleNamespace(events=events, sent=sent, connect=connect, writer=writer,
                               monitor=monitor, adapter=adapter, error=error)

    def test_authenticated_bootstrap_precedes_peer_and_socket_loss_ends_active_session(self):
        ready = {'type': 'session-ready', 'generation': 'gen',
                 'iceServers': [{'urls': 'stun:stun.cloudflare.com:3478'}],
                 'expiresAt': ground.time.time() * 1000 + 7_140_000, 'turnStatus': 'unavailable'}
        result = self.exercise([{'type': 'room-ready', 'generation': 'gen'}, ready,
                                {'type': 'peer-joined'}])
        self.assertIsInstance(result.error, ConnectionError)
        self.assertLess(result.events.index('session-ready'), result.events.index('peer-created'))
        self.assertLess(result.events.index('peer-created'), result.events.index('offer'))
        self.assertIn({'type': 'rtc-ready', 'generation': 'gen'}, result.sent)
        self.assertIn({'type': 'host-heartbeat', 'generation': 'gen'}, result.sent)
        self.assertIn({'type': 'rtc-connected', 'generation': 'gen'}, result.sent)
        self.assertIn('peer-closed', result.events)
        result.writer.close.assert_called_once()
        result.monitor.close.assert_called_once()
        args, kwargs = result.connect.call_args
        self.assertNotIn(TOKEN, args[0])
        self.assertEqual(kwargs['additional_headers'], {'Authorization': 'Bearer ' + TOKEN})

    def test_auth_rejection_is_explicit_and_does_not_create_peer(self):
        result = self.exercise([], close_code=4401)
        self.assertIsInstance(result.error, ground.RoomRejected)
        self.assertEqual(result.error.code, 4401)
        self.assertIn('authentication', str(result.error).lower())
        self.assertNotIn('peer-created', result.events)
        result.writer.close.assert_called_once()

    def test_invalid_bootstrap_never_creates_peer(self):
        result = self.exercise([{'type': 'room-ready', 'generation': 'gen'},
                                {'type': 'session-ready', 'generation': 'old'}])
        self.assertIsInstance(result.error, ValueError)
        self.assertNotIn('peer-created', result.events)

    def test_lost_owner_ack_times_out_waiting_room_and_cleans_up(self):
        result = self.exercise([{'type': 'room-ready', 'generation': 'gen'}],
                               close_code=None, clock_step=5000)
        self.assertIsInstance(result.error, TimeoutError)
        self.assertIn('heartbeat', str(result.error))
        self.assertNotIn('peer-created', result.events)
        result.writer.close.assert_called_once()

    def test_server_deadline_ends_active_session_before_credentials_expire(self):
        ready = {'type': 'session-ready', 'generation': 'gen',
                 'iceServers': [{'urls': 'stun:stun.cloudflare.com:3478'}],
                 'expiresAt': ground.time.time() * 1000 + 2000, 'turnStatus': 'unavailable'}
        result = self.exercise([{'type': 'room-ready', 'generation': 'gen'}, ready,
                                {'type': 'peer-joined'}], close_code=None, clock_step=250)
        self.assertIsNone(result.error)
        self.assertIn('offer', result.events)
        self.assertIn('peer-closed', result.events)
        result.writer.close.assert_called_once()


if __name__ == '__main__': unittest.main()
