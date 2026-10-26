#!/usr/bin/env python3
import json
import math
import os
import tempfile

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
            return Anchor(
                float(d["real_lat"]), float(d["real_lon"]),
                float(d["fake_lat"]), float(d["fake_lon"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

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
