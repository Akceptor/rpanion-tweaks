"""Network settings page for trusted networks; no shell evaluation of env files.

Managed assignments support one unquoted or simply quoted value per line. Escapes,
multiline values and embedded quotes are rejected rather than reinterpreted.
Unknown assignments and comments are preserved verbatim.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit

from fly_ground_config import Settings
from fly_ground_auth import validate_host_token
from video_output import VideoOutputStore, Conflict as OutputConflict
from video_stream_state import read_state
from fly_ground_services import ServiceManager, Busy

FIELDS = {
    'FLY_ROOM_CODE': 'code', 'FLY_SITE_URL': 'site_url', 'FLY_RTSP_URL': 'rtsp_url',
    'FLY_MAVLINK': 'router_url', 'FLY_MONITOR': 'monitor_url', 'FLY_CHANNELS': 'channels',
    'FLY_THROTTLE_CHANNEL': 'throttle_channel', 'FLY_GCS_SYSID': 'gcs_sysid',
    'FLY_GCS_COMPID': 'gcs_compid', 'FLY_PLANNER_SYSID': 'planner_sysid',
    'FLY_PLANNER_COMPID': 'planner_compid', 'FLY_SESSION_SECONDS': 'session_seconds',
}
ASSIGN = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$')
ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'

class Conflict(ValueError):
    pass


def clean_value(value):
    if not isinstance(value, str) or any(c in value for c in '\n\r\x00\\\"\''):
        raise ValueError('Settings must be single-line values without quotes or escapes')
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Control characters are not supported')
    return value


def parse_env(raw, fields=FIELDS):
    env = {}
    for line in raw.decode('utf-8').splitlines():
        match = ASSIGN.match(line)
        if not match or match[1] not in fields:
            continue
        value = match[2].strip()
        if value[:1] in ('"', "'"):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError('Unsupported quoted setting')
            value = value[1:-1]
        env[match[1]] = clean_value(value)
    return env


def host_token_configured(raw):
    try:
        validate_host_token(parse_env(raw, {'FLY_HOST_TOKEN'}).get('FLY_HOST_TOKEN'))
        return True
    except ValueError:
        return False


def normalized(env):
    settings = Settings.from_env(env)
    return {key: ','.join(map(str, getattr(settings, attr))) if attr == 'channels'
            else str(getattr(settings, attr)) for key, attr in FIELDS.items()}


class ConfigStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.lock = threading.RLock()

    def _read(self):
        raw = self.path.read_bytes()
        return raw, {'settings': normalized(parse_env(raw)),
                     'host_token_configured': host_token_configured(raw),
                     'revision': hashlib.sha256(raw).hexdigest()}

    def site_origin(self):
        # Service recovery must work even when an unrelated setting is invalid.
        try:
            with self.lock:
                site = parse_env(self.path.read_bytes(), {'FLY_SITE_URL'}).get('FLY_SITE_URL', '').rstrip('/')
            parsed = urlsplit(site)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                    or parsed.password or parsed.path or parsed.query or parsed.fragment):
                return None
            host = parsed.hostname.lower()
            if ':' in host:
                host = '[' + host + ']'
            port = parsed.port
            return 'https://' + host + (':' + str(port) if port not in (None, 443) else '')
        except (OSError, ValueError, UnicodeError):
            return None

    def read(self):
        with self.lock:
            return self._read()[1]

    def _write(self, updates, revision):
        raw, current = self._read()
        if not isinstance(revision, str) or revision != current['revision']:
            raise Conflict('Settings changed; reload before saving')
        # Validate the complete effective configuration before touching disk.
        normalized(dict(current['settings'], **updates))
        lines, written = [], set()
        for line in raw.decode('utf-8').splitlines(keepends=True):
            match = ASSIGN.match(line.rstrip('\r\n'))
            key = match[1] if match else None
            if key in updates:
                if key not in written:
                    lines.append(f'{key}="{updates[key]}"\n')
                    written.add(key)
            else:
                lines.append(line)
        for key, value in updates.items():
            if key not in written:
                if lines and not lines[-1].endswith('\n'): lines.append('\n')
                lines.append(f'{key}="{value}"\n')
        before = self.path.stat()
        fd, name = tempfile.mkstemp(prefix='.fly-ground-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                if (os.fstat(stream.fileno()).st_uid, os.fstat(stream.fileno()).st_gid) != (before.st_uid, before.st_gid):
                    os.fchown(stream.fileno(), before.st_uid, before.st_gid)
                os.fchmod(stream.fileno(), 0o600)
                stream.writelines(lines)
                stream.flush(); os.fsync(stream.fileno())
            # Detect changes made by external editors during serialization.
            if self.path.read_bytes() != raw:
                raise Conflict('Settings changed; reload before saving')
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if os.path.exists(name): os.unlink(name)
        return self._read()[1]

    def save(self, values, revision):
        if not isinstance(values, dict) or set(values) != set(FIELDS):
            raise ValueError('Supply exactly the supported settings')
        values = {key: clean_value(value) for key, value in values.items()}
        values = normalized(values)
        with self.lock:
            return self._write(values, revision)

    def recreate(self, revision):
        code = 'CRAWLER-' + '-'.join(''.join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3))
        with self.lock:
            return self._write({'FLY_ROOM_CODE': code}, revision)

    def set_host_token(self, token, revision):
        if token != '':
            validate_host_token(token)
        with self.lock:
            if token == '':
                current = self._read()[1]
                if not isinstance(revision, str) or revision != current['revision']:
                    raise Conflict('Settings changed; reload before saving')
                return current
            return self._write({'FLY_HOST_TOKEN': token}, revision)


def restart_service():
    subprocess.run(['systemctl', 'restart', 'fly-ground.service'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


class SettingsServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store, restart=restart_service, output_store=None, services=None):
        self.store, self.restart = store, restart
        self.services = services if services is not None else ServiceManager()
        self.output_store = output_store if output_store is not None else VideoOutputStore()
        self.write_lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Request URLs/body may contain secrets; do not log them.

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def respond(self, status, value, content_type='application/json'):
        body = json.dumps(value).encode() if content_type == 'application/json' else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        if getattr(self, 'cors_origin', None):
            self.send_header('Access-Control-Allow-Origin', self.cors_origin)
            self.send_header('Vary', 'Origin')
            if self.command == 'OPTIONS':
                self.send_header('Access-Control-Allow-Methods', 'GET, POST')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-CSRF-Token')
        self.end_headers(); self.wfile.write(body)

    def state(self, result=None):
        return dict(result or self.server.store.read(), token=self.server.token)

    def output_state(self, result=None):
        saved = self.server.output_store.read() if result is None else result
        source = read_state()
        applied = source.output_mode if source is not None else None
        return dict(saved, transport=source.transport if source is not None else None,
                    applied_mode=applied, running=source is not None,
                    pending=saved["mode"] != applied if source is not None else None)

    def service_origin_allowed(self):
        origin = self.headers.get('Origin')
        if origin is None:
            return self.headers.get('Sec-Fetch-Site') != 'cross-site'
        if origin == 'http://' + self.headers.get('Host', ''):
            return True
        if origin == self.server.store.site_origin():
            self.cors_origin = origin
            return True
        return False

    def do_OPTIONS(self):
        requested = {header.strip().lower() for header in self.headers.get('Access-Control-Request-Headers', '').split(',') if header.strip()}
        if (self.path != '/api/services'
                or self.headers.get('Access-Control-Request-Method') not in ('GET', 'POST')
                or not requested <= {'content-type', 'x-csrf-token'}
                or not self.service_origin_allowed()):
            self.respond(403, {'error': 'Request denied'}); return
        self.respond(204, b'', 'text/plain')

    def do_GET(self):
        if self.path == '/api/services' and not self.service_origin_allowed():
            self.respond(403, {'error': 'Request denied'}); return
        try:
            if self.path == '/':
                self.respond(200, Path(__file__).with_name('fly_ground_settings.html').read_bytes(), 'text/html; charset=utf-8')
            elif self.path == '/api/services': self.respond(200, dict(self.server.services.read(), token=self.server.token))
            elif self.path == '/api/settings': self.respond(200, self.state())
            elif self.path == '/api/video-output': self.respond(200, self.output_state())
            else: self.respond(404, {'error': 'Not found'})
        except (OSError, ValueError): self.respond(500, {'error': 'Unable to read settings; check the environment file'})

    def do_POST(self):
        origin = self.headers.get('Origin')
        expected = 'http://' + self.headers.get('Host', '')
        origin_allowed = (self.service_origin_allowed() if self.path == '/api/services'
                          else self.headers.get('Sec-Fetch-Site') != 'cross-site'
                          and (origin is None or origin == expected))
        if (not origin_allowed
                or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.server.token)):
            self.respond(403, {'error': 'Request denied'}); return
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            self.respond(415, {'error': 'JSON required'}); return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 16384: raise ValueError('Invalid request size')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict): raise ValueError('JSON object required')
            if self.path == '/api/services':
                if set(data) != {'service', 'action'}:
                    raise ValueError('Supply exactly service and action')
                result = self.server.services.operate(data['service'], data['action'])
                self.respond(200, dict(result, token=self.server.token)); return
            with self.server.write_lock:
                if self.path == '/api/video-output':
                    if set(data) != {'mode', 'revision'}:
                        raise ValueError('Supply exactly mode and revision')
                    result = self.server.output_store.save(data['mode'], data['revision'])
                    self.respond(200, self.output_state(result)); return
                elif self.path == '/api/settings':
                    result = self.server.store.save(data.get('settings'), data.get('revision'))
                elif self.path == '/api/recreate':
                    result = self.server.store.recreate(data.get('revision'))
                elif self.path == '/api/host-token':
                    if set(data) != {'token', 'revision'}:
                        raise ValueError('Supply exactly token and revision')
                    result = self.server.store.set_host_token(data['token'], data['revision'])
                elif self.path == '/api/restart':
                    if not self.server.services.operation_lock.acquire(blocking=False):
                        raise Busy('Another service operation is in progress; retry shortly')
                    try:
                        self.server.restart()
                    finally:
                        self.server.services.operation_lock.release()
                    self.respond(200, {'ok': True}); return
                else: self.respond(404, {'error': 'Not found'}); return
                self.respond(200, self.state(result))
        except (Conflict, OutputConflict, Busy) as exc: self.respond(409, {'error': str(exc)})
        except (ValueError, UnicodeError): self.respond(400, {'error': 'Invalid settings or request'})
        except (OSError, subprocess.SubprocessError):
            message = ('Service operation failed or timed out; refresh status before retrying'
                       if self.path == '/api/services' else 'Operation failed; check service permissions')
            self.respond(500, {'error': message})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', default='/etc/fly-ground.env')
    parser.add_argument('--host', '--bind', dest='bind', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=4001)
    args = parser.parse_args()
    server = SettingsServer((args.bind, args.port), ConfigStore(args.env_file))
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == '__main__': main()
