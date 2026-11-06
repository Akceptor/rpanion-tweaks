import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from fly_ground_settings import ConfigStore, SettingsServer, Conflict

BASE = '# comment\nFLY_ROOM_CODE=CRAWLER-1234-5678-9012\nFLY_SITE_URL="https://example.com"\nEXTRA="keep me"\n'

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

class HttpTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.restarts = []
        self.server = SettingsServer(('127.0.0.1', 0), self.store, restart=lambda: self.restarts.append(True))
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
        self.assertIn('readonly', body)
        conn.close()

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

if __name__ == '__main__': unittest.main()
