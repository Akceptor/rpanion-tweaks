"""MAVLink writer and read-only monitor for fly-by-webrtc Ground."""

from pymavlink.dialects.v20 import ardupilotmega as mavlink


TELEMETRY = frozenset({"HEARTBEAT", "SYS_STATUS", "BATTERY_STATUS", "ATTITUDE",
                       "GPS_RAW_INT", "GLOBAL_POSITION_INT", "RC_CHANNELS", "STATUSTEXT"})


def is_planner_heartbeat(msg, settings):
    return (msg.get_type() == "HEARTBEAT" and msg.get_srcSystem() == settings.planner_sysid
            and msg.get_srcComponent() == settings.planner_compid
            and msg.type == mavlink.MAV_TYPE_GCS)


class MavlinkAdapter:
    def __init__(self, writer, monitor, arbiter, settings, on_telemetry=None):
        self.writer = writer
        self.monitor = monitor
        self.arbiter = arbiter
        self.settings = settings
        self.on_telemetry = on_telemetry
        self.target = None
        self.last_heartbeat_ms = None
        self.last_override_ms = None

    def poll(self, now_ms):
        for _ in range(100):
            msg = self.monitor.recv_match(blocking=False)
            if msg is None:
                break
            kind = msg.get_type()
            if kind == "BAD_DATA":
                continue
            if is_planner_heartbeat(msg, self.settings):
                self.arbiter.planner_seen(now_ms)
                continue
            if (kind == "HEARTBEAT" and msg.get_srcComponent() == mavlink.MAV_COMP_ID_AUTOPILOT1
                    and msg.type != mavlink.MAV_TYPE_GCS
                    and msg.autopilot != mavlink.MAV_AUTOPILOT_INVALID):
                self.target = (msg.get_srcSystem(), msg.get_srcComponent())
            if self.target and msg.get_srcSystem() == self.target[0] and kind in TELEMETRY:
                if self.on_telemetry:
                    self.on_telemetry(msg)

    def send(self, now_ms):
        owner = self.arbiter.owner_at(now_ms)
        if not self.target:
            return
        release = self.arbiter.take_release()
        if release is not None:
            self.writer.mav.rc_channels_override_send(*self.target, *release)
        if owner != "browser":
            return
        if self.last_heartbeat_ms is None or now_ms - self.last_heartbeat_ms >= 1000:
            self.writer.mav.heartbeat_send(mavlink.MAV_TYPE_GCS, mavlink.MAV_AUTOPILOT_INVALID,
                                           0, 0, mavlink.MAV_STATE_ACTIVE)
            self.last_heartbeat_ms = now_ms
        if self.last_override_ms is None or now_ms - self.last_override_ms >= 20:
            values = self.arbiter.output(now_ms)
            if values is not None:
                self.writer.mav.rc_channels_override_send(*self.target, *values)
                self.last_override_ms = now_ms

    def writer_reconnected(self):
        was_connected = self.arbiter.peer_connected
        self.arbiter.disconnect()
        self.arbiter.peer_connected = was_connected
        self.target = None
        self.last_heartbeat_ms = None
        self.last_override_ms = None
