"""Bounded, allowlisted systemd controls and read-only producer health."""
import subprocess
import threading

from video_stream_state import read_state

UNITS = {
    'rpanion': ('Rpanion', 'rpanion-server.service'),
    'bridge': ('WebRTC bridge', 'fly-ground.service'),
    'lima': ('Lima', 'lima.service'),
    'agps': ('AGPS', 'agps.service'),
}
ACTIONS = ('start', 'stop', 'restart')
STATES = {'active': 'up', 'inactive': 'down', 'failed': 'down',
          'activating': 'starting', 'deactivating': 'stopping'}


class Busy(RuntimeError):
    pass


class ServiceManager:
    def __init__(self, run=subprocess.run, video_state=read_state):
        self.run = run
        self.video_state = video_state
        self.operation_lock = threading.Lock()

    def _unit_state(self, unit):
        try:
            result = self.run(['systemctl', 'show', '--property=ActiveState', '--value', unit],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, timeout=3)
            return STATES.get(result.stdout.strip(), 'unknown') if result.returncode == 0 else 'unknown'
        except (OSError, subprocess.SubprocessError):
            return 'unknown'

    def read(self):
        units = {key: self._unit_state(unit) for key, (_, unit) in UNITS.items()}
        rows = {}
        for key, (label, _) in UNITS.items():
            state = units[key]
            detail = {'up': 'Service is active.', 'down': 'Service is stopped or failed.',
                      'starting': 'Service is starting.', 'stopping': 'Service is stopping.',
                      'unknown': 'Service status is unavailable.'}[state]
            rows[key] = dict(id=key, label=label, state=state, detail=detail, actions=list(ACTIONS))
        try:
            video = 'up' if self.video_state() is not None else 'down'
        except (OSError, ValueError):
            video = 'unknown'
        rows['video'] = dict(id='video', label='Video producer', state=video, actions=[],
                            detail='Fresh producer heartbeat; not proof of frames flowing. Recover through Rpanion.' if video == 'up'
                            else 'Fresh producer heartbeat unavailable. Recover through Rpanion.')
        router = 'unknown'
        if units['rpanion'] == 'down':
            router = 'down'
        elif units['rpanion'] == 'up':
            try:
                result = self.run(['pgrep', '-x', 'mavlink-routerd'], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=3)
                router = {0: 'up', 1: 'down'}.get(result.returncode, 'unknown')
            except (OSError, subprocess.SubprocessError):
                pass
        rows['router'] = dict(id='router', label='MAVLink router', state=router, actions=[],
                              detail='Process health only. Recover through Rpanion.')
        return {'services': [rows[key] for key in ('rpanion', 'video', 'bridge', 'router', 'lima', 'agps')]}

    def _action(self, unit, action):
        result = self.run(['systemctl', action, unit], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=20)
        if result.returncode != 0:
            raise subprocess.SubprocessError('Service action failed')

    def operate(self, service, action):
        if not isinstance(service, str) or service not in UNITS or action not in ACTIONS:
            raise ValueError('Unsupported service or action')
        if not self.operation_lock.acquire(blocking=False):
            raise Busy('Another service operation is in progress; retry shortly')
        try:
            # Requires= stops the bridge when Rpanion or Lima restarts. Restore a bridge
            # that was running, without starting a deliberately stopped bridge.
            restore_bridge = service in ('rpanion', 'lima') and action == 'restart' and self._unit_state(UNITS['bridge'][1]) in ('up', 'starting')
            self._action(UNITS[service][1], action)
            if restore_bridge:
                self._action(UNITS['bridge'][1], 'start')
            return self.read()
        finally:
            self.operation_lock.release()
