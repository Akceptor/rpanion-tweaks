#!/usr/bin/env python3
import json
import logging
import math
import os
import tempfile
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
    "ADSB_VEHICLE": ("lat", "lon", 1e7),
    "HIGH_LATENCY2": ("latitude", "longitude", 1e7),
    "CAMERA_FEEDBACK": ("lat", "lng", 1e7),
    "GLOBAL_POSITION_INT_COV": ("lat", "lon", 1e7),
    "TERRAIN_REQUEST": ("lat", "lon", 1e7),
    "TERRAIN_REPORT": ("lat", "lon", 1e7),
    "SIMSTATE": ("lat", "lng", 1e7),
}

COMMAND_AWARE = frozenset({"MISSION_ITEM", "MISSION_ITEM_INT", "COMMAND_INT", "COMMAND_LONG"})

POSITION_COMMANDS = frozenset(
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

GLOBAL_FRAMES = frozenset(
    getattr(mavlink, "MAV_FRAME_" + n) for n in (
        "GLOBAL", "GLOBAL_RELATIVE_ALT", "GLOBAL_INT", "GLOBAL_RELATIVE_ALT_INT",
        "GLOBAL_TERRAIN_ALT", "GLOBAL_TERRAIN_ALT_INT",
    )
)

_PASS_BOTH = frozenset("""
HEARTBEAT SYS_STATUS SYSTEM_TIME TIMESYNC PING STATUSTEXT COMMAND_ACK
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


_XY_IGNORE = (mavlink.POSITION_TARGET_TYPEMASK_X_IGNORE
              | mavlink.POSITION_TARGET_TYPEMASK_Y_IGNORE)


def _unset(a, b):
    return a in (0, INT32_MAX) and b in (0, INT32_MAX)


class Rewriter:
    def __init__(self, store, fake_anchor, max_target_m, clock=time.monotonic):
        self.store = store
        self.clock = clock
        self.fake_anchor = fake_anchor
        self.max_target_m = max_target_m
        self.anchor = store.load()
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
            if name == "SET_POSITION_TARGET_GLOBAL_INT" and msg.type_mask & _XY_IGNORE == _XY_IGNORE:
                return bytes(msg.get_msgbuf())
            if _unset(getattr(msg, lat_f), getattr(msg, lon_f)):
                return bytes(msg.get_msgbuf())
        else:
            action = self._command_action(msg)
            if action == "pass":
                return bytes(msg.get_msgbuf())
            if action == "drop":
                return None
            lat_f, lon_f, scale = self._command_fields(msg)
        if self.anchor is None:
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
        name = msg.get_type()
        if name == "GLOBAL_POSITION_INT":
            ok = msg.lat != 0 or msg.lon != 0
        elif name == "GPS_RAW_INT":
            ok = msg.fix_type >= 3 and (msg.lat != 0 or msg.lon != 0)
        else:
            return
        if not ok:
            return
        if abs(msg.lat) > 900000000 or abs(msg.lon) > 1800000000:
            return
        lat, lon = msg.lat / 1e7, msg.lon / 1e7
        self.real_pos = (lat, lon)
        if name == "GPS_RAW_INT" and self.anchor is None:
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
        is_pos = msg.command in POSITION_COMMANDS
        if name == "COMMAND_LONG":
            if not is_pos or _unset(getattr(msg, lat_f), getattr(msg, lon_f)):
                return "pass"
            return "convert"
        if msg.frame not in GLOBAL_FRAMES:
            return "pass"
        if getattr(msg, lat_f) == 0 and getattr(msg, lon_f) == 0:
            return "pass"
        return "convert" if is_pos else "drop"
