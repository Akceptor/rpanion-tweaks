#!/usr/bin/env python3
import datetime
import struct

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
