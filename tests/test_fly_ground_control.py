import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from fly_ground_control import Arbiter, decode_packet, crsf_to_pwm


def packet(seq=1, ts=1000, channels=None):
    channels = channels or [992] * 16
    return bytes([1, 0]) + seq.to_bytes(4, "little") + ts.to_bytes(4, "little") + b"".join(
        x.to_bytes(2, "little") for x in channels)


class DecodeTest(unittest.TestCase):
    def test_round_trip_and_invalid_frames(self):
        self.assertEqual(decode_packet(packet()).seq, 1)
        for bad in (packet()[:-1], bytes([2]) + packet()[1:], packet(channels=[171] * 16)):
            self.assertIsNone(decode_packet(bad))

    def test_pwm_endpoints(self):
        self.assertEqual(crsf_to_pwm(172), 1000)
        self.assertEqual(crsf_to_pwm(1811), 2000)
        self.assertAlmostEqual(crsf_to_pwm(992), 1500, delta=1)


class ArbiterTest(unittest.TestCase):
    def setUp(self):
        self.a = Arbiter(channels=(1, 3), throttle_channel=3)
        self.a.peer_connected = True

    def feed(self, seq=1, now=1000, ts=1000, channels=None):
        return self.a.accept_packet(packet(seq, ts, channels), now, offset_ms=0)

    def test_resume_requires_fresh_neutral_input(self):
        self.assertFalse(self.a.resume(1000))
        self.assertTrue(self.feed())
        self.assertTrue(self.a.resume(1000))
        self.assertEqual(self.a.owner, "browser")
        self.assertFalse(self.a.resume(1300))
        self.assertEqual(self.a.owner, "idle")

    def test_rejects_old_and_out_of_order_packets(self):
        self.assertTrue(self.feed())
        self.assertFalse(self.feed(seq=1, now=1020, ts=1020))
        self.assertFalse(self.feed(seq=2, now=1201, ts=1000))
        self.assertFalse(self.feed(seq=2, now=1000, ts=1300))

    def test_output_overrides_only_selected_channels(self):
        self.assertTrue(self.feed())
        self.assertTrue(self.a.resume(1000))
        ch = [992] * 16
        ch[0], ch[2] = 1811, 172
        self.assertTrue(self.feed(seq=2, now=1020, ts=1020, channels=ch))
        out = self.a.output(1020)
        self.assertEqual(len(out), 18)
        self.assertEqual(out[0], 2000)
        self.assertEqual(out[2], 1000)
        self.assertTrue(all(x == 65535 for i, x in enumerate(out) if i not in (0, 2)))

    def test_planner_takes_over_and_requires_new_resume(self):
        self.assertTrue(self.feed())
        self.assertTrue(self.a.resume(1000))
        self.a.planner_seen(1001)
        self.assertEqual(self.a.owner_at(1001), "mission-planner")
        release = self.a.take_release()
        self.assertEqual(release[0], 0)
        self.assertEqual(release[2], 0)
        self.assertIsNone(self.a.take_release())
        self.assertIsNone(self.a.output(1001))
        self.assertEqual(self.a.owner_at(4000), "mission-planner")
        self.assertEqual(self.a.owner_at(4002), "idle")
        self.assertFalse(self.a.resume(4002))
        self.assertTrue(self.feed(seq=2, now=4002, ts=4002))
        self.assertTrue(self.a.resume(4002))

    def test_high_throttle_and_disconnect_block_resume(self):
        ch = [992] * 16
        ch[2] = 1300
        self.assertTrue(self.feed(channels=ch))
        self.assertFalse(self.a.resume(1000))
        self.a.peer_connected = False
        self.assertTrue(self.feed(seq=2, now=1020, ts=1020))
        self.assertFalse(self.a.resume(1020))

    def test_watchdog_and_disconnect_release_previous_override_once(self):
        self.assertTrue(self.feed())
        self.assertTrue(self.a.resume(1000))
        self.assertEqual(self.a.owner_at(1300), "idle")
        self.assertEqual(self.a.take_release()[0], 0)
        self.assertIsNone(self.a.take_release())
        self.assertTrue(self.feed(seq=2, now=1400, ts=1400))
        self.assertTrue(self.a.resume(1400))
        self.a.disconnect()
        self.assertEqual(self.a.take_release()[2], 0)


if __name__ == "__main__":
    unittest.main()
