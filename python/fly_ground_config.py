"""Validated settings shared by the Raspberry Pi Ground service."""

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from pathlib import Path
from video_stream_state import STATE_FILE


CODE_RE = re.compile(r"CRAWLER(?:-[0-9A-HJKMNP-TV-Z]{4}){3}\Z")


def normalize_code(value):
    if not isinstance(value, str):
        raise ValueError("FLY_ROOM_CODE is required")
    code = value.strip().upper()
    if not CODE_RE.fullmatch(code):
        raise ValueError("FLY_ROOM_CODE must be CRAWLER-XXXX-XXXX-XXXX")
    return code


def room_id_for(code):
    return hashlib.sha256(normalize_code(code).encode("ascii")).hexdigest()


def _int(env, name, default, minimum=0, maximum=255):
    try:
        value = int(env.get(name, str(default)))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid {name}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return value


@dataclass(frozen=True)
class Settings:
    code: str
    room_id: str
    site_url: str
    rtsp_url: str
    router_url: str
    monitor_url: str
    channels: tuple[int, ...]
    throttle_channel: int
    gcs_sysid: int
    gcs_compid: int
    planner_sysid: int
    planner_compid: int
    session_seconds: int
    video_source: str = "rtsp"
    video_state_file: str = STATE_FILE

    @classmethod
    def from_env(cls, env):
        code = normalize_code(env.get("FLY_ROOM_CODE"))
        site = env.get("FLY_SITE_URL", "").rstrip("/")
        parsed = urlsplit(site)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("FLY_SITE_URL must be an HTTPS origin")
        rtsp = env.get("FLY_RTSP_URL", "rtsp://127.0.0.1:8554/video")
        if urlsplit(rtsp).scheme != "rtsp" or not urlsplit(rtsp).hostname:
            raise ValueError("invalid FLY_RTSP_URL")
        video_source = env.get("FLY_VIDEO_SOURCE", "rtsp")
        if video_source not in ("auto", "rtsp"):
            raise ValueError("FLY_VIDEO_SOURCE must be auto or rtsp")
        state_file = env.get("FLY_VIDEO_STATE_FILE", STATE_FILE)
        if not isinstance(state_file, str) or not Path(state_file).is_absolute():
            raise ValueError("FLY_VIDEO_STATE_FILE must be an absolute path")
        router = env.get("FLY_MAVLINK", "udpout:127.0.0.1:14552")
        if not router.startswith(("tcp:", "udpout:")):
            raise ValueError("FLY_MAVLINK must be a TCP or udpout pymavlink connection")
        monitor = env.get("FLY_MONITOR", "tcp:127.0.0.1:5760")
        if not monitor.startswith("tcp:"):
            raise ValueError("FLY_MONITOR must be a TCP pymavlink connection")
        try:
            channels = tuple(int(x.strip()) for x in env.get("FLY_CHANNELS", "1,3").split(","))
        except ValueError as exc:
            raise ValueError("invalid FLY_CHANNELS") from exc
        if not channels or len(set(channels)) != len(channels) or any(c < 1 or c > 16 for c in channels):
            raise ValueError("invalid FLY_CHANNELS")
        throttle = _int(env, "FLY_THROTTLE_CHANNEL", 3, 1, 16)
        if throttle not in channels:
            raise ValueError("throttle channel must be allowed")
        gcs_sysid = _int(env, "FLY_GCS_SYSID", 255, 1)
        gcs_compid = _int(env, "FLY_GCS_COMPID", 191, 1)
        planner_sysid = _int(env, "FLY_PLANNER_SYSID", 255, 1)
        planner_compid = _int(env, "FLY_PLANNER_COMPID", 190, 1)
        if (gcs_sysid, gcs_compid) == (planner_sysid, planner_compid):
            raise ValueError("Pi and Mission Planner MAVLink source IDs must differ")
        return cls(code, room_id_for(code), site, rtsp, router, monitor, channels, throttle, gcs_sysid,
                   gcs_compid, planner_sysid, planner_compid,
                   _int(env, "FLY_SESSION_SECONDS", 3600, 0, 86400), video_source, state_file)
