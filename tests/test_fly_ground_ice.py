"""ICE configuration must arrive from the admitted signaling connection."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from fly_ground_bootstrap import parse_session_ready

STUN = {'urls': 'stun:stun.cloudflare.com:3478'}
TURN = {'urls': 'turn:turn.cloudflare.com:3478?transport=udp', 'username': 'u', 'credential': 'p'}
NOW = 1_800_000_000_000


def bootstrap():
    return dict(type='session-ready', generation='generation-1', iceServers=[STUN, TURN],
                expiresAt=NOW + 7_140_000, turnStatus='available')


class IceServerTest(unittest.TestCase):
    def test_validates_and_normalizes_admitted_ice_servers(self):
        value = parse_session_ready(bootstrap(), 'generation-1', NOW)
        self.assertEqual(value['iceServers'][1]['urls'], [TURN['urls']])
        self.assertEqual(value['expiresAt'], NOW + 7_140_000)
        self.assertEqual(value['turnStatus'], 'available')

    def test_authorized_stun_only_fallback_is_explicit(self):
        value = bootstrap()
        value.update(iceServers=[STUN], turnStatus='unavailable')
        self.assertEqual(parse_session_ready(value, 'generation-1', NOW)['iceServers'],
                         [{'urls': [STUN['urls']]}])

    def test_rejects_wrong_generation_type_or_deadline(self):
        for update in ({'generation': 'old'}, {'type': 'offer'}, {'expiresAt': NOW},
                       {'expiresAt': NOW + 7_200_001}, {'expiresAt': float('inf')},
                       {'expiresAt': True}, {'turnStatus': 'unknown'}):
            value = bootstrap()
            value.update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                parse_session_ready(value, 'generation-1', NOW)

    def test_rejects_invalid_or_uncredentialed_relay_configuration(self):
        for servers in ([], 'bad', [{'urls': 'https://evil.example'}],
                        [dict(TURN, credential='')], [dict(TURN, username=1)],
                        [dict(TURN, urls='turn:host:bad')], [dict(TURN, urls='turn:user:pass@host')],
                        [STUN], [dict(TURN, urls=[])], [None]):
            value = bootstrap()
            value['iceServers'] = copy.deepcopy(servers)
            with self.subTest(servers=servers), self.assertRaises(ValueError):
                parse_session_ready(value, 'generation-1', NOW)


if __name__ == '__main__': unittest.main()
