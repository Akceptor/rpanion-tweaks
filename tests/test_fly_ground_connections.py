import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from fly_ground import mavlink_connections


class ConnectionTest(unittest.TestCase):
    settings = SimpleNamespace(router_url="udpout:127.0.0.1:14552",
                               monitor_url="tcp:127.0.0.1:5760", gcs_sysid=255, gcs_compid=191)

    def test_lima_writer_and_raw_priority_monitor_use_separate_routes(self):
        writer, monitor = Mock(), Mock()
        with patch("pymavlink.mavutil.mavlink_connection", side_effect=[writer, monitor]) as connect:
            self.assertEqual(mavlink_connections(self.settings), (writer, monitor))
        self.assertEqual([call.args[0] for call in connect.call_args_list],
                         [self.settings.router_url, self.settings.monitor_url])

    def test_monitor_failure_closes_the_writer(self):
        writer = Mock()
        with patch("pymavlink.mavutil.mavlink_connection", side_effect=[writer, OSError("offline")]):
            with self.assertRaises(OSError):
                mavlink_connections(self.settings)
        writer.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
