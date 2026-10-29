#!/usr/bin/env python3
# -*- coding:utf-8 vi:ts=4:noexpandtab
# Simple RTSP server. Run as-is or with a command-line to replace the default pipeline
# Taken from https://github.com/tamaggo/gstreamer-examples/blob/master/test_gst_rtsp_server.py
# gst-launch-1.0 rtspsrc location=rtsp://127.0.0.1:8554/video latency=0 ! decodebin ! autovideosink

import argparse
import platform
import ipaddress
from typing import List
import subprocess
import os
import time
import threading
import socket
import struct
import fcntl
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtsp", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst, GstRtspServer, GLib


# Returns true if this is a Raspi5 or later
# https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#raspberry-pi-revision-codes
def is_pi_5_or_later() -> bool:
    cmd = "cat /proc/cpuinfo | awk '/Revision/ {print $3}'"
    revcode = subprocess.check_output(cmd, shell=True)

    if revcode == "":
        return False

    try:
        code = int(revcode, 16)
        new = (code >> 23) & 0x1
        model = (code >> 4) & 0xff
        # mem = (code >> 20) & 0x7

        if new and model >= 0x17:
            return True
        else:
            return False
    except:
        return False


def is_multicast(ip: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip)
        # Multicast addresses are in the range 224.0.0.0 to 239.255.255.255
        return ip_obj.is_multicast
    except ValueError:
        # If the IP address is not valid, return False
        return False


# Interface carrying the default route (ie the 4G modem), unless forced via STATS_IFACE
def get_default_iface() -> str:
    forced = os.environ.get("STATS_IFACE", "")
    if forced:
        return forced
    best = None
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) > 6 and fields[1] == "00000000":
                    metric = int(fields[6])
                    if best is None or metric < best[1]:
                        best = (fields[0], metric)
    except OSError:
        pass
    return best[0] if best else ""


def read_iface_bytes(iface: str):
    try:
        with open("/sys/class/net/{0}/statistics/rx_bytes".format(iface)) as f:
            rx = int(f.read())
        with open("/sys/class/net/{0}/statistics/tx_bytes".format(iface)) as f:
            tx = int(f.read())
        return rx, tx
    except (OSError, ValueError):
        return None


def read_cpu_times():
    try:
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
        idle = fields[3] + fields[4]
        return sum(fields), idle
    except (OSError, ValueError, IndexError):
        return None


def read_soc_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000
    except (OSError, ValueError):
        return None


def run_cmd(cmd: List[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def read_throttled() -> str:
    out = run_cmd(["vcgencmd", "get_throttled"])
    if "=" not in out:
        return ""
    flags = int(out.split("=")[1], 16)
    if flags & 0x1:
        return "UNDERVOLT"
    if flags & 0x6:
        return "THROTTLED"
    return ""


# Pi 5 only. Sums PMIC rails (V*I), so excludes USB peripherals such as the modem.
def read_pi5_power():
    out = run_cmd(["vcgencmd", "pmic_read_adc"])
    amps, volts = {}, {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2 or "=" not in parts[1]:
            continue
        name, value = parts[0], parts[1].split("=")[1]
        try:
            if name.endswith("_A") and value.endswith("A"):
                amps[name[:-2]] = float(value[:-1])
            elif name.endswith("_V") and value.endswith("V"):
                volts[name[:-2]] = float(value[:-1])
        except ValueError:
            pass
    rails = amps.keys() & volts.keys()
    if not rails:
        return None
    return sum(amps[r] * volts[r] for r in rails)


# ModemManager-managed modems only (QMI/MBIM). HiLink/RNDIS modems aren't visible here.
def read_modem_signal() -> str:
    out = run_cmd(["mmcli", "-m", "any", "--output-keyvalue"])
    quality, tech = None, ""
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, value = [s.strip() for s in line.split(":", 1)]
        if key == "modem.generic.signal-quality.value":
            quality = value
        elif key.startswith("modem.generic.access-technologies.value") and not tech:
            tech = value.upper()
    if quality is None:
        return ""
    return "{0} {1}%".format(tech, quality).strip()


def crc_x25(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        tmp = b ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


# msgid -> (CRC_EXTRA, base payload length)
MAVLINK_MSGS = {1: (124, 31), 24: (24, 30), 147: (154, 36)}


# Returns (msgid, compid, payload, sysid) for each valid SYS_STATUS / GPS_RAW_INT / BATTERY_STATUS frame in buf,
# ignoring garbage and everything else
def parse_mavlink_frames(buf: bytes):
    frames = []
    i = 0
    while i < len(buf):
        stx = buf[i]
        if stx == 0xFE and i + 6 <= len(buf):
            length = buf[i + 1]
            hdr_len, signature = 5, 0
            sysid, compid, msgid = buf[i + 3], buf[i + 4], buf[i + 5]
        elif stx == 0xFD and i + 10 <= len(buf):
            length = buf[i + 1]
            hdr_len = 9
            signature = 13 if buf[i + 2] & 0x01 else 0
            sysid, compid = buf[i + 5], buf[i + 6]
            msgid = int.from_bytes(buf[i + 7:i + 10], "little")
        else:
            i += 1
            continue

        end = i + 1 + hdr_len + length
        if msgid in MAVLINK_MSGS and end + 2 + signature <= len(buf):
            extra, base_len = MAVLINK_MSGS[msgid]
            crc = crc_x25(buf[i + 1:end] + bytes([extra]))
            if crc == int.from_bytes(buf[end:end + 2], "little"):
                # v2 drops trailing zero bytes of the payload
                payload = buf[i + 1 + hdr_len:end].ljust(base_len, b"\0")
                frames.append((msgid, compid, payload, sysid))
                i = end + 2 + signature
                continue
        i += 1
    return frames


def decode_sys_status(payload: bytes):
    mv, ca, rem = struct.unpack_from("<Hh", payload, 14) + struct.unpack_from("<b", payload, 30)
    return (None if mv == 65535 else mv / 1000,
            None if ca == -1 else ca / 100,
            None if rem == -1 else rem)


def decode_battery_status(payload: bytes):
    mah = struct.unpack_from("<i", payload, 0)[0]
    return payload[32], None if mah == -1 else mah


# (fix_type, satellites_visible, eph, lat degE7, lon degE7)
def decode_gps_raw(payload: bytes):
    lat, lon = struct.unpack_from("<ii", payload, 8)
    eph = struct.unpack_from("<H", payload, 20)[0]
    return payload[28], payload[29], eph, lat, lon


# Updates state in place from one parsed frame. Only the autopilot (compid 1) is trusted.
def apply_mavlink(state: dict, msgid: int, compid: int, payload: bytes, now: float, sysid=None):
    if compid != 1:
        return
    if sysid is not None:
        state["sysid"] = sysid
    if msgid == 1:
        state["volts"], state["amps"], state["pct"] = decode_sys_status(payload)
        state["ts"] = now
    elif msgid == 24:
        state["gps"] = decode_gps_raw(payload)
        state["gps_ts"] = now
    elif msgid == 147:
        batt_id, mah = decode_battery_status(payload)
        if batt_id == 0:
            state["mah"] = mah


def fc_fresh(state: dict, now: float) -> bool:
    return now - state.get("ts", -1e9) <= 5


GPS_FIX_NAMES = {0: "NO GPS", 1: "NO FIX", 2: "2D", 3: "3D", 4: "DGPS",
                 5: "RTK FLT", 6: "RTK FIX", 7: "STATIC", 8: "PPP"}


def gps_line(state: dict, now: float) -> str:
    if "gps" not in state or now - state["gps_ts"] > 5:
        return ""
    fix, sats, eph, lat, lon = state["gps"]
    parts = ["GPS", GPS_FIX_NAMES.get(fix, "FIX{0}".format(fix))]
    if sats != 255:
        parts.append("{0} sats".format(sats))
    if eph != 65535:
        parts.append("HDOP {0:.1f}".format(eph / 100))
    lines = [" ".join(parts)]
    if fix >= 2:
        lines.append("{0:.6f} {1:.6f}".format(lat / 1e7, lon / 1e7))
    return "\n".join(lines)


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


# BATTERY_STATUS (147) as a MAVLink v2 frame, battery id 1 (a second battery next to the FC's id 0)
def encode_battery_status(sysid: int, compid: int, seq: int, volts, amps, pct) -> bytes:
    mv = 65535 if volts is None else clamp(round(volts * 1000), 0, 65534)
    ca = -1 if amps is None else clamp(round(amps * 100), -32768, 32767)
    rem = -1 if pct is None else clamp(round(pct), 0, 100)
    # ArduPilot puts the whole pack voltage in cell 0 when cells are unknown
    payload = struct.pack("<iihH9HhBBBb", -1, -1, 32767, mv, *([65535] * 9), ca, 1, 0, 0, rem)
    hdr = bytes([len(payload), 0, 0, seq & 0xFF, sysid & 0xFF, compid & 0xFF]) + (147).to_bytes(3, "little")
    crc = crc_x25(hdr + payload + bytes([MAVLINK_MSGS[147][0]]))
    return b"\xFD" + hdr + payload + struct.pack("<H", crc)


def fc_line(state: dict, now: float) -> str:
    if not fc_fresh(state, now):
        return ""
    parts = []
    if state.get("volts") is not None:
        parts.append("{0:.1f}V".format(state["volts"]))
    if state.get("amps") is not None:
        parts.append("{0:.1f}A".format(state["amps"]))
    if state.get("pct") is not None:
        parts.append("{0}%".format(state["pct"]))
    if state.get("mah") is not None:
        parts.append("{0}mAh".format(state["mah"]))
    return " ".join(["FC"] + parts) if parts else ""


def parse_host_port(value, default):
    try:
        host, port = value.rsplit(":", 1)
        port = int(port)
        if host and 0 < port < 65536:
            return host, port
    except (AttributeError, ValueError):
        pass
    return default


# Sends frames over a TCP client connection to mavlink-router's TCP server. Whatever the router
# forwards to this connection is read and discarded so its send buffer never fills up.
# Reconnects at most every 10 s.
class Batt2Sender(threading.Thread):
    def __init__(self, target=None):
        super().__init__(daemon=True)
        default = ("127.0.0.1", 5760)
        if target is None:
            target = parse_host_port(os.environ.get("MAVLINK_BATT2_TCP"), default)
        self.host, self.port = target
        self.sock = None
        self.lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def run(self):
        while True:
            sock = None
            try:
                sock = socket.create_connection((self.host, self.port), timeout=3)
                sock.settimeout(2)
                with self.lock:
                    self.sock = sock
                while self.drain(sock):
                    pass
            except OSError:
                pass
            self.drop(sock)
            time.sleep(10)

    @staticmethod
    def drain(sock) -> bool:
        try:
            return bool(sock.recv(4096))
        except socket.timeout:
            return True

    def drop(self, sock):
        if sock is None:
            return
        with self.lock:
            if self.sock is sock:
                self.sock = None
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def send(self, frame: bytes):
        with self.lock:
            sock = self.sock
            if sock is None:
                return
            try:
                sock.sendall(frame)
                return
            except OSError:
                pass
        self.drop(sock)


# Reads the FC's telemetry from a UDP port that the user adds as a mavlink-router destination in
# Rpanion, and injects the Pi battery as battery 2 through a separate router endpoint (Batt2Sender)
class MavlinkBattery(threading.Thread):
    def __init__(self, port: int, enabled=None, compid=None):
        super().__init__(daemon=True)
        self.port = port
        self.state = {}
        self.enabled = env_number("MAVLINK_BATT2", 1) != 0 if enabled is None else enabled
        # Mission Planner only shows battery 2 from the autopilot (1/1), so we spoof it. mavlink-router
        # never forwards a message to an endpoint that has sent the same sysid/compid, so sending
        # this from the 14570 socket would cut it off from the FC's telemetry. A TCP connection is
        # its own endpoint, so only that one is affected. Side effect: our seq interleaves with the
        # FC's for 1/1, so Mission Planner's link quality / packet loss reads worse than reality.
        self.compid = env_number("MAVLINK_BATT2_COMPID", 1) if compid is None else compid
        self.sender = Batt2Sender()
        self.seq = 0

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("127.0.0.1", self.port))
        except OSError as e:
            print("Battery overlay: cannot bind UDP 127.0.0.1:{0} ({1}), FC battery disabled".format(self.port, e))
            return
        while True:
            data = sock.recv(2048)
            now = time.monotonic()
            for msgid, compid, payload, sysid in parse_mavlink_frames(data):
                apply_mavlink(self.state, msgid, compid, payload, now, sysid)

    def send_batt2(self, reading, now: float):
        sysid = self.state.get("sysid")
        if (not self.enabled or reading is None or sysid is None or self.sender is None
                or not self.sender.connected or not fc_fresh(self.state, now)):
            return
        frame = encode_battery_status(sysid, self.compid, self.seq, *reading)
        self.seq = (self.seq + 1) & 0xFF
        self.sender.send(frame)


def apply_lima(state: dict, data: bytes, now: float):
    if data in (b"LIMA 0", b"LIMA 1"):
        state["on"] = data == b"LIMA 1"
        state["ts"] = now


def lima_line(state: dict, now: float) -> str:
    on = state.get("on") and now - state["ts"] <= 3
    return "Lima: ON" if on else "Lima: OFF"


# Listens for the status datagrams that lima.py sends once per second
class LimaStatus(threading.Thread):
    def __init__(self, port: int):
        super().__init__(daemon=True)
        self.port = port
        self.state = {}

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("127.0.0.1", self.port))
        except OSError as e:
            print("Lima overlay: cannot bind UDP 127.0.0.1:{0} ({1}), Lima status disabled".format(self.port, e))
            return
        while True:
            apply_lima(self.state, sock.recv(64), time.monotonic())


# Returns (bus volts, amps, shunt saturated). Raw shunt is signed, 2.5 uV/LSB;
# bus is unsigned, 1.25 mV/LSB. Full scale is 81.92 mV, so a clipped reading is flagged.
def ina226_convert(bus_raw: int, shunt_raw: int, shunt_ohms: float):
    volts = bus_raw * 1.25e-3
    amps = shunt_raw * 2.5e-6 / shunt_ohms
    return volts, amps, abs(shunt_raw) >= 32767


LIPO_CURVE = [(4.20, 100), (4.15, 95), (4.11, 90), (4.08, 85), (4.02, 80), (3.98, 75), (3.95, 70),
              (3.91, 65), (3.87, 60), (3.85, 55), (3.84, 50), (3.82, 45), (3.80, 40), (3.79, 35),
              (3.77, 30), (3.75, 25), (3.73, 20), (3.71, 15), (3.69, 10), (3.61, 5), (3.27, 0)]


def lipo_percent(cell_volts: float) -> float:
    if cell_volts >= LIPO_CURVE[0][0]:
        return 100
    for (v_hi, p_hi), (v_lo, p_lo) in zip(LIPO_CURVE, LIPO_CURVE[1:]):
        if cell_volts >= v_lo:
            return p_lo + (p_hi - p_lo) * (cell_volts - v_lo) / (v_hi - v_lo)
    return 0


def pack_percent(volts: float, cells: int):
    if cells <= 0:
        return None
    return lipo_percent(volts / cells)


def format_pi_line(volts, amps, saturated, pct) -> str:
    if volts is None:
        return ""
    parts = ["{0:.1f}V".format(volts), "{0:.1f}A{1}".format(amps, "+" if saturated else "")]
    if pct is not None:
        parts.append("{0:.0f}%".format(pct))
    return "PI " + " ".join(parts)


def env_number(name: str, default):
    try:
        value = os.environ.get(name)
        if value is None:
            return default
        return int(value, 0) if isinstance(default, int) else float(value)
    except ValueError:
        return default


# Minimal INA226 reader over /dev/i2c-N using only os/fcntl. Reopens on error, at most every 10 s.
class Ina226:
    I2C_SLAVE = 0x0703

    def __init__(self):
        self.bus = env_number("INA226_BUS", 1)
        self.addr = env_number("INA226_ADDR", 0x40)
        self.shunt_ohms = env_number("INA226_SHUNT_OHMS", 0.1)
        if self.shunt_ohms <= 0:
            self.shunt_ohms = 0.1
        self.cells = env_number("PI_BATT_CELLS", 2)
        self.last = None  # (volts, amps, pct) from the latest line() call, None if unreadable
        self.fd = None
        self.next_try = 0.0

    def _read(self, reg: int) -> int:
        os.write(self.fd, bytes([reg]))
        return int.from_bytes(os.read(self.fd, 2), "big")

    def line(self, now: float) -> str:
        self.last = None
        if self.fd is None:
            if now < self.next_try:
                return ""
            self.next_try = now + 10
            try:
                self.fd = os.open("/dev/i2c-{0}".format(self.bus), os.O_RDWR)
                fcntl.ioctl(self.fd, self.I2C_SLAVE, self.addr)
                os.write(self.fd, bytes([0x00, 0x45, 0x27]))
            except OSError:
                self.close()
                return ""
        try:
            shunt = self._read(0x01)
            bus = self._read(0x02)
        except OSError:
            self.close()
            return ""
        if shunt >= 0x8000:
            shunt -= 0x10000
        volts, amps, saturated = ina226_convert(bus, shunt, self.shunt_ohms)
        pct = pack_percent(volts, self.cells)
        self.last = (volts, amps, pct)
        return format_pi_line(volts, amps, saturated, pct)

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None


# Single background sampler shared by all streams, so slow commands (vcgencmd, mmcli)
# never block the GLib main loop that drives the RTSP server
class SystemStats(threading.Thread):
    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = SystemStats()
            cls._instance.start()
            cls._instance.mavlink.start()
            if cls._instance.mavlink.enabled:
                cls._instance.mavlink.sender.start()
            cls._instance.lima.start()
        return cls._instance

    def __init__(self):
        super().__init__(daemon=True)
        self.lines = ["", ""]
        self.batt_lines = ["", ""]
        self.gps_text = ""
        self.mavlink = MavlinkBattery(env_number("STATS_MAVLINK_PORT", 14570))
        self.lima = LimaStatus(env_number("STATS_LIMA_PORT", 14571))
        self.ina = Ina226()

    def run(self):
        iface, last_bytes, last_cpu = "", None, read_cpu_times()
        last_time = time.monotonic()
        slow = {"throttle": "", "power": None, "signal": ""}
        tick = 0
        while True:
            time.sleep(1)
            now = time.monotonic()
            dt = now - last_time
            last_time = now

            cur_iface = get_default_iface()
            cur_bytes = read_iface_bytes(cur_iface) if cur_iface else None
            if cur_bytes is None:
                net = "no link"
            elif cur_iface != iface or last_bytes is None:
                net = "{0} ...".format(cur_iface)
            else:
                down = (cur_bytes[0] - last_bytes[0]) * 8 / dt / 1e6
                up = (cur_bytes[1] - last_bytes[1]) * 8 / dt / 1e6
                net = "{0} ↓{1:.2f} ↑{2:.2f} Mbps".format(cur_iface, down, up)
            iface, last_bytes = cur_iface, cur_bytes

            if tick % 5 == 0:
                slow["throttle"] = read_throttled()
                slow["power"] = read_pi5_power()
                slow["signal"] = read_modem_signal()
            tick += 1

            sys_parts = []
            cpu = read_cpu_times()
            if cpu and last_cpu and cpu[0] > last_cpu[0]:
                busy = 1 - (cpu[1] - last_cpu[1]) / (cpu[0] - last_cpu[0])
                sys_parts.append("CPU {0:.0f}%".format(busy * 100))
            last_cpu = cpu
            temp = read_soc_temp()
            if temp is not None:
                sys_parts.append("{0:.0f}°C".format(temp))
            if slow["power"] is not None:
                sys_parts.append("{0:.1f}W".format(slow["power"]))
            if slow["throttle"]:
                sys_parts.append(slow["throttle"])

            if slow["signal"]:
                net += " | " + slow["signal"]
            self.lines = [net, " | ".join(sys_parts)]
            self.batt_lines = [fc_line(self.mavlink.state, now), self.ina.line(now)]
            self.gps_text = gps_line(self.mavlink.state, now)
            self.mavlink.send_batt2(self.ina.last, now)


# Updates the "statsoverlay" textoverlay (if present) once per second with
# system stats and actual FPS. Returns the GLib timer id, or None.
def attach_stats(pipeline):
    overlay = pipeline.get_by_name("statsoverlay")
    if overlay is None:
        return None
    battoverlay = pipeline.get_by_name("battoverlay")
    limaoverlay = pipeline.get_by_name("limaoverlay")
    gpsoverlay = pipeline.get_by_name("gpsoverlay")

    stats = SystemStats.get()
    frames = [0]

    def count_frame(pad, info):
        frames[0] += 1
        return Gst.PadProbeReturn.OK

    overlay.get_static_pad("video_sink").add_probe(Gst.PadProbeType.BUFFER, count_frame)

    state = {"time": time.monotonic()}

    def tick():
        now = time.monotonic()
        dt = now - state["time"]
        state["time"] = now
        fps = frames[0] / dt if dt > 0 else 0
        frames[0] = 0

        net, system = stats.lines
        system = "{0} | {1:.0f} fps".format(system, fps) if system else "{0:.0f} fps".format(fps)
        overlay.set_property("text", net + "\n" + system)
        if battoverlay is not None:
            battoverlay.set_property("text", "\n".join(l for l in stats.batt_lines if l))
        if limaoverlay is not None:
            limaoverlay.set_property("text", lima_line(stats.lima.state, now))
        if gpsoverlay is not None:
            gpsoverlay.set_property("text", stats.gps_text)
        return True

    return GLib.timeout_add(1000, tick)


def getPipeline(device, height, width, bitrate, format, rotation, framerate, timestamp, compression) -> str:
    pipeline: List[str] = []

    # -1 is no framerate specified
    if framerate == -1:
        framestr = ""
    else:
        framestr = ",framerate={0}/1".format(framerate)

    # start with device
    if device == "testsrc":
        pipeline.append("videotestsrc pattern=ball")
        pipeline.append("video/x-raw,width={0},height={1}{2}".format(width, height, framestr))
    elif device.startswith("rtsp://"):
        # rtsp streaming source
        pipeline.append("rtspsrc location=\"{0}\" is-live=true latency=0 udp-buffer-size=212992".format(device))
        if format == "video/x-h264":
            pipeline.append("rtph264depay")
        elif format == "video/x-h265":
            pipeline.append("rtph265depay")
        else:
            print("Error: Need to specify video/x-h264 or video/x-h265 for rtsp source in --format")
            return ""
    elif device in ["argus0", "argus1"]:
        pipeline.append("nvarguscamerasrc sensor-id={0}".format(device[-1]))
        pipeline.append("video/x-raw(memory:NVMM),width={0},height={1},format=NV12{2}".format(width, height, framestr))
    elif device in ["0rpicam", "1rpicam"]:
        # Old (Buster and earlier) can use the rpicamsrc interface
        ts = ""
        if timestamp:
            ts = "annotation-mode=12 annotation-text-colour=0"
        pipeline.append("rpicamsrc {2} bitrate={0} rotation={1} camera-number={3} preview=false".format(
            bitrate*1000, rotation, ts, device[0]))
        pipeline.append("video/x-h264,width={0},height={1}{2}".format(
            width, height, framestr))
    elif device.startswith("/base/soc/i2c") or device.startswith("/base/axi/pcie"):
        # Bullseye uses the new libcamera interface ... so need a different pipeline
        # Note that the Pi5 uses a different format
        if is_pi_5_or_later():
            format = "RGBx"
        else:
            format = "I420"  # https://forums.raspberrypi.com/viewtopic.php?t=93560
        pipeline.append("libcamerasrc camera-name={0}".format(device))
        pipeline.append("capsfilter caps=video/x-raw,width={0},height={1},format={3}{2}".format(width, height, framestr, format))
        pipeline.append("queue max-size-buffers=3 leaky=downstream")
    elif format == "video/x-raw":
        # Use io-mode=2 (mmap) for better performance
        pipeline.append("v4l2src device={0} io-mode=2".format(device))
        pipeline.append("videorate drop-only=true")
        pipeline.append("{2},width={0},height={1}{3}".format(width, height, format, framestr))
        pipeline.append("queue max-size-buffers=2 leaky=downstream")
    elif format == "video/x-h264":
        pipeline.append("v4l2src device={0}".format(device))
        pipeline.append("{2},width={0},height={1}{3}".format(width, height, format, framestr))
    elif format == "image/jpeg":
        # Use io-mode=2 (mmap) for better performance with JPEG sources
        pipeline.append("v4l2src device={0} io-mode=2".format(device))
        # Drop frames immediately if processing can't keep up
        pipeline.append("videorate drop-only=true max-rate={0}".format(
            framerate if framerate != -1 else 30))
        pipeline.append("{2},width={0},height={1}{3}".format(
            width, height, format, framestr))
        # Reduce buffer to 2 and make it leaky to drop old frames faster
        pipeline.append("queue max-size-buffers=2 leaky=downstream")
        # Use hardware JPEG decoder if available, otherwise software
        if Gst.ElementFactory.find("v4l2jpegdec"):
            # Hardware decoder with output buffer optimization
            pipeline.append("v4l2jpegdec capture-io-mode=4")
        else:
            # Software decoder - allow error recovery
            pipeline.append("jpegdec max-errors=-1")
        # Output format from decoder - use I420 for better encoder compatibility
        pipeline.append("videoconvert n-threads=4")
        pipeline.append("video/x-raw,format=I420")
        # Add another small queue after decode to prevent blocking
        pipeline.append("queue max-size-buffers=2 leaky=downstream")
    else:
        print("Bad camera")
        return ""

    # now for rotations, overlays and compression, if required. Note we can't modify an x264 source stream
    if format not in ["video/x-h264", "video/x-h265"] and not device.startswith("rtsp://"):
        # now add rotations for not-jetson and not-legacy-pi-camera
        if device not in ["0rpicam", "1rpicam"] and 'tegra' not in platform.uname().release:
            if rotation == 90:
                pipeline.append("videoflip video-direction=90r")
            elif rotation == 180:
                pipeline.append("videoflip video-direction=180")
            elif rotation == 270:
                pipeline.append("videoflip video-direction=90l")

        # and then timestamps
        if timestamp and device not in ["0rpicam", "1rpicam"] and 'tegra' not in platform.uname().release:
            pipeline.append("videoconvert")
            pipeline.append("clockoverlay time-format=\"%d-%b-%Y %H:%M:%S\" valignment=top halignment=left "
                            "font-desc=\"Sans 10\" shaded-background=true ypad=10")
            pipeline.append("textoverlay name=statsoverlay text=\"\" valignment=top halignment=right line-alignment=right "
                            "font-desc=\"Sans 10\" shaded-background=true ypad=10")
            pipeline.append("textoverlay name=battoverlay text=\"\" valignment=bottom halignment=left line-alignment=left "
                            "font-desc=\"Sans 10\" shaded-background=true ypad=10")
            pipeline.append("textoverlay name=limaoverlay text=\"\" valignment=bottom halignment=center "
                            "font-desc=\"Sans 10\" shaded-background=true ypad=10")
            pipeline.append("textoverlay name=gpsoverlay text=\"\" valignment=bottom halignment=right line-alignment=right "
                            "font-desc=\"Sans 10\" shaded-background=true ypad=10")

        # 3 options for H264: Rpi hardware compression (v4l2h264enc), Jetson hardware compression (nvv4l2h264enc)
        # or software compression (x264enc)
        # 2 options for H265: Jetson hardware compression (nvv4l2h265enc) or software compression (x265enc)
        # Use v4l2-ctl -d 11 --list-ctrls-menu to get v4l2h264enc options
        if (Gst.ElementFactory.find("nvv4l2h264enc") and compression == "H264") or (Gst.ElementFactory.find("nvv4l2h265enc") and compression == "H265"):
            # Jetson, with h/w rotation
            if rotation == 90:
                devrotation = "flip-method=3"
            elif rotation == 180:
                devrotation = "flip-method=2"
            elif rotation == 270:
                devrotation = "flip-method=1"
            else:
                devrotation = ""
            pipeline.append("nvvidconv {0}".format(devrotation))
            if timestamp:
                pipeline.append("clockoverlay time-format=\"%d-%b-%Y %H:%M:%S\"")
                pipeline.append("nvvidconv")
            if compression == "H265":
                pipeline.append("nvv4l2h265enc bitrate={0} iframeinterval=5 preset-level=1 insert-sps-pps=true".format(bitrate*1000))
                pipeline.append("h265parse")
            elif compression == "H264":
                pipeline.append("nvv4l2h264enc bitrate={0} iframeinterval=5 preset-level=1 insert-sps-pps=true".format(bitrate*1000))
                pipeline.append("h264parse")
        elif Gst.ElementFactory.find("v4l2h264enc") and compression == "H264":
            # Pi or similar arm platforms running on RasPiOS. Note that Pi5 onwards don't support hardware encoding
            # Only use a higher h264 level if the bitrate requires it. I find that level 4.1 can be a little
            # crashy sometimes.
            if bitrate > 20000:
                level = "4.1"
            else:
                level = "4"
            pipeline.append("videoconvert")
            pipeline.append("v4l2h264enc extra-controls=\"controls,repeat_sequence_header=1,h264_profile=4,video_bitrate={0},h264_i_frame_period=5\"".format(bitrate*1000))
            pipeline.append("video/x-h264,profile=high,level=(string){0}".format(level))
            pipeline.append("h264parse")
        else:
            # s/w encoder - x86, Pi5, etc
            pipeline.append("videoconvert")
            if is_pi_5_or_later() and compression == "H264":
                pipeline.append("video/x-raw,format=NV12")
            else:
                pipeline.append("video/x-raw,format=I420")
            # testcamerasrc doesn't like leaky queues
            if device != "testsrc":
                pipeline.append("queue max-size-buffers=2 leaky=downstream")
            else:
                pipeline.append("queue max-size-buffers=2")
            if compression == "H264":
                # Use multiple threads for software encoding
                pipeline.append("x264enc tune=zerolatency bitrate={0} speed-preset=superfast key-int-max=25 threads=0".format(bitrate))
            elif compression == "H265":
                pipeline.append("x265enc tune=zerolatency bitrate={0} speed-preset=superfast key-int-max=25".format(bitrate))

        # final rtp formatting
        pipeline.append("queue")
        if compression == "H264":
            pipeline.append("rtph264pay config-interval=1 name=pay0 pt=96")
        elif compression == "H265":
            pipeline.append("rtph265pay config-interval=1 name=pay0 pt=96")
    else:
        # just need to do rtp payloader for pre-compressed streams
        pipeline.append("queue")
        if format == "video/x-h264":
            pipeline.append("rtph264pay config-interval=1 name=pay0 pt=96")
        elif format == "video/x-h265":
            pipeline.append("rtph265pay config-interval=1 name=pay0 pt=96")

    # return as full string
    print(" ! ".join(pipeline))
    return " ! ".join(pipeline)


class MyFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(self, device, h, w, bitrate, format, rotation, framerate, timestamp, compression):
        GstRtspServer.RTSPMediaFactory.__init__(self)
        self.device = device
        self.height = h
        self.width = w
        self.bitrate = bitrate
        self.format = format
        self.rotation = rotation
        self.framerate = framerate
        self.timestamp = timestamp
        self.compression = compression

        # Configure for low latency streaming
        self.set_latency(0)  # Minimize latency
        self.set_buffer_size(0)  # Use default but don't accumulate
        self.set_transport_mode(GstRtspServer.RTSPTransportMode.PLAY)

    def do_create_element(self, url):
        pipeline_str = getPipeline(self.device, self.height, self.width, self.bitrate, self.format, self.rotation,
                                   self.framerate, self.timestamp, self.compression)
        return Gst.parse_launch(pipeline_str)

    def do_configure(self, media):
        # Configure the media for each client connection
        # This is called when a client connects
        self.set_eos_shutdown(True)  # Clean shutdown on EOS

        timer = attach_stats(media.get_element())
        if timer is not None:
            media.connect("unprepared", lambda m: GLib.source_remove(timer))


class GstServer():
    def __init__(self):
        self.server = GstRtspServer.RTSPServer()

        # Configure server for low-latency streaming
        self.server.set_backlog(5)  # Limit queued connections

        self.sourceID = self.server.attach(None)
        print("Server available on rtsp://<IP>:8554")

    def addStream(self, device, h, w, bitrate, format, rotation, framerate, timestamp, compression):
        f = MyFactory(device, h, w, bitrate, format,
                      rotation, framerate, timestamp,
                      compression)

        # Don't share the media pipeline - each client gets their own
        # This prevents one slow client from affecting others
        f.set_shared(False)

        # Enable clock synchronization for smoother playback
        f.set_clock(None)  # Use default system clock

        m = self.server.get_mount_points()
        if not device.startswith("rtsp://"):
            name = ''.join(filter(str.isalnum, device))
        else:
            # remove any rtsp username or passwords, format rtsp://admin:admin@192.168.1.217:554/11
            if "@" in device:
                name = device.split('@')[1]
            else:
                name = device.replace("rtsp://", "")
            name = ''.join(filter(str.isalnum, name))
        m.add_factory("/" + name, f)

        print("Added " + "rtsp://<IP>:8554/" + name)
        print("Use: gst-launch-1.0 rtspsrc location=rtsp://<IP>:8554/" +
              name + " latency=0 ! queue ! decodebin ! autovideosink sync=false")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="RTSP Server using Gstreamer")
    parser.add_argument("--videosource", help="Video Device. Can be device (/dev/video0) or rtsp source (rtsp://192.168.1.100:8554/stream)",
                        default="/dev/video0", type=str)
    parser.add_argument("--height", help="Height", default=480, type=int)
    parser.add_argument("--width", help="Width", default=640, type=int)
    parser.add_argument("--fps", help="Framerate", default=10, type=int)
    parser.add_argument(
        "--bitrate", help="Max bitrate (kbps)", default=2000, type=int)
    parser.add_argument("--format", help="Video format",
                        default="video/x-raw", type=str)
    parser.add_argument("--compression", help="encoder choice",
                        default='H264', type=str, choices=['H264', 'H265'])
    parser.add_argument("--rotation", help="rotation angle",
                        default=0, type=int, choices=[0, 90, 180, 270])
    parser.add_argument("--transport", help="Transport protocol selection",
                        default="RTSP", type=str, choices=['RTSP', 'RTP'])
    parser.add_argument(
        "--udp", help="If using RTP, the destinatinon IP:port", default="127.0.0.1:5600", type=str)
    parser.add_argument(
        "--multirtsp", help="CSV of multi-camera RTSP setup. Format is videosource,height,width,bitrate,formatstr,rotation, fps;source2,etc", default="", type=str)
    parser.add_argument("--timestamp", help="add timestamp",
                        default=False, action='store_true')
    args = parser.parse_args()

    loop = GLib.MainLoop()
    Gst.init(None)

    Gst.debug_set_active(True)
    Gst.debug_set_default_threshold(3)

    # Started here, not on the first RTSP viewer, so battery 2 reaches the GCS even when no one watches the video
    SystemStats.get()

    if args.multirtsp != "":
        # Multi-camera streaming, delimited via ';'
        # Example commandline is:
        # ./video-server.py --multirtsp="/dev/video0,480,640,2000,video/x-raw,0,10;/dev/video2,480,640,2000,video/x-raw,0,10"

        cams = args.multirtsp.split(';')
        s = GstServer()

        # Add each camera
        for cam in cams:
            try:
                (videosource, height, width, bitrate, formatstr,
                 rotation, fps, timestamp) = cam.split(',')
            except:
                print("Bad format: " + cam)
                break
            if not (height.isdigit() and width.isdigit() and bitrate.isdigit() and rotation.isdigit() and fps.isdigit()):
                print("Bad format: " + cam)
                break
            s.addStream(videosource, height, width, bitrate,
                        formatstr, rotation, fps, timestamp, args.compression)

        try:
            loop.run()
        except:
            print("Exiting RTSP Server")
            loop.quit()
    elif args.transport == "RTSP":
        # RTSP
        s = GstServer()
        s.addStream(args.videosource, args.height, args.width, args.bitrate,
                    args.format, args.rotation, args.fps, args.timestamp, args.compression)

        try:
            loop.run()
        except:
            print("Exiting RTSP Server")
            loop.quit()
    elif args.transport == "RTP":
        # RTP
        pipeline_str = getPipeline(args.videosource, args.height, args.width,
                                   args.bitrate, args.format, args.rotation, args.fps, args.timestamp,
                                   args.compression)
        pipeline_str += " ! udpsink host={0} port={1}".format(
            args.udp.split(':')[0], args.udp.split(':')[1])
        if is_multicast(args.udp.split(':')[0]):
            pipeline_str += " auto-multicast=true"
        pipeline = Gst.parse_launch(pipeline_str)
        attach_stats(pipeline)
        pipeline.set_state(Gst.State.PLAYING)

        print("Server sending UDP stream to " + args.udp)
        if args.compression == "H264" or (args.videosource.startswith("rtsp://") and args.format == "video/x-h264"):
            print(
                "Use: gst-launch-1.0 udpsrc port={0} caps='application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H264' ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! autovideosink sync=false".format(args.udp.split(':')[1]))
        elif args.compression == "H265" or (args.videosource.startswith("rtsp://") and args.format == "video/x-h265"):
            print(
                "Use: gst-launch-1.0 udpsrc port={0} caps='application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265' ! rtpjitterbuffer ! rtph265depay ! h265parse ! avdec_h265 ! videoconvert ! autovideosink sync=false".format(args.udp.split(':')[1]))

        try:
            loop.run()
        except:
            print("Exiting UDP Server")
            pipeline.set_state(Gst.State.NULL)
            loop.quit()
