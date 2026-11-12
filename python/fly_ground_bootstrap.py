"""Validation and liveness for an owner-authorized signaling connection."""

import math
import re
from urllib.parse import urlsplit


GENERATION_RE = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
MAX_CREDENTIAL_MS = 7_200_000
HEARTBEAT_INTERVAL_MS = 20_000
HOST_LEASE_MS = 60_000


def valid_generation(value):
    return isinstance(value, str) and GENERATION_RE.fullmatch(value) is not None


def parse_session_ready(message, generation, epoch_ms):
    """Return normalized ICE data without accepting unauthenticated HTTP fallback."""
    error = 'Invalid admitted session configuration'
    if (not isinstance(message, dict) or message.get('type') != 'session-ready'
            or not valid_generation(generation) or message.get('generation') != generation):
        raise ValueError(error)
    expires = message.get('expiresAt')
    status = message.get('turnStatus')
    if (type(expires) not in (int, float) or not math.isfinite(expires)
            or not epoch_ms < expires <= epoch_ms + MAX_CREDENTIAL_MS
            or status not in ('available', 'unavailable')):
        raise ValueError(error)
    servers = message.get('iceServers')
    if not isinstance(servers, list) or not 1 <= len(servers) <= 16:
        raise ValueError(error)
    normalized, has_turn = [], False
    for server in servers:
        if not isinstance(server, dict):
            raise ValueError(error)
        urls = server.get('urls')
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list) or not 1 <= len(urls) <= 16:
            raise ValueError(error)
        relay = False
        for url in urls:
            if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 for c in url):
                raise ValueError(error)
            scheme, separator, rest = url.partition(':')
            if scheme not in ('stun', 'stuns', 'turn', 'turns') or not separator:
                raise ValueError(error)
            try:
                parsed = urlsplit(scheme + '://' + rest)
                if (not parsed.hostname or parsed.username or parsed.password
                        or parsed.path or parsed.fragment):
                    raise ValueError(error)
                parsed.port  # Validate malformed/out-of-range ports before passing to GStreamer.
            except ValueError:
                raise ValueError(error) from None
            relay = relay or scheme in ('turn', 'turns')
        item = {'urls': list(urls)}
        if relay:
            for name in ('username', 'credential'):
                value = server.get(name)
                if (not isinstance(value, str) or not value or len(value) > 2048
                        or any(ord(c) < 32 for c in value)):
                    raise ValueError(error)
                item[name] = value
            has_turn = True
        normalized.append(item)
    if has_turn != (status == 'available'):
        raise ValueError(error)
    return {'iceServers': normalized, 'expiresAt': expires, 'turnStatus': status}


class HostLease:
    """Use monotonic time for local loss detection, independently of wall-clock changes."""

    def __init__(self, now):
        self.generation = None
        self.admission_deadline = now + 15_000
        self.last_ack = None
        self.last_heartbeat = None

    def admit(self, generation, now):
        if self.generation is not None or not valid_generation(generation):
            raise ValueError('Invalid host admission')
        self.generation = generation
        self.last_ack = now

    def ack(self, generation, now):
        if self.generation is None or generation != self.generation:
            return False
        self.last_ack = now
        return True

    def heartbeat(self, now):
        if self.generation is None:
            return None
        if self.last_heartbeat is None or now - self.last_heartbeat >= HEARTBEAT_INTERVAL_MS:
            self.last_heartbeat = now
            return {'type': 'host-heartbeat', 'generation': self.generation}
        return None

    def check(self, now):
        if self.generation is None:
            if now >= self.admission_deadline:
                raise TimeoutError('Host admission timed out')
        elif now - self.last_ack >= HOST_LEASE_MS:
            raise TimeoutError('Signaling heartbeat timed out')
