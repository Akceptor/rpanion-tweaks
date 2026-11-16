import http.client
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from fly_ground_settings import ConfigStore, SettingsServer, Conflict
from video_output import VideoOutputStore
from types import SimpleNamespace

BASE = '# comment\nFLY_ROOM_CODE=CRAWLER-1234-5678-9012\nFLY_SITE_URL="https://example.com"\nEXTRA="keep me"\n'
HOST_TOKEN = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('=')
NEW_HOST_TOKEN = base64.urlsafe_b64encode(bytes(reversed(range(32)))).decode().rstrip('=')

class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'env'
        self.path.write_text(BASE)
        self.store = ConfigStore(self.path)

    def test_defaults_validation_and_atomic_permissions(self):
        old = self.store.read()
        self.assertEqual(old['settings']['FLY_THROTTLE_CHANNEL'], '3')
        values = dict(old['settings'], FLY_CHANNELS='1,2,3,4,5,8', FLY_THROTTLE_CHANNEL='2')
        new = self.store.save(values, old['revision'])
        self.assertEqual(new['settings']['FLY_THROTTLE_CHANNEL'], '2')
        self.assertIn('EXTRA="keep me"', self.path.read_text())
        self.assertIn('# comment', self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(Conflict): self.store.save(values, old['revision'])

    def test_invalid_changes_leave_file_intact(self):
        old = self.store.read()
        for bad in [{'OTHER': 'x'}, dict(old['settings'], FLY_ROOM_CODE='bad'), dict(old['settings'], FLY_SITE_URL='https://x\nEVIL=1')]:
            with self.assertRaises(ValueError): self.store.save(bad, old['revision'])
            self.assertEqual(self.path.read_text(), BASE)

    def test_regenerate_only_changes_code(self):
        old = self.store.read()
        new = self.store.recreate(old['revision'])
        self.assertNotEqual(old['settings']['FLY_ROOM_CODE'], new['settings']['FLY_ROOM_CODE'])
        self.assertEqual(new['settings']['FLY_SITE_URL'], old['settings']['FLY_SITE_URL'])
        self.assertEqual(self.path.read_text().splitlines()[2:], BASE.splitlines()[2:])

    def test_symlink_survives_atomic_save(self):
        link = self.path.parent / 'link'
        link.symlink_to(self.path)
        store = ConfigStore(link)
        old = store.read()
        store.recreate(old['revision'])
        self.assertTrue(link.is_symlink())
        self.assertNotEqual(old['revision'], store.read()['revision'])

    def test_duplicate_and_unsupported_managed_syntax(self):
        self.path.write_text(BASE + 'FLY_ROOM_CODE=CRAWLER-AAAA-BBBB-CCCC\n')
        old = self.store.read()
        new = self.store.recreate(old['revision'])
        self.assertEqual(self.path.read_text().count('FLY_ROOM_CODE='), 1)
        self.path.write_text(BASE.replace('https://example.com', 'https://example.com\\'))
        with self.assertRaises(ValueError): self.store.read()

    def test_host_token_is_write_only_and_preserved_by_normal_edits(self):
        old = self.store.read()
        self.assertFalse(old['host_token_configured'])
        saved = self.store.set_host_token(HOST_TOKEN, old['revision'])
        self.assertTrue(saved['host_token_configured'])
        self.assertNotIn(HOST_TOKEN, json.dumps(saved))
        self.assertNotIn('FLY_HOST_TOKEN', saved['settings'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        changed = self.store.save(saved['settings'], saved['revision'])
        recreated = self.store.recreate(changed['revision'])
        self.assertIn('FLY_HOST_TOKEN="' + HOST_TOKEN + '"', self.path.read_text())
        self.assertTrue(recreated['host_token_configured'])

    def test_host_token_replacement_removes_duplicates_and_blank_preserves(self):
        self.path.write_text(BASE + 'FLY_HOST_TOKEN=' + HOST_TOKEN + '\nFLY_HOST_TOKEN=' + HOST_TOKEN + '\n')
        old = self.store.read()
        replaced = self.store.set_host_token(NEW_HOST_TOKEN, old['revision'])
        self.assertEqual(self.path.read_text().count('FLY_HOST_TOKEN='), 1)
        self.assertNotIn(HOST_TOKEN, self.path.read_text())
        before = self.path.read_bytes()
        unchanged = self.store.set_host_token('', replaced['revision'])
        self.assertEqual(unchanged['revision'], replaced['revision'])
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaises(Conflict): self.store.set_host_token('', old['revision'])
        for invalid in (None, 'secret', HOST_TOKEN + '\n'):
            with self.assertRaises(ValueError): self.store.set_host_token(invalid, replaced['revision'])
        self.assertEqual(before, self.path.read_bytes())

    def test_existing_bad_token_does_not_prevent_owner_replacing_it(self):
        self.path.write_text(BASE + 'FLY_HOST_TOKEN="wrong"\n')
        old = self.store.read()
        self.assertFalse(old['host_token_configured'])
        self.assertTrue(self.store.set_host_token(HOST_TOKEN, old['revision'])['host_token_configured'])

class HttpTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.restarts = []
        self.server = SettingsServer(('127.0.0.1', 0), self.store, restart=lambda: self.restarts.append(True), output_store=VideoOutputStore(Path(self.tmp.name) / 'output.json'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.finish)
    def finish(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.request(method, path, None if body is None else json.dumps(body), headers or {})
        response = conn.getresponse(); payload = json.loads(response.read()); conn.close()
        return response.status, payload
    def test_serves_settings_page_without_caching(self):
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.request('GET', '/')
        response = conn.getresponse()
        body = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertIn('id="recreate"', body)
        self.assertIn('id="servicesList"', body)
        self.assertIn("api('/api/services'", body)
        self.assertIn('Restarting Rpanion disconnects video and telemetry', body)
        self.assertIn('readonly', body)
        self.assertIn('id="hostToken" type="password"', body)
        self.assertIn('id="saveHostToken"', body)
        self.assertIn("api('/api/host-token'", body)
        conn.close()

    def test_service_api_is_independent_allowlisted_and_protected(self):
        from fly_ground_services import ServiceManager
        self.server.services = ServiceManager(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout='inactive'), video_state=lambda: None)
        status, state = self.request('GET', '/api/services')
        self.assertEqual(status, 200)
        self.assertEqual(len(state['services']), 6)
        headers = {'Content-Type': 'application/json', 'X-CSRF-Token': state['token']}
        body = {'service': 'bridge', 'action': 'start'}
        self.assertEqual(self.request('POST', '/api/services', body)[0], 403)
        self.assertEqual(self.request('POST', '/api/services', body, dict(headers, Origin='http://evil.example'))[0], 403)
        for bad in ({'service': 'bridge'}, dict(body, extra=True), dict(body, service='video'), dict(body, action='reload')):
            self.assertEqual(self.request('POST', '/api/services', bad, headers)[0], 400)
        self.assertEqual(self.request('POST', '/api/services', body, headers), (200, state))
        self.server.services.operation_lock.acquire()
        try:
            self.assertEqual(self.request('POST', '/api/services', body, headers)[0], 409)
            self.assertEqual(self.request('POST', '/api/restart', {}, headers)[0], 409)
        finally: self.server.services.operation_lock.release()
        # Services remain available even when the settings file is missing.
        self.path.unlink()
        self.assertEqual(self.request('GET', '/api/services')[0], 200)

    def test_services_cors_is_limited_to_saved_site_and_endpoint(self):
        from fly_ground_services import ServiceManager
        self.server.services = ServiceManager(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout='active'), video_state=lambda: None)
        def call(method, path, headers, body=None):
            conn = http.client.HTTPConnection(*self.server.server_address)
            conn.request(method, path, None if body is None else json.dumps(body), headers)
            response = conn.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
            conn.close()
            return result
        allowed = {'Origin': 'https://example.com', 'Sec-Fetch-Site': 'cross-site'}
        status, headers, body = call('GET', '/api/services', allowed)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Access-Control-Allow-Origin'), allowed['Origin'])
        self.assertEqual(headers.get('Vary'), 'Origin')
        token = json.loads(body)['token']
        preflight = dict(allowed, **{'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type,x-csrf-token'})
        status, headers, _ = call('OPTIONS', '/api/services', preflight)
        self.assertEqual(status, 204)
        self.assertEqual(headers.get('Access-Control-Allow-Methods'), 'GET, POST')
        self.assertIn('X-CSRF-Token', headers.get('Access-Control-Allow-Headers', ''))
        for bad in (dict(preflight, Origin='https://evil.example'), dict(preflight, **{'Access-Control-Request-Method': 'DELETE'}), dict(preflight, **{'Access-Control-Request-Headers': 'authorization'})):
            status, headers, _ = call('OPTIONS', '/api/services', bad)
            self.assertEqual(status, 403)
            self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual(call('OPTIONS', '/api/settings', preflight)[0], 403)
        status, headers, body = call('GET', '/api/services', dict(allowed, Origin='https://evil.example'))
        self.assertEqual(status, 403)
        self.assertNotIn('token', json.loads(body))
        post = dict(allowed, **{'Content-Type': 'application/json', 'X-CSRF-Token': token})
        self.assertEqual(call('POST', '/api/services', post, {'service':'bridge','action':'start'})[0], 200)
        self.assertEqual(call('POST', '/api/services', allowed, {'service':'bridge','action':'start'})[0], 403)
        self.assertEqual(call('POST', '/api/restart', post, {})[0], 403)
        # Invalid unrelated configuration must not prevent local recovery controls.
        self.path.write_text('FLY_ROOM_CODE=bad\nFLY_SITE_URL=broken\n')
        self.assertEqual(call('GET', '/api/services', {})[0], 200)
        self.assertEqual(call('GET', '/api/services', allowed)[0], 403)

    def test_services_failure_is_safe_and_refreshable(self):
        import subprocess
        from unittest.mock import Mock
        self.server.services = Mock()
        self.server.services.operate.side_effect = subprocess.TimeoutExpired('SECRET', 20)
        headers = {'Content-Type':'application/json', 'X-CSRF-Token':self.server.token}
        status, reply = self.request('POST', '/api/services', {'service':'bridge','action':'restart'}, headers)
        self.assertEqual(status, 500)
        self.assertIn('refresh status', reply['error'])
        self.assertNotIn('SECRET', json.dumps(reply))

    def test_api_csrf_revision_and_restart(self):
        status, data = self.request('GET', '/api/settings')
        self.assertEqual(status, 200)
        headers = {'Content-Type': 'application/json', 'X-CSRF-Token': data['token']}
        self.assertEqual(self.request('POST','/api/recreate',{'revision':data['revision']})[0],403)
        for bad in [{'Origin':'http://evil.example'}, {'Sec-Fetch-Site':'cross-site'}]:
            self.assertEqual(self.request('POST','/api/recreate',{'revision':data['revision']},dict(headers,**bad))[0],403)
        self.assertEqual(self.request('POST','/api/recreate',{'revision':data['revision']},headers)[0],200)
        self.assertEqual(self.request('POST','/api/settings',{'settings':data['settings'],'revision':data['revision']},headers)[0],409)
        self.assertFalse(self.restarts)
        self.assertEqual(self.request('POST','/api/restart',{},headers)[0],200)
        self.assertEqual(self.restarts,[True])

    def test_video_output_is_separate_revisioned_and_protected(self):
        _, settings = self.request('GET', '/api/settings')
        with patch('fly_ground_settings.read_state', return_value=None):
            status, output = self.request('GET', '/api/video-output')
        self.assertEqual(status, 200)
        self.assertEqual(output['mode'], 'both')
        self.assertFalse(output['running'])
        self.assertIsNone(output['applied_mode'])
        self.assertIsNone(output['pending'])
        headers = {'Content-Type': 'application/json', 'X-CSRF-Token': settings['token']}
        body = {'mode': 'rpanion', 'revision': output['revision']}
        self.assertEqual(self.request('POST', '/api/video-output', body)[0], 403)
        self.assertEqual(self.request('POST', '/api/video-output', body, dict(headers, Origin='http://evil.example'))[0], 403)
        for bad in ({'mode': 'webrtc'}, dict(body, extra=1), dict(body, mode='RTP')):
            self.assertEqual(self.request('POST', '/api/video-output', bad, headers)[0], 400)
        with patch('fly_ground_settings.read_state', return_value=SimpleNamespace(transport='RTP', output_mode='both')):
            status, saved = self.request('POST', '/api/video-output', body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(saved['transport'], 'RTP')
        self.assertEqual(saved['applied_mode'], 'both')
        self.assertTrue(saved['pending'])
        self.assertEqual(self.request('POST', '/api/video-output', body, headers)[0], 409)
        self.assertEqual(self.request('GET', '/api/settings')[1], settings)
        self.assertFalse(self.restarts)

    def test_video_output_reports_fresh_applied_state_and_bundled_ui(self):
        with patch('fly_ground_settings.read_state', return_value=SimpleNamespace(transport='RTSP', output_mode='both')):
            status, data = self.request('GET', '/api/video-output')
        self.assertEqual(status, 200)
        self.assertEqual(data['transport'], 'RTSP')
        self.assertEqual(data['applied_mode'], 'both')
        self.assertFalse(data['pending'])
        page = Path(__file__).resolve().parents[1] / 'python' / 'fly_ground_settings.html'
        html = page.read_text()
        for text in ('id="videoOutput"', 'id="applyVideoOutput"', '/api/video-output', 'Refresh connection', 'RTP/RTSP only'):
            self.assertIn(text, html)

    def test_host_token_api_checks_csrf_shape_revision_and_never_echoes_secret(self):
        _, old = self.request('GET', '/api/settings')
        body = {'token': HOST_TOKEN, 'revision': old['revision']}
        headers = {'Content-Type': 'application/json', 'X-CSRF-Token': old['token']}
        self.assertEqual(self.request('POST', '/api/host-token', body)[0], 403)
        self.assertEqual(self.request('POST', '/api/host-token', body, dict(headers, Origin='http://evil.example'))[0], 403)
        for bad in ({'token': HOST_TOKEN}, dict(body, extra='bad'), dict(body, token='bad')):
            status, reply = self.request('POST', '/api/host-token', bad, headers)
            self.assertEqual(status, 400)
            self.assertNotIn(HOST_TOKEN, json.dumps(reply))
        status, saved = self.request('POST', '/api/host-token', body, headers)
        self.assertEqual(status, 200)
        self.assertTrue(saved['host_token_configured'])
        self.assertNotIn(HOST_TOKEN, json.dumps(saved))
        self.assertEqual(self.request('POST', '/api/host-token', body, headers)[0], 409)
        _, state = self.request('GET', '/api/settings')
        self.assertTrue(state['host_token_configured'])
        self.assertNotIn(HOST_TOKEN, json.dumps(state))

if __name__ == '__main__': unittest.main()
