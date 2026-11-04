import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from fly_ground import REJECT_DELAY, RoomRejected, next_delay


class RetryTest(unittest.TestCase):
    def test_ordinary_errors_back_off_exponentially_to_30s(self):
        self.assertEqual(next_delay(1, RuntimeError()), (1, 2))
        self.assertEqual(next_delay(16, RuntimeError()), (16, 30))
        self.assertEqual(next_delay(30, RuntimeError()), (30, 30))

    def test_room_rejection_waits_long_then_resets(self):
        exc = RoomRejected(4001)
        self.assertEqual(exc.code, 4001)
        self.assertIn("4001", str(exc))
        self.assertEqual(next_delay(1, exc), (REJECT_DELAY, 1))
        self.assertGreaterEqual(REJECT_DELAY, 30)


if __name__ == "__main__":
    unittest.main()
