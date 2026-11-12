from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from fly_ground_bootstrap import HostLease


class HostLeaseTest(unittest.TestCase):
    def test_waits_for_admission_then_sends_generation_bound_heartbeats(self):
        lease = HostLease(1000)
        self.assertIsNone(lease.heartbeat(1000))
        lease.admit('gen-1', 2000)
        self.assertEqual(lease.heartbeat(2000), {'type': 'host-heartbeat', 'generation': 'gen-1'})
        self.assertIsNone(lease.heartbeat(21_999))
        self.assertEqual(lease.heartbeat(22_000)['generation'], 'gen-1')

    def test_wrong_generation_ack_cannot_keep_host_session_alive(self):
        lease = HostLease(1000)
        lease.admit('gen-1', 2000)
        self.assertFalse(lease.ack('old', 50_000))
        with self.assertRaisesRegex(TimeoutError, 'heartbeat'):
            lease.check(62_000)

    def test_valid_ack_extends_local_liveness_without_clock_dependence(self):
        lease = HostLease(1000)
        lease.admit('gen-1', 2000)
        self.assertTrue(lease.ack('gen-1', 40_000))
        lease.check(99_999)
        with self.assertRaises(TimeoutError): lease.check(100_000)

    def test_missing_or_duplicate_admission_fails_closed(self):
        lease = HostLease(1000)
        with self.assertRaisesRegex(TimeoutError, 'admission'): lease.check(16_000)
        for invalid in ('', None, 'wrong\n'):
            with self.assertRaises(ValueError): HostLease(0).admit(invalid, 0)
        lease.admit('gen', 2000)
        with self.assertRaises(ValueError): lease.admit('new-gen', 3000)


if __name__ == '__main__': unittest.main()
