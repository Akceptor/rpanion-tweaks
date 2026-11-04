import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from fly_ground import ice_servers


STUN = [{"urls": "stun:stun.cloudflare.com:3478"}]


class IceServerTest(unittest.TestCase):
    def test_uses_turn_response_when_available(self):
        servers = STUN + [{"urls": ["turn:turn.cloudflare.com:3478?transport=udp"],
                           "username": "user", "credential": "pass"}]
        with mock.patch("fly_ground.urlopen", return_value=io.BytesIO(json.dumps({"iceServers": servers}).encode())):
            self.assertEqual(ice_servers("https://site.example"), servers)

    def test_falls_back_to_stun_when_turn_endpoint_is_missing(self):
        error = HTTPError("https://site.example/turn", 404, "Not Found", {}, None)
        with mock.patch("fly_ground.urlopen", side_effect=error):
            with self.assertLogs("fly-ground", level="WARNING") as logs:
                self.assertEqual(ice_servers("https://site.example"), STUN)
        error.close()
        self.assertIn("using STUN only", logs.output[0])

    def test_falls_back_to_stun_when_turn_is_unavailable_or_invalid(self):
        with mock.patch("fly_ground.urlopen", side_effect=URLError("network down")):
            with self.assertLogs("fly-ground", level="WARNING"):
                self.assertEqual(ice_servers("https://site.example"), STUN)
        with mock.patch("fly_ground.urlopen", return_value=io.BytesIO(b"{}")):
            with self.assertLogs("fly-ground", level="WARNING"):
                self.assertEqual(ice_servers("https://site.example"), STUN)


if __name__ == "__main__":
    unittest.main()
