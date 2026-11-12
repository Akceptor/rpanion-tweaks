import base64
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from fly_ground_auth import host_token_from_env, validate_host_token


TOKEN = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('=')


class HostTokenTest(unittest.TestCase):
    def test_requires_canonical_32_byte_base64url(self):
        self.assertEqual(validate_host_token(TOKEN), TOKEN)
        # A nonzero unused final-bit pattern decodes to the same bytes, but is not canonical.
        noncanonical = TOKEN[:-1] + chr(ord(TOKEN[-1]) + 1)
        for invalid in ('', None, 'short', TOKEN + '=', TOKEN + '\n', noncanonical,
                        base64.urlsafe_b64encode(b'a' * 31).decode().rstrip('=')):
            with self.subTest(invalid=type(invalid).__name__):
                with self.assertRaises(ValueError) as cm:
                    validate_host_token(invalid)
                if isinstance(invalid, str) and invalid:
                    self.assertNotIn(invalid, str(cm.exception))

    def test_loads_secret_separately_from_public_settings(self):
        self.assertEqual(host_token_from_env({'FLY_HOST_TOKEN': TOKEN}), TOKEN)
        with self.assertRaisesRegex(ValueError, 'FLY_HOST_TOKEN'):
            host_token_from_env({})


if __name__ == '__main__': unittest.main()
