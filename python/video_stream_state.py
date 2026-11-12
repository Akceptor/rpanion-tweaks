"""Local video producer contract; no GStreamer dependency.

Only the producer writes state. Consumers compare VideoSource values, which exclude
heartbeat time, to distinguish restarts/input changes from routine refreshes.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
from dataclasses import asdict, dataclass, replace
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit
import uuid


STATE_FILE = '/run/rpanion-video/state.json'
MAX_AGE = 3.0


def port_number(value):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)):
        raise ValueError('video port must be an integer between 1 and 65535')
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError('video port must be between 1 and 65535')
    return port


def parse_destination(value):
    parsed = urlsplit('udp://' + value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment or not parsed.port):
        raise ValueError('RTP destination must be host:port (IPv6: [address]:port)')
    host = parsed.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host):
            raise ValueError('invalid RTP destination host') from None
    return host, port_number(parsed.port)


def rtp_destinations(remote, local_port):
    destinations = [parse_destination(remote)]
    if local_port is not None:
        local = ('127.0.0.1', port_number(local_port))
        if local not in destinations:
            destinations.append(local)
    return destinations


def configure_rtp_sink(sink, destinations):
    """Configure a multiudpsink without interpreting addresses as launch syntax."""
    for host, port in destinations:
        sink.emit('add', host, port)


def mount_name(device):
    # Preserve Rpanion's existing naming, including RTSP camera credentials removal.
    name = device
    if device.startswith('rtsp://'):
        name = device.split('@', 1)[1] if '@' in device else device.replace('rtsp://', '')
    return ''.join(filter(str.isalnum, name))


def effective_codec(format_str, compression):
    return {'video/x-h264': 'H264', 'video/x-h265': 'H265'}.get(format_str, compression)


@dataclass(frozen=True)
class VideoSource:
    instance: str
    transport: str
    codec: str
    rtp_host: str | None = None
    rtp_port: int | None = None
    rtsp_url: str | None = None


def validate_source(source):
    if not isinstance(source.instance, str) or not 1 <= len(source.instance) <= 128:
        raise ValueError('invalid video instance')
    if source.codec not in ('H264', 'H265'):
        raise ValueError('unsupported video codec')
    if source.transport == 'RTP':
        if source.rtp_host != '127.0.0.1' or source.rtsp_url is not None:
            raise ValueError('RTP source must be private loopback')
        if type(source.rtp_port) is not int:
            raise ValueError('invalid RTP port')
        port_number(source.rtp_port)
    elif source.transport == 'RTSP':
        if source.rtp_host is not None or source.rtp_port is not None:
            raise ValueError('RTSP descriptor has RTP fields')
        parsed = urlsplit(source.rtsp_url)
        if (parsed.scheme != 'rtsp' or parsed.hostname != '127.0.0.1'
                or parsed.username or parsed.password or not parsed.path
                or parsed.query or parsed.fragment):
            raise ValueError('RTSP source must be a local mount')
        if parsed.port is not None:
            port_number(parsed.port)
    else:
        raise ValueError('unsupported video transport')
    return source


def read_state(path=STATE_FILE, now=None):
    try:
        with open(path, encoding='utf-8') as stream:
            raw = json.loads(stream.read(16385))
        if type(raw.get('version')) is not int or raw['version'] != 1:
            return None
        updated = raw['updated_monotonic']
        if type(updated) not in (int, float) or not math.isfinite(updated):
            return None
        age = (time.monotonic() if now is None else now) - updated
        if not 0 <= age <= MAX_AGE:
            return None
        return validate_source(VideoSource(**{key: raw[key] for key in VideoSource.__dataclass_fields__}))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


class StatePublisher:
    def __init__(self, path, source):
        self.path = Path(path)
        self.source = validate_source(replace(source, instance=uuid.uuid4().hex))
        self.claimed = False
        self.retired = False
        self.lock_path = self.path.with_name('.' + self.path.name + '.lock')

    @contextmanager
    def _locked(self):
        # Keep this inode for the runtime directory's lifetime. Removing a lock
        # file can split competing processes across different locked inodes.
        with self.lock_path.open('a+', encoding='ascii') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                lock.seek(0)
                yield lock
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def refresh(self):
        if self.retired:
            return False
        with self._locked() as lock:
            if self.claimed and lock.read(128) != self.source.instance:
                self.retired = True
                return False
            raw = dict(asdict(self.source), version=1, updated_monotonic=time.monotonic())
            fd, name = tempfile.mkstemp(prefix='.video-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    os.fchmod(stream.fileno(), 0o644)
                    json.dump(raw, stream, allow_nan=False)
                os.replace(name, self.path)
                lock.seek(0)
                lock.truncate()
                lock.write(self.source.instance)
                lock.flush()
                self.claimed = True
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return True  # GLib timeout callback keeps running.

    def close(self):
        self.retired = True
        if not self.claimed:
            return
        try:
            with self._locked() as lock:
                if lock.read(128) != self.source.instance:
                    return
                raw = json.loads(self.path.read_text())
                if raw.get('instance') == self.source.instance:
                    self.path.unlink()
        except (OSError, ValueError, AttributeError):
            pass
