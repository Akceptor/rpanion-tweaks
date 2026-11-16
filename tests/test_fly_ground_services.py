import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))


class ServiceTest(unittest.TestCase):
    def manager(self, states=None, router=0, video=None):
        from fly_ground_services import ServiceManager
        self.calls = []
        states = states or {}
        def run(args, **kwargs):
            self.calls.append((args, kwargs))
            if args[0] == 'pgrep':
                return SimpleNamespace(returncode=router, stdout='')
            if args[1] == 'show':
                return SimpleNamespace(returncode=0, stdout=states.get(args[-1], 'active') + '\n')
            return SimpleNamespace(returncode=0, stdout='')
        return ServiceManager(run=run, video_state=lambda: video)

    def test_fixed_status_order_health_and_actions(self):
        manager = self.manager({'lima.service': 'activating', 'agps.service': 'deactivating'}, video=object())
        rows = manager.read()['services']
        self.assertEqual([row['id'] for row in rows], ['rpanion', 'video', 'bridge', 'router', 'lima', 'agps'])
        self.assertEqual([row['state'] for row in rows], ['up', 'up', 'up', 'up', 'starting', 'stopping'])
        self.assertEqual(rows[1]['actions'], [])
        self.assertEqual(rows[3]['actions'], [])
        self.assertIn('not proof', rows[1]['detail'])
        self.assertIn(['pgrep', '-x', 'mavlink-routerd'], [call[0] for call in self.calls])
        self.assertTrue(all(call[1]['timeout'] <= 5 for call in self.calls))

    def test_down_unknown_and_router_errors(self):
        manager = self.manager({'rpanion-server.service': 'inactive', 'fly-ground.service': 'failed', 'lima.service': 'surprise'})
        rows = manager.read()['services']
        self.assertEqual([row['state'] for row in rows[:5]], ['down', 'down', 'down', 'down', 'unknown'])
        self.assertFalse(any(call[0][0] == 'pgrep' for call in self.calls))
        for code, expected in [(1, 'down'), (2, 'unknown')]:
            self.assertEqual(self.manager(router=code).read()['services'][3]['state'], expected)
        manager.run = lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired('secret', 1))
        self.assertTrue(all(row['state'] == 'unknown' for row in manager.read()['services'] if row['id'] != 'video'))

    def test_allowlist_serialization_and_restart_dependency_recovery(self):
        from fly_ground_services import Busy
        manager = self.manager()
        for service, action in [('video','restart'), ('router','start'), ('bridge; reboot','start'), ('bridge','reload'), ([], 'stop')]:
            with self.assertRaises(ValueError): manager.operate(service, action)
        self.assertEqual(self.calls, [])
        manager.operation_lock.acquire()
        try:
            with self.assertRaises(Busy): manager.operate('bridge', 'start')
        finally: manager.operation_lock.release()
        manager.operate('rpanion', 'restart')
        commands = [call[0] for call in self.calls if call[0][0] == 'systemctl' and call[0][1] != 'show']
        self.assertEqual(commands, [['systemctl','restart','rpanion-server.service'], ['systemctl','start','fly-ground.service']])
        self.assertTrue(all(call[1]['timeout'] <= 30 for call in self.calls))

    def test_dependency_restart_restores_running_bridge_only(self):
        for service in ('rpanion', 'lima'):
            for state in ('active', 'inactive'):
                manager = self.manager({'fly-ground.service': state})
                manager.operate(service, 'restart')
                self.assertEqual(['systemctl', 'start', 'fly-ground.service'] in [call[0] for call in self.calls], state == 'active')

    def test_action_timeout_releases_busy_lock(self):
        manager = self.manager()
        def fail(*args, **kwargs):
            raise subprocess.TimeoutExpired('sensitive subprocess detail', 20)
        manager.run = fail
        with self.assertRaises(subprocess.TimeoutExpired): manager.operate('bridge', 'start')
        self.assertTrue(manager.operation_lock.acquire(False))
        manager.operation_lock.release()

    def test_nonzero_operation_is_error_and_lock_is_released(self):
        manager = self.manager()
        def failed(*a, **k): return SimpleNamespace(returncode=1, stdout='SECRET')
        manager.run = failed
        with self.assertRaises(subprocess.SubprocessError): manager.operate('bridge', 'stop')
        self.assertTrue(manager.operation_lock.acquire(False))
        manager.operation_lock.release()
