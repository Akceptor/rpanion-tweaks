#!/usr/bin/env python3
import argparse
import datetime
import json
import logging
import math
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import NamedTuple

from pymavlink.dialects.v20 import ardupilotmega as mavlink

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


def _number(env, name, default, scale=1.0):
    try:
        v = float(env.get(name, default))
    except ValueError:
        raise ValueError(f"invalid {name}: not a number") from None
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"invalid {name}: must be finite and >= 0")
    return v * scale


def _approx_acc_m(env):
    try:
        km = float(env.get("AGPS_APPROX_ACC_KM", "50"))
        if not 1 <= km <= 500:
            raise ValueError
    except ValueError:
        logging.warning("agps: invalid AGPS_APPROX_ACC_KM (want 1..500), using 50")
        km = 50.0
    return km * 1000


def _status_addr(env):
    raw = env.get("AGPS_STATUS", "127.0.0.1:14572")
    if raw == "":
        return None
    host, sep, port = raw.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("invalid AGPS_STATUS: expected host:port")
    return host, int(port)


class Settings(NamedTuple):
    enable: bool
    mavlink: str
    settle_s: float
    cache_enabled: bool
    cache_file: str
    cache_max_age_s: float
    cache_acc_m: float
    manual: object
    manual_acc_m: float
    status: object

    @classmethod
    def from_env(cls, env):
        return cls(
            enable=env.get("AGPS_ENABLE", "1") != "0",
            mavlink=env.get("AGPS_MAVLINK", "tcp:127.0.0.1:5760"),
            settle_s=_number(env, "AGPS_SETTLE_S", "15"),
            cache_enabled=env.get("AGPS_CACHE", "1") != "0",
            cache_file=env.get("AGPS_CACHE_FILE", "/var/lib/agps/last_fix.json"),
            cache_max_age_s=_number(env, "AGPS_CACHE_MAX_DAYS", "14", 86400),
            cache_acc_m=_number(env, "AGPS_CACHE_ACC_KM", "25", 1000),
            manual=parse_manual(env),
            manual_acc_m=_approx_acc_m(env),
            status=_status_addr(env),
        )


FC = (1, 1)
HEARTBEAT_TIMEOUT_S = 10
RESEND_AFTER_S = 30
CACHE_INTERVAL_S = 900
FRAME_GAP_S = 0.1
STATUS_INTERVAL_S = 1


def status_datagram(pos):
    return f"AGPS {pos.source} {round(pos.acc_m)} {pos.lat:.5f} {pos.lon:.5f}".encode("ascii")


def udp_status_sender(dest):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(data):
        try:
            sock.sendto(data, dest)
        except OSError:
            pass

    return send


class Runner:
    def __init__(self, conn, settings, cache, clock=time.monotonic, wall=time.time,
                 ntp=ntp_synced, sleep=time.sleep, status_send=None):
        self.conn = conn
        self.settings = settings
        self.cache = cache
        self.clock = clock
        self.wall = wall
        self.ntp = ntp
        self.sleep = sleep
        self.status_send = status_send
        self.armed = False
        self.last_save = None
        if not settings.cache_enabled:
            cache.delete()
        self._restart()

    def _restart(self):
        self.state = "WAIT_GPS"
        self.last_hb = None
        self.good_fix = False
        self.t_detect = None
        self.sent_t = None
        self.sent_count = 0
        self.fix_at_detect = False
        self.chosen_pos = None
        self.status_t = None

    def handle(self, m):
        if m.get_srcSystem() != FC[0] or m.get_srcComponent() != FC[1]:
            return
        kind = m.get_type()
        if kind == "HEARTBEAT":
            now = self.clock()
            if self.last_hb is not None and now - self.last_hb > HEARTBEAT_TIMEOUT_S:
                self._restart()
            self.last_hb = now
            self.armed = bool(m.base_mode & mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
        elif kind == "GPS_RAW_INT":
            self._on_gps(m)

    def _on_gps(self, m):
        now = self.clock()
        self.good_fix = m.fix_type >= 3 and m.lat != 0 and m.lon != 0
        if self.state == "WAIT_GPS" and m.fix_type >= 1:
            self.state = "SETTLING"
            self.t_detect = now
            self.fix_at_detect = self.good_fix
        if not self.good_fix:
            return
        if self.state == "SENT":
            self.state = "DONE"
            if not self.fix_at_detect:
                logging.info("agps: first 3D fix %ds after GPS detected", round(now - self.t_detect))
        if self.settings.cache_enabled and (self.last_save is None or now - self.last_save >= CACHE_INTERVAL_S):
            alt = getattr(m, "alt_ellipsoid", 0) or m.alt
            self.cache.save(m.lat / 1e7, m.lon / 1e7, alt / 1000.0, self.wall())
            self.last_save = now

    def tick(self):
        now = self.clock()
        if self.last_hb is not None and now - self.last_hb > HEARTBEAT_TIMEOUT_S and self.state != "WAIT_GPS":
            self._restart()
            return
        if self.state == "SETTLING" and now - self.t_detect >= self.settings.settle_s:
            self.send_bundle(self.good_fix)
            self.state = "SENT"
            self.sent_t = now
            self.sent_count = 1
        elif self.state == "SENT" and self.sent_count == 1 and now - self.sent_t >= RESEND_AFTER_S:
            self.send_bundle(self.good_fix)
            self.sent_count = 2
            self.sent_t = now
        if self.state == "SENT" and self.status_send and self.chosen_pos is not None and (
                self.status_t is None or now - self.status_t >= STATUS_INTERVAL_S):
            self.status_send(status_datagram(self.chosen_pos))
            self.status_t = now

    def send_bundle(self, have_fix):
        wall = self.wall()
        s = self.settings
        pos = None
        if not have_fix:
            cached = self.cache.load() if s.cache_enabled else None
            pos = choose_position(cached, s.manual, wall, s.cache_max_age_s, s.cache_acc_m, s.manual_acc_m)
        self.chosen_pos = pos
        time_ok = (not have_fix) and self.ntp()
        now = datetime.datetime.fromtimestamp(wall, datetime.timezone.utc)
        frames = build_assist(now, time_ok, pos, have_fix)
        for i, frame in enumerate(frames):
            if i:
                self.sleep(FRAME_GAP_S)
            n, data = to_inject(frame)
            self.conn.mav.gps_inject_data_send(FC[0], FC[1], n, data)
        parts = ["valset"] + (["time"] if time_ok else []) + ([f"pos={pos.source}"] if pos else [])
        logging.info("agps: sent %s", ",".join(parts))


WAIT_HEARTBEAT_S = 10
PROBE_WAIT_S = 5


def _from_fc(m):
    return m is not None and m.get_srcSystem() == FC[0] and m.get_srcComponent() == FC[1]


def _wait_for(conn, kind, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        m = conn.recv_match(type=kind, blocking=True, timeout=1)
        if _from_fc(m) and m.get_type() == kind:
            return m
    return None


def serve(conn, runner, stop):
    while not stop.is_set():
        m = conn.recv_match(blocking=True, timeout=0.5)
        if m is not None and m.get_type() != "BAD_DATA":
            runner.handle(m)
        runner.tick()


def main(argv=None, env=os.environ, connect=None):
    if connect is None:
        from pymavlink import mavutil
        connect = mavutil.mavlink_connection
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="agps")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sub.add_parser("probe")
    sub.add_parser("inject")
    sub.add_parser("reset").add_argument("mode", choices=sorted(RESET_MASKS))
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    try:
        settings = Settings.from_env(env)
    except ValueError as e:
        print(f"agps: {e}", file=sys.stderr)
        return 2
    if not settings.enable:
        return 0
    conn = connect(settings.mavlink, source_system=1, source_component=193, autoreconnect=True)
    cmd = args.cmd or "run"
    cache = FixCache(settings.cache_file)

    if cmd == "run":
        send = udp_status_sender(settings.status) if settings.status else None
        runner = Runner(conn, settings, cache, status_send=send)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        serve(conn, runner, stop)
        return 0

    hb = _wait_for(conn, "HEARTBEAT", WAIT_HEARTBEAT_S)
    if hb is None:
        print("agps: no heartbeat from the FC", file=sys.stderr)
        return 1
    if cmd == "probe":
        conn.mav.gps_inject_data_send(FC[0], FC[1], *to_inject(mon_ver_poll()))
        st = _wait_for(conn, "STATUSTEXT", PROBE_WAIT_S)
        if st is None:
            print("agps: no u-blox version reply", file=sys.stderr)
            return 1
        print(st.text)
        return 0
    if cmd == "reset":
        if hb.base_mode & mavlink.MAV_MODE_FLAG_SAFETY_ARMED:
            print("agps: refusing to reset the GPS while armed", file=sys.stderr)
            return 3
        conn.mav.gps_inject_data_send(FC[0], FC[1], *to_inject(cfg_rst(RESET_MASKS[args.mode])))
        return 0
    Runner(conn, settings, cache).send_bundle(have_fix=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
