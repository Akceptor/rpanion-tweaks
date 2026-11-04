"""Pure control-packet validation and browser/Mission Planner ownership."""

from dataclasses import dataclass


PACKET_LEN = 42
CH_MIN = 172
CH_MAX = 1811


@dataclass(frozen=True)
class ControlPacket:
    seq: int
    ts: int
    channels: tuple[int, ...]


def decode_packet(data):
    if not isinstance(data, (bytes, bytearray)) or len(data) != PACKET_LEN or data[0] != 1:
        return None
    channels = tuple(int.from_bytes(data[10 + 2 * i:12 + 2 * i], "little") for i in range(16))
    if any(v < CH_MIN or v > CH_MAX for v in channels):
        return None
    return ControlPacket(int.from_bytes(data[2:6], "little"), int.from_bytes(data[6:10], "little"), channels)


def crsf_to_pwm(value):
    if not CH_MIN <= value <= CH_MAX:
        raise ValueError("CRSF channel out of range")
    return round(1000 + (value - CH_MIN) * 1000 / (CH_MAX - CH_MIN))


class Arbiter:
    def __init__(self, channels=(1, 3), throttle_channel=3):
        if not channels or any(c < 1 or c > 18 for c in channels):
            raise ValueError("invalid channel allowlist")
        if throttle_channel not in channels or throttle_channel > 16:
            raise ValueError("throttle channel must be in allowlist and packet")
        self.channels = tuple(channels)
        self.throttle_channel = throttle_channel
        self.peer_connected = False
        self.owner = "idle"
        self.last_seq = None
        self.last_packet_ms = None
        self.last_channels = None
        self.last_planner_ms = None
        self.release_pending = False

    def accept_packet(self, data, now_ms, offset_ms):
        pkt = decode_packet(data)
        if pkt is None or offset_ms is None:
            return False
        if self.last_seq is not None and pkt.seq <= self.last_seq:
            return False
        age = now_ms - (pkt.ts - offset_ms)
        if age > 200 or age < -200:
            return False
        self.last_seq = pkt.seq
        self.last_packet_ms = now_ms
        self.last_channels = pkt.channels
        return True

    def planner_seen(self, now_ms):
        self.last_planner_ms = now_ms
        self.owner_at(now_ms)

    def owner_at(self, now_ms):
        planner = self.last_planner_ms is not None and now_ms - self.last_planner_ms <= 3000
        if planner:
            if self.owner == "browser":
                self.release_pending = True
            self.owner = "mission-planner"
        elif self.owner == "mission-planner":
            self.owner = "idle"
        elif self.owner == "browser" and not self._fresh(now_ms):
            self.owner = "idle"
            self.release_pending = True
        return self.owner

    def _fresh(self, now_ms):
        return (self.peer_connected and self.last_packet_ms is not None
                and 0 <= now_ms - self.last_packet_ms <= 250)

    def resume(self, now_ms):
        if self.owner_at(now_ms) == "mission-planner" or not self._fresh(now_ms):
            return False
        if abs(self.last_channels[self.throttle_channel - 1] - 992) > 64:
            return False
        self.owner = "browser"
        return True

    def output(self, now_ms):
        if self.owner_at(now_ms) != "browser":
            return None
        values = [65535] * 18
        for channel in self.channels:
            if channel <= 16:
                values[channel - 1] = crsf_to_pwm(self.last_channels[channel - 1])
        return tuple(values)

    def take_release(self):
        if not self.release_pending:
            return None
        self.release_pending = False
        values = [65535] * 18
        for channel in self.channels:
            values[channel - 1] = 0
        return tuple(values)

    def disconnect(self):
        was_browser = self.owner == "browser"
        self.peer_connected = False
        self.owner = "idle"
        self.last_seq = None
        self.last_packet_ms = None
        self.last_channels = None
        self.release_pending = self.release_pending or was_browser
