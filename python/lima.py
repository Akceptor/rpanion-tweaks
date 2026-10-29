#!/usr/bin/env python3
import ipaddress
import json
import logging
import math
import os
import select
import signal
import socket
import sys
import tempfile
import threading
import time

from pymavlink.dialects.v20 import ardupilotmega as mavlink

R_EARTH = 6378137.0


class Anchor:
    def __init__(self, real_lat, real_lon, fake_lat, fake_lon):
        self.real_lat = real_lat
        self.real_lon = real_lon
        self.fake_lat = fake_lat
        self.fake_lon = fake_lon

    def to_fake(self, lat, lon):
        d_n = (lat - self.real_lat) * R_EARTH * math.pi / 180
        d_e = (lon - self.real_lon) * R_EARTH * math.cos(math.radians(self.real_lat)) * math.pi / 180
        return (
            self.fake_lat + d_n / (R_EARTH * math.pi / 180),
            self.fake_lon + d_e / (R_EARTH * math.cos(math.radians(self.fake_lat)) * math.pi / 180),
        )

    def to_real(self, lat, lon):
        d_n = (lat - self.fake_lat) * R_EARTH * math.pi / 180
        d_e = (lon - self.fake_lon) * R_EARTH * math.cos(math.radians(self.fake_lat)) * math.pi / 180
        return (
            self.real_lat + d_n / (R_EARTH * math.pi / 180),
            self.real_lon + d_e / (R_EARTH * math.cos(math.radians(self.real_lat)) * math.pi / 180),
        )


def distance_m(lat1, lon1, lat2, lon2):
    mean_lat = math.radians((lat1 + lat2) / 2)
    d_n = math.radians(lat2 - lat1) * R_EARTH
    d_e = math.radians(lon2 - lon1) * R_EARTH * math.cos(mean_lat)
    return math.hypot(d_n, d_e)


class AnchorStore:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            vals = [float(d[k]) for k in ("real_lat", "real_lon", "fake_lat", "fake_lon")]
        except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
            return None
        if not all(math.isfinite(v) for v in vals):
            return None
        if abs(vals[0]) > 90 or abs(vals[2]) > 90 or abs(vals[1]) > 180 or abs(vals[3]) > 180:
            return None
        return Anchor(*vals)

    def save(self, anchor):
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=d)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({
                    "real_lat": anchor.real_lat, "real_lon": anchor.real_lon,
                    "fake_lat": anchor.fake_lat, "fake_lon": anchor.fake_lon,
                }, f)
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise


CONVERT = {
    "GLOBAL_POSITION_INT": ("lat", "lon", 1e7),
    "GPS_RAW_INT": ("lat", "lon", 1e7),
    "GPS2_RAW": ("lat", "lon", 1e7),
    "HOME_POSITION": ("latitude", "longitude", 1e7),
    "SET_HOME_POSITION": ("latitude", "longitude", 1e7),
    "GPS_GLOBAL_ORIGIN": ("latitude", "longitude", 1e7),
    "SET_GPS_GLOBAL_ORIGIN": ("latitude", "longitude", 1e7),
    "POSITION_TARGET_GLOBAL_INT": ("lat_int", "lon_int", 1e7),
    "SET_POSITION_TARGET_GLOBAL_INT": ("lat_int", "lon_int", 1e7),
    "AHRS2": ("lat", "lng", 1e7),
    "AHRS3": ("lat", "lng", 1e7),
    "FENCE_POINT": ("lat", "lng", 1.0),
    "RALLY_POINT": ("lat", "lng", 1e7),
    "HIGH_LATENCY2": ("latitude", "longitude", 1e7),
    "CAMERA_FEEDBACK": ("lat", "lng", 1e7),
    "GLOBAL_POSITION_INT_COV": ("lat", "lon", 1e7),
    "TERRAIN_REQUEST": ("lat", "lon", 1e7),
    "TERRAIN_REPORT": ("lat", "lon", 1e7),
    "SIMSTATE": ("lat", "lng", 1e7),
}

COMMAND_AWARE = frozenset({"MISSION_ITEM", "MISSION_ITEM_INT", "COMMAND_INT", "COMMAND_LONG"})

_EXPLICIT_POSITION_COMMANDS = frozenset(
    getattr(mavlink, "MAV_CMD_" + n) for n in (
        "NAV_WAYPOINT", "NAV_LOITER_UNLIM", "NAV_LOITER_TURNS", "NAV_LOITER_TIME",
        "NAV_LOITER_TO_ALT", "NAV_LAND", "NAV_TAKEOFF", "NAV_SPLINE_WAYPOINT",
        "DO_SET_HOME", "DO_SET_ROI_LOCATION", "DO_SET_ROI", "DO_REPOSITION",
        "DO_LAND_START",
        "NAV_FENCE_POLYGON_VERTEX_INCLUSION", "NAV_FENCE_POLYGON_VERTEX_EXCLUSION",
        "NAV_FENCE_CIRCLE_INCLUSION", "NAV_FENCE_CIRCLE_EXCLUSION",
        "NAV_RALLY_POINT",
    )
)

_LOCAL_XY_COMMANDS = frozenset((
    mavlink.MAV_CMD_NAV_LAND_LOCAL, mavlink.MAV_CMD_NAV_TAKEOFF_LOCAL,
))

POSITION_COMMANDS = (_EXPLICIT_POSITION_COMMANDS | frozenset(
    cmd for cmd, e in mavlink.enums["MAV_CMD"].items()
    if e.has_location or e.param.get(5) == "Latitude" or e.param.get(6) == "Longitude"
)) - _LOCAL_XY_COMMANDS

KNOWN_COMMANDS = frozenset(mavlink.enums["MAV_CMD"])

GLOBAL_FRAMES = frozenset(
    getattr(mavlink, "MAV_FRAME_" + n) for n in (
        "GLOBAL", "GLOBAL_RELATIVE_ALT", "GLOBAL_INT", "GLOBAL_RELATIVE_ALT_INT",
        "GLOBAL_TERRAIN_ALT", "GLOBAL_TERRAIN_ALT_INT",
    )
)

_PASS_BOTH = frozenset("""
PROTOCOL_VERSION HEARTBEAT SYS_STATUS SYSTEM_TIME TIMESYNC PING STATUSTEXT COMMAND_ACK
PARAM_REQUEST_LIST PARAM_REQUEST_READ PARAM_SET PARAM_VALUE
MISSION_COUNT MISSION_REQUEST MISSION_REQUEST_INT MISSION_ACK MISSION_CURRENT
MISSION_ITEM_REACHED MISSION_CLEAR_ALL MISSION_REQUEST_LIST MISSION_SET_CURRENT
MESSAGE_INTERVAL AUTOPILOT_VERSION AUTOPILOT_VERSION_REQUEST
""".split())

PASS_DOWN = _PASS_BOTH | frozenset("""
ATTITUDE ATTITUDE_QUATERNION ATTITUDE_TARGET AHRS VFR_HUD NAV_CONTROLLER_OUTPUT
LOCAL_POSITION_NED EKF_STATUS_REPORT ESTIMATOR_STATUS VIBRATION WIND WIND_COV
RANGEFINDER DISTANCE_SENSOR
RC_CHANNELS RC_CHANNELS_RAW RC_CHANNELS_SCALED SERVO_OUTPUT_RAW
RAW_IMU SCALED_IMU SCALED_IMU2 SCALED_IMU3
SCALED_PRESSURE SCALED_PRESSURE2 SCALED_PRESSURE3
BATTERY_STATUS BATTERY2 POWER_STATUS SMART_BATTERY_INFO
GPS_STATUS FENCE_STATUS MEMINFO HWSTATUS RADIO_STATUS RADIO
EXTENDED_SYS_STATE ESC_TELEMETRY_1_TO_4 ESC_TELEMETRY_5_TO_8
ESC_TELEMETRY_9_TO_12 ESC_TELEMETRY_13_TO_16 NAMED_VALUE_FLOAT NAMED_VALUE_INT
DEBUG DEBUG_VECT PID_TUNING MAG_CAL_PROGRESS MAG_CAL_REPORT SENSOR_OFFSETS
LIMITS_STATUS WHEEL_DISTANCE OPTICAL_FLOW RPM RELAY_STATUS
PARAM_ERROR PARAM_EXT_VALUE PARAM_EXT_ACK
""".split())

PASS_UP = _PASS_BOTH | frozenset("""
REQUEST_DATA_STREAM SET_MODE MANUAL_CONTROL RC_CHANNELS_OVERRIDE
MISSION_WRITE_PARTIAL_LIST MISSION_REQUEST_PARTIAL_LIST
FENCE_FETCH_POINT RALLY_FETCH_POINT
PARAM_EXT_REQUEST_LIST PARAM_EXT_REQUEST_READ PARAM_EXT_SET
SET_MAG_OFFSETS
GPS_RTCM_DATA GPS_INJECT_DATA
""".split())


def classify(direction, name):
    if name in COMMAND_AWARE:
        return "command"
    if name in CONVERT:
        return "convert"
    if name in (PASS_DOWN if direction == "down" else PASS_UP):
        return "pass"
    return "drop"


log = logging.getLogger("lima")

INT32_MAX = 2147483647


def repack(msg, mav):
    # pack() takes seq and the source ids from the MAVLink object, not from msg
    mav.seq = msg.get_seq()
    mav.srcSystem = msg.get_srcSystem()
    mav.srcComponent = msg.get_srcComponent()
    v1 = msg.get_msgbuf()[0] == 0xFE
    return msg.pack(mav, force_mavlink1=v1)


_XYZ_IGNORE = (mavlink.POSITION_TARGET_TYPEMASK_X_IGNORE
               | mavlink.POSITION_TARGET_TYPEMASK_Y_IGNORE
               | mavlink.POSITION_TARGET_TYPEMASK_Z_IGNORE)


def _unset(a, b):
    return a in (0, INT32_MAX) and b in (0, INT32_MAX)


class Rewriter:
    def __init__(self, store, fake_anchor, max_target_m, clock=time.monotonic):
        self.store = store
        self.clock = clock
        self.fake_anchor = fake_anchor
        self.max_target_m = max_target_m
        self.anchor = store.load()
        if self.anchor is not None and (
                abs(self.anchor.fake_lat - fake_anchor[0]) > 1e-7
                or abs(self.anchor.fake_lon - fake_anchor[1]) > 1e-7):
            self.anchor = Anchor(self.anchor.real_lat, self.anchor.real_lon, *fake_anchor)
            store.save(self.anchor)
        self.real_pos = None
        self._parsers = {
            d: mavlink.MAVLink(None) for d in ("down", "up")
        }
        for p in self._parsers.values():
            p.robust_parsing = True
        self._out = mavlink.MAVLink(None)
        self._logged = set()
        self.vehicle_sysid = 1
        self._warn_mav = mavlink.MAVLink(
            None, srcComponent=mavlink.MAV_COMP_ID_ONBOARD_COMPUTER)
        self._warnings = []
        self._warned_at = {}

    def downlink(self, datagram):
        return self._process("down", datagram, True)

    def uplink(self, datagram):
        return self._process("up", datagram, False)

    def take_warnings(self):
        out, self._warnings = self._warnings, []
        return out

    def _warn(self, name, detail):
        now = self.clock()
        last = self._warned_at.get(name)
        if last is not None and now - last < 1.0:
            return
        self._warned_at[name] = now
        text = f"Lima: dropped {name} {detail}"[:50]
        self._warn_mav.srcSystem = self.vehicle_sysid
        msg = mavlink.MAVLink_statustext_message(mavlink.MAV_SEVERITY_WARNING, text.encode())
        self._warnings.append(msg.pack(self._warn_mav))

    def _process(self, direction, datagram, to_fake):
        out = []
        for msg in self._parsers[direction].parse_buffer(datagram) or []:
            if msg.get_type() == "BAD_DATA":
                continue
            try:
                data = self._handle(direction, msg, to_fake)
            except Exception:
                name = msg.get_type()
                if name not in self._logged:
                    self._logged.add(name)
                    log.exception("dropping %s", name)
                data = None
            if data is not None:
                out.append(data)
        return out

    def _handle(self, direction, msg, to_fake):
        name = msg.get_type()
        if (direction == "down" and name == "HEARTBEAT"
                and msg.type != mavlink.MAV_TYPE_GCS
                and msg.autopilot != mavlink.MAV_AUTOPILOT_INVALID):
            self.vehicle_sysid = msg.get_srcSystem()
        kind = classify(direction, name)
        if kind == "pass":
            return bytes(msg.get_msgbuf())
        if kind == "drop":
            return None
        if direction == "down":
            self._track(msg)
        if kind == "convert":
            lat_f, lon_f, scale = CONVERT[name]
            if (not to_fake and name == "SET_POSITION_TARGET_GLOBAL_INT"
                    and msg.type_mask & _XYZ_IGNORE == _XYZ_IGNORE):
                return bytes(msg.get_msgbuf())
            if to_fake and _unset(getattr(msg, lat_f), getattr(msg, lon_f)):
                return bytes(msg.get_msgbuf())
        else:
            action = self._command_action(msg)
            if action == "pass":
                return bytes(msg.get_msgbuf())
            if action == "drop":
                if not to_fake:
                    self._warn(name, "unsupported")
                return None
            lat_f, lon_f, scale = self._command_fields(msg)
        if self.anchor is None:
            if to_fake and name == "GLOBAL_POSITION_INT":
                msg.lat = round(self.fake_anchor[0] * 1e7)
                msg.lon = round(self.fake_anchor[1] * 1e7)
                return repack(msg, self._out)
            if to_fake and name in ("GPS_RAW_INT", "GPS2_RAW"):
                # Zeroed, not dropped: a GCS that gets no GPS_RAW_INT shows "No GPS" instead of "No Fix".
                setattr(msg, lat_f, 0)
                setattr(msg, lon_f, 0)
                return repack(msg, self._out)
            if not to_fake:
                self._warn(name, "no anchor")
            return None
        if not to_fake and self.real_pos is None:
            self._warn(name, "no position")
            return None
        convert = self.anchor.to_fake if to_fake else self.anchor.to_real
        lat = getattr(msg, lat_f) / scale
        lon = getattr(msg, lon_f) / scale
        lat, lon = convert(lat, lon)
        if not to_fake:
            dist = distance_m(*self.real_pos, lat, lon)
            if not dist <= self.max_target_m:
                self._warn(name, f"{dist / 1000:.1f}km")
                return None
        if scale == 1.0:
            setattr(msg, lat_f, lat)
            setattr(msg, lon_f, lon)
        else:
            setattr(msg, lat_f, round(lat * scale))
            setattr(msg, lon_f, round(lon * scale))
        return repack(msg, self._out)

    def _track(self, msg):
        # GLOBAL_POSITION_INT may be agps's approximate spoof, so it must never feed the
        # anchor or the uplink guard.
        if msg.get_type() != "GPS_RAW_INT":
            return
        if not (msg.fix_type >= 3 and (msg.lat != 0 or msg.lon != 0)):
            return
        if abs(msg.lat) > 900000000 or abs(msg.lon) > 1800000000:
            return
        lat, lon = msg.lat / 1e7, msg.lon / 1e7
        self.real_pos = (lat, lon)
        if self.anchor is None:
            self.anchor = Anchor(lat, lon, *self.fake_anchor)
            self.store.save(self.anchor)

    @staticmethod
    def _command_fields(msg):
        name = msg.get_type()
        if name == "COMMAND_LONG":
            return "param5", "param6", 1.0
        if name == "MISSION_ITEM":
            return "x", "y", 1.0
        return "x", "y", 1e7

    @classmethod
    def _command_action(cls, msg):
        name = msg.get_type()
        lat_f, lon_f, _ = cls._command_fields(msg)
        x, y = getattr(msg, lat_f), getattr(msg, lon_f)
        if name in ("COMMAND_LONG", "MISSION_ITEM"):
            unset = all(v == 0 or math.isnan(v) for v in (x, y))
        else:
            unset = _unset(x, y)
        if unset:
            return "pass"
        frame = getattr(msg, "frame", None)
        # COMMAND_LONG has no frame; its params 5/6 are global for position commands
        global_like = frame is None or frame in GLOBAL_FRAMES or frame == mavlink.MAV_FRAME_MISSION
        if msg.command in POSITION_COMMANDS:
            return "convert" if global_like else "drop"
        if msg.command in KNOWN_COMMANDS:
            return "pass"
        return "drop" if global_like else "pass"


def parse_hostport(s):
    host, sep, port = s.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"expected host:port, got {s!r}")
    return host, int(port)


def parse_latlon(s):
    parts = s.split(",")
    if len(parts) != 2:
        raise ValueError(f"expected lat,lon, got {s!r}")
    lat, lon = float(parts[0]), float(parts[1])
    if not (abs(lat) <= 90 and abs(lon) <= 180):
        raise ValueError(f"lat,lon out of range: {s!r}")
    return lat, lon


def _log_anchor(rewriter, was_set):
    if rewriter.anchor is not None and not was_set:
        log.info("Lima: anchor set")
    return rewriter.anchor is not None


PEER_TIMEOUT = 5.0


def parse_ip_list(s):
    return {str(ipaddress.IPv4Address(part.strip())) for part in s.split(",")}


STATUS_INTERVAL = 1.0


def status_datagram(on, fake):
    return f"LIMA {int(on)} {fake[0]:.5f} {fake[1]:.5f}".encode("ascii")


def run(listen, serve, rewriter, stop, allow=None, clock=time.monotonic, status=None):
    from_router = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    from_gcs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    status_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        from_router.bind(listen)
        from_gcs.bind(serve)
        router_addr = None
        peer = None
        peer_seen = 0.0
        status_sent = None
        status_state = None
        anchored = rewriter.anchor is not None
        if anchored:
            log.info("Lima: anchor loaded")
        while not stop.is_set():
            ready, _, _ = select.select([from_router, from_gcs], [], [], 0.5)
            if peer is not None and clock() - peer_seen > PEER_TIMEOUT:
                log.info("Lima: GCS timed out")
                peer = None
            for sock in ready:
                # On Linux an ICMP port-unreachable from an earlier sendto makes the next
                # recvfrom raise ConnectionRefusedError; a GCS that is off must not kill us.
                try:
                    data, addr = sock.recvfrom(65535)
                except OSError:
                    continue
                if sock is from_router:
                    router_addr = addr
                    for pkt in rewriter.downlink(data):
                        if peer is not None:
                            _send(from_gcs, pkt, peer)
                    anchored = _log_anchor(rewriter, anchored)
                else:
                    if allow is not None and addr[0] not in allow:
                        continue
                    out = rewriter.uplink(data)
                    warnings = rewriter.take_warnings()
                    if not (out or warnings):
                        continue
                    if addr != peer:
                        log.info("Lima: GCS connected %s:%d", *addr)
                    peer = addr
                    peer_seen = clock()
                    for pkt in out:
                        if router_addr is not None:
                            _send(from_router, pkt, router_addr)
                    for w in warnings:
                        _send(from_gcs, w, peer)
            if status is not None:
                state = peer is not None
                now = clock()
                if state != status_state or now - status_sent >= STATUS_INTERVAL:
                    _send(status_sock, status_datagram(state, rewriter.fake_anchor), status)
                    status_state, status_sent = state, now
    finally:
        from_router.close()
        from_gcs.close()
        status_sock.close()


def _send(sock, data, dest):
    try:
        sock.sendto(data, dest)
    except OSError:
        pass


def _setting(name, default, parse):
    raw = os.environ.get(name, default)
    try:
        if raw is None:
            raise ValueError("required")
        return parse(raw)
    except ValueError as e:
        print(f"Lima: invalid {name}: {e}", file=sys.stderr)
        sys.exit(2)


def _parse_km(s):
    km = float(s)
    if not 0 <= km < float("inf"):
        raise ValueError(f"expected a non-negative number, got {s!r}")
    return km


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    serve = _setting("LIMA_SERVE", "0.0.0.0:14551", parse_hostport)
    allow = _setting("LIMA_GCS_ALLOW", None, parse_ip_list) if "LIMA_GCS_ALLOW" in os.environ else None
    listen = _setting("LIMA_LISTEN", "127.0.0.1:14580", parse_hostport)
    fake_anchor = _setting("LIMA_FAKE_ANCHOR", "-12.0700,-77.0435", parse_latlon)
    status = _setting("LIMA_STATUS", "127.0.0.1:14571", parse_hostport)
    max_km = _setting("LIMA_MAX_TARGET_KM", "5", _parse_km)
    store = AnchorStore(os.environ.get("LIMA_ANCHOR_FILE", "/run/lima/anchor.json"))
    rewriter = Rewriter(store, fake_anchor, max_km * 1000)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    run(listen, serve, rewriter, stop, allow=allow, status=status)


if __name__ == "__main__":
    main()
