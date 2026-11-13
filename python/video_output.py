"""Shared, atomic video-output preference. Missing configuration keeps both outputs."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

OUTPUT_FILE = os.environ.get('RPANION_VIDEO_OUTPUT_FILE', '/var/lib/rpanion-video/output.json')


class Conflict(ValueError):
    pass


def validate_output_mode(mode):
    if not isinstance(mode, str) or mode not in ('webrtc', 'rpanion', 'both'):
        raise ValueError('Invalid video output mode')
    return mode


def _read(path):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None, 'both'
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {'mode'}:
            raise ValueError('Supply exactly mode')
        return raw, validate_output_mode(value['mode'])
    except (UnicodeError, TypeError) as exc:
        raise ValueError('Invalid video output file') from exc


def read_output_mode(path=OUTPUT_FILE):
    return _read(path)[1]


class VideoOutputStore:
    """Caller serializes saves; revision detects edits by other writers."""
    def __init__(self, path=OUTPUT_FILE):
        self.path = Path(path)

    def read(self):
        raw, mode = _read(self.path)
        return {'mode': mode, 'revision': hashlib.sha256(b'missing' if raw is None else b'file:' + raw).hexdigest()}

    def save(self, mode, revision):
        validate_output_mode(mode)
        before = self.read()
        if not isinstance(revision, str) or revision != before['revision']:
            raise Conflict('Video output changed; reload before saving')
        fd, name = tempfile.mkstemp(prefix='.output-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o644)
                json.dump({'mode': mode}, stream)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            if self.read()['revision'] != before['revision']:
                raise Conflict('Video output changed; reload before saving')
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if os.path.exists(name): os.unlink(name)
        return self.read()
