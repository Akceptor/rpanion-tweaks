#!/usr/bin/env python3
import datetime
import json
import logging
import math
import os
import struct
import subprocess
import tempfile
from typing import NamedTuple

INJECT_MAX = 110

RESET_MASKS = {"hot": 0x0000, "warm": 0x0001, "warm-noana": 0x8001, "cold": 0xFFFF}


def ubx_frame(cls, msg_id, payload):
    body = struct.pack("<BBH", cls, msg_id, len(payload)) + payload
    a = b = 0
    for x in body:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return b"\xb5\x62" + body + bytes([a, b])


def valset_use_ana():
    # CFG-ANA-USE_ANA, layers RAM+BBR
    return ubx_frame(0x06, 0x8A, struct.pack("<BBBBIB", 0, 0x03, 0, 0, 0x10230001, 1))


def mga_ini_time_utc(now, acc_s=1, leap_s=18):
    if now.tzinfo is None or now.utcoffset() != datetime.timedelta(0):
        raise ValueError("now must be tz-aware UTC")
    payload = struct.pack(
        "<BBBbHBBBBBBIHHI",
        0x10, 0, 0, leap_s,
        now.year, now.month, now.day, now.hour, now.minute, now.second,
        0, now.microsecond * 1000,
        acc_s, 0, 0,
    )
    return ubx_frame(0x13, 0x40, payload)


def mga_ini_pos_llh(lat, lon, alt_m, acc_m):
    payload = struct.pack(
        "<BBHiiiI",
        0x01, 0, 0,
        round(lat * 1e7), round(lon * 1e7), round(alt_m * 100), round(acc_m * 100),
    )
    return ubx_frame(0x13, 0x40, payload)


def mon_ver_poll():
    return ubx_frame(0x0A, 0x04, b"")


def cfg_rst(nav_bbr_mask):
    return ubx_frame(0x06, 0x04, struct.pack("<HBB", nav_bbr_mask, 0x02, 0))


def to_inject(frame):
    if len(frame) > INJECT_MAX:
        raise ValueError("UBX frame too long for one GPS_INJECT_DATA")
    return len(frame), list(frame) + [0] * (INJECT_MAX - len(frame))


class Position(NamedTuple):
    lat: float
    lon: float
    alt_m: float
    acc_m: float
    source: str


def _valid_latlon(lat, lon):
    return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180


class FixCache:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            out = {k: float(d[k]) for k in ("lat", "lon", "alt_m", "t")}
        except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
            return None
        if not all(math.isfinite(v) for v in out.values()) or not _valid_latlon(out["lat"], out["lon"]):
            return None
        return out

    def save(self, lat, lon, alt_m, t):
        d = {"lat": round(lat, 2), "lon": round(lon, 2), "alt_m": alt_m, "t": t}
        dirname = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=dirname, prefix=".last_fix.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(d, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def delete(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def parse_manual(env):
    raw_lat = env.get("AGPS_APPROX_LAT")
    raw_lon = env.get("AGPS_APPROX_LON")
    if raw_lat is None or raw_lon is None:
        return None
    try:
        lat, lon = float(raw_lat), float(raw_lon)
        alt = float(env.get("AGPS_APPROX_ALT_M", "0"))
        if not _valid_latlon(lat, lon) or not math.isfinite(alt):
            raise ValueError
    except ValueError:
        logging.warning("agps: invalid AGPS_APPROX_* position ignored")
        return None
    return lat, lon, alt


def choose_position(cache, manual, now, max_age_s, cache_acc_m, manual_acc_m):
    if cache is not None and 0 <= now - cache["t"] <= max_age_s:
        return Position(cache["lat"], cache["lon"], cache["alt_m"], cache_acc_m, "cache")
    if manual is not None:
        return Position(manual[0], manual[1], manual[2], manual_acc_m, "manual")
    return None


def ntp_synced(run=subprocess.run):
    try:
        r = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    return r.returncode == 0 and r.stdout.strip() == "yes"


def build_assist(now, time_ok, pos, have_fix):
    frames = [valset_use_ana()]
    if have_fix:
        return frames
    if time_ok:
        frames.append(mga_ini_time_utc(now))
    if pos is not None:
        frames.append(mga_ini_pos_llh(pos.lat, pos.lon, pos.alt_m, pos.acc_m))
    return frames
