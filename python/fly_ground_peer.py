"""Signaling and session protocol for the Pi's GStreamer WebRTC peer."""

import json
import logging
from urllib.parse import quote, urlsplit


LOG = logging.getLogger("fly-ground")


def signal_url(settings):
    return settings.site_url.replace("https://", "wss://", 1) + f"/room/{settings.room_id}?role=host"


def turn_uris(servers):
    result = []
    for server in servers:
        if not server.get("username") or not server.get("credential"):
            continue
        user = quote(server["username"], safe="")
        password = quote(server["credential"], safe="")
        for url in server.get("urls", []):
            parsed = urlsplit(url.replace("turn:", "turn://", 1).replace("turns:", "turns://", 1))
            if parsed.scheme in ("turn", "turns") and parsed.hostname:
                port = f":{parsed.port}" if parsed.port else ""
                result.append(f"{parsed.scheme}://{user}:{password}@{parsed.hostname}{port}")
    return result


def require_webrtc_plugins(gst):
    if not all(gst.ElementFactory.find(name) for name in ("nicesrc", "nicesink")):
        raise RuntimeError("GStreamer ICE plugin missing: install gstreamer1.0-nice")


def answer_media_summary(sdp):
    keep = ("m=", "a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive", "a=rtpmap:", "a=fmtp:")
    return " | ".join(line.strip() for line in sdp.splitlines() if line.startswith(keep))


PONG_WINDOW = 6
CLOCK_SAMPLES = 10


class PeerProtocol:
    def __init__(self, arbiter, send, session_seconds=3600):
        self.arbiter = arbiter
        self.send = send
        self.session_seconds = session_seconds
        self.started_ms = None
        self.offset_ms = None
        self.ping_id = 0
        self.sent_pings = {}
        self.clock_samples = []
        self.accepted = 0
        self.rejected = 0
        self.last_owner = None
        self.ended = False

    def open(self, now_ms):
        self.started_ms = now_ms
        self.arbiter.peer_connected = True
        self.state(now_ms)

    def remaining(self, now_ms):
        return None if self.session_seconds == 0 or self.started_ms is None else max(0, self.session_seconds * 1000 - (now_ms - self.started_ms))

    def state(self, now_ms):
        owner = self.arbiter.owner_at(now_ms)
        phase = "ended" if self.ended else "accepted"
        self.last_owner = owner
        self.send({"t": "state", "source": "pi", "phase": phase, "owner": owner,
                   "remoteArm": True, "throttleCap": 1, "remainingMs": self.remaining(now_ms),
                   "reason": "session expired" if self.ended else None})

    def ping(self, now_ms):
        self.ping_id += 1
        self.sent_pings[self.ping_id] = now_ms
        self.sent_pings.pop(self.ping_id - PONG_WINDOW, None)
        self.send({"t": "ping", "id": self.ping_id, "t0": now_ms})

    def control(self, data, now_ms):
        if self.ended:
            return False
        ok = self.arbiter.accept_packet(data, now_ms, self.offset_ms)
        if ok:
            self.accepted += 1
        else:
            self.rejected += 1
        return ok

    def message(self, value, now_ms):
        if not isinstance(value, dict) or self.ended:
            return False
        if value.get("t") == "pong":
            return self.pong(value, now_ms)
        if value.get("t") == "resume":
            granted = self.arbiter.resume(now_ms)
            a = self.arbiter
            throttle = a.last_channels[a.throttle_channel - 1] if a.last_channels else None
            LOG.info("Resume %s (owner %s, fresh %s, throttle %s, neutral 928-1056)",
                     "granted" if granted else "refused", a.owner_at(now_ms), a._fresh(now_ms), throttle)
            self.state(now_ms)
            return granted
        if value.get("t") == "leave":
            self.close(now_ms)
            return True
        return False

    def pong(self, value, now_ms):
        # Pings go out every 500 ms; on a loaded LTE link RTT exceeds that, so a pong can only
        # match an earlier ping. Keep the lowest-RTT recent sample to resist bufferbloat spikes.
        t0, tp = self.sent_pings.pop(value.get("id"), None), value.get("tp")
        if t0 is None or value.get("t0") != t0 or not isinstance(tp, (int, float)) or t0 > now_ms:
            return False
        self.clock_samples = (self.clock_samples + [(now_ms - t0, tp - (t0 + now_ms) / 2)])[-CLOCK_SAMPLES:]
        self.offset_ms = min(self.clock_samples)[1]
        return True

    def tick(self, now_ms):
        if self.ended or self.started_ms is None:
            return
        if self.remaining(now_ms) == 0:
            self.close(now_ms)
            return
        owner = self.arbiter.owner_at(now_ms)
        if owner != self.last_owner:
            self.state(now_ms)

    def close(self, now_ms):
        if not self.ended:
            self.ended = True
            self.arbiter.disconnect()
            if self.started_ms is not None:
                self.state(now_ms)


RTSP_STALL_MS = 5000
VIDEO_CAPS = "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"


class GstPeer:
    """One GStreamer peer; all methods run on the GLib/asyncio main thread."""

    def __init__(self, rtsp_url, ice_servers, signal, protocol, on_closed):
        import asyncio
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstWebRTC", "1.0")
        gi.require_version("GstSdp", "1.0")
        from gi.repository import Gst, GstSdp, GstWebRTC

        Gst.init(None)
        require_webrtc_plugins(Gst)
        self.loop = asyncio.get_running_loop()
        self.Gst, self.GstSdp, self.GstWebRTC = Gst, GstSdp, GstWebRTC
        self.signal, self.protocol, self.on_closed = signal, protocol, on_closed
        # Bound latency before packetization, on complete H.264 access units.
        # Leaking RTP packets can truncate frames during motion/bitrate bursts;
        # packets discarded there never reach webrtcbin's retransmission buffer.
        self.pipeline = Gst.parse_launch(
            "webrtcbin name=webrtc bundle-policy=max-bundle "
            "rtspsrc name=source protocols=tcp latency=0 ! rtph264depay name=depay ! h264parse "
            "! video/x-h264,alignment=au "
            "! queue name=q max-size-buffers=0 max-size-bytes=0 max-size-time=300000000 leaky=downstream "
            "! rtph264pay name=pay config-interval=1 pt=96 "
            f"! {VIDEO_CAPS} ! webrtc.")
        self.pipeline.get_by_name("source").set_property("location", rtsp_url)
        self.webrtc = self.pipeline.get_by_name("webrtc")
        self.webrtc.set_property("stun-server", "stun://stun.cloudflare.com:3478")
        for uri in turn_uris(ice_servers):
            self.webrtc.emit("add-turn-server", uri)
        # Without codec-preferences, create-offer fails until RTSP caps reach webrtcbin.
        video = self.webrtc.emit("get-transceiver", 0)
        if video is None:
            raise RuntimeError("webrtcbin has no video transceiver")
        video.set_property("direction", GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
        video.set_property("codec-preferences", Gst.Caps.from_string(VIDEO_CAPS))
        self.webrtc.connect("on-ice-candidate", self._ice)
        self.webrtc.connect("notify::connection-state", self._connection)
        self.webrtc.connect("notify::ice-connection-state", self._ice_state)
        # webrtcbin refuses create-data-channel until NULL->READY starts its internal thread.
        if self.pipeline.set_state(Gst.State.READY) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer pipeline failed to reach READY")
        self.control = self.webrtc.emit("create-data-channel", "control", self._control_config())
        self.session = self.webrtc.emit("create-data-channel", "session", None)
        if self.control is None or self.session is None:
            self.pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("webrtcbin did not create data channels")
        self.control.connect("on-message-data", self._control)
        self.session.connect("on-message-string", self._message)
        self.session.connect("on-open", self._opened)
        self.counts = {}
        self.counts_logged_ms = 0
        self.stall_check = (None, None)
        for name, pad in (("depay", "src"), ("pay", "src"), ("q", "src")):
            self._count(self.pipeline.get_by_name(name).get_static_pad(pad), name)
        self._count(self.pipeline.get_by_name("q").get_static_pad("sink"), "q-in")
        self.bus = self.pipeline.get_bus()
        self.pipeline.set_state(Gst.State.PLAYING)

    def _control_config(self):
        config = self.Gst.Structure.new_empty("data-channel")
        config.set_value("ordered", False)
        config.set_value("max-retransmits", 0)
        return config

    def _opened(self, _channel):
        LOG.info("Session channel open")
        self.loop.call_soon_threadsafe(self.protocol.open, self.now())

    @staticmethod
    def now():
        import time
        return int(time.monotonic() * 1000)

    def _control(self, _channel, data):
        self.loop.call_soon_threadsafe(self.protocol.control, data.get_data(), self.now())

    def _message(self, _channel, data):
        try:
            self.loop.call_soon_threadsafe(self.protocol.message, json.loads(data), self.now())
        except (ValueError, TypeError):
            pass

    def send_session(self, obj):
        if self.session.get_property("ready-state") == self.GstWebRTC.WebRTCDataChannelState.OPEN:
            if obj.get("t") == "telemetry" and self.session.get_property("buffered-amount") > 65536:
                return
            self.session.emit("send-string", json.dumps(obj, separators=(",", ":")))

    def _ice(self, _webrtc, index, candidate):
        self.signal({"type": "ice", "candidate": {"candidate": candidate, "sdpMLineIndex": index}})

    def _ice_state(self, peer, _pspec):
        LOG.info("ICE: %s", peer.get_property("ice-connection-state").value_nick)

    def _connection(self, peer, _pspec):
        state = peer.get_property("connection-state")
        LOG.info("WebRTC: %s", state.value_nick)
        if state in (self.GstWebRTC.WebRTCPeerConnectionState.FAILED,
                     self.GstWebRTC.WebRTCPeerConnectionState.CLOSED):
            self.loop.call_soon_threadsafe(self.on_closed)

    def offer(self):
        promise = self.Gst.Promise.new_with_change_func(self._offer_ready)
        self.webrtc.emit("create-offer", None, promise)

    def _offer_ready(self, promise, *_user_data):
        reply = promise.get_reply()
        offer = reply.get_value("offer") if reply is not None and reply.has_field("offer") else None
        if offer is None or offer.sdp is None:
            error = reply.get_value("error") if reply is not None and reply.has_field("error") else None
            LOG.error("create-offer failed: %s", error.message if error is not None else reply)
            self.loop.call_soon_threadsafe(self.on_closed)
            return
        self.webrtc.emit("set-local-description", offer, self.Gst.Promise.new())
        self.signal({"type": "offer", "sdp": offer.sdp.as_text()})
        LOG.info("Offer sent")

    def on_signal(self, msg):
        if msg.get("type") == "answer":
            result, sdp = self.GstSdp.SDPMessage.new()
            if result != self.GstSdp.SDPResult.OK or self.GstSdp.sdp_message_parse_buffer(msg["sdp"].encode(), sdp) != self.GstSdp.SDPResult.OK:
                raise ValueError("invalid SDP answer")
            answer = self.GstWebRTC.WebRTCSessionDescription.new(self.GstWebRTC.WebRTCSDPType.ANSWER, sdp)
            LOG.info("Answer media: %s", answer_media_summary(msg["sdp"]))
            promise = self.Gst.Promise.new_with_change_func(self._remote_set)
            self.webrtc.emit("set-remote-description", answer, promise)
        elif msg.get("type") == "ice":
            candidate = msg.get("candidate", {})
            if isinstance(candidate.get("candidate"), str) and isinstance(candidate.get("sdpMLineIndex"), int):
                self.webrtc.emit("add-ice-candidate", candidate["sdpMLineIndex"], candidate["candidate"])

    def _count(self, pad, name):
        self.counts[name] = 0
        self.counts[name + "-corrupted"] = 0
        self.counts[name + "-discont"] = 0

        def probe(_pad, info):
            if info.type & self.Gst.PadProbeType.BUFFER_LIST:
                batch = info.get_buffer_list()
                buffers = [batch.get(i) for i in range(batch.length())]
            else:
                buffers = [info.get_buffer()]
            for buffer in buffers:
                self.counts[name] += 1
                for suffix, flag in (("-corrupted", self.Gst.BufferFlags.CORRUPTED),
                                     ("-discont", self.Gst.BufferFlags.DISCONT)):
                    self.counts[name + suffix] += int(buffer.has_flags(flag))
            return self.Gst.PadProbeReturn.OK
        pad.add_probe(self.Gst.PadProbeType.BUFFER | self.Gst.PadProbeType.BUFFER_LIST, probe)

    def log_counts(self, now_ms):
        if now_ms - self.counts_logged_ms < 5000:
            return
        self.counts_logged_ms = now_ms
        caps = self.pipeline.get_by_name("pay").get_static_pad("src").get_current_caps()
        LOG.info("Video buffers %s; pay caps %s", self.counts,
                 caps.to_string() if caps is not None else None)

    def _remote_set(self, promise, *_user_data):
        reply = promise.get_reply()
        if reply is not None and reply.has_field("error"):
            LOG.error("set-remote-description failed: %s", reply.get_value("error").message)
        else:
            LOG.info("Remote description applied")

    def check_stall(self, now_ms):
        # Rpanion restarting its stream leaves rtspsrc connected but silent, with only a warning.
        count, since = self.stall_check
        if count != self.counts["depay"] or since is None:
            self.stall_check = (self.counts["depay"], now_ms)
        elif now_ms - since > RTSP_STALL_MS:
            raise RuntimeError(f"RTSP stalled: no video for {RTSP_STALL_MS // 1000}s")

    def poll_bus(self):
        Gst = self.Gst
        self.log_counts(self.now())
        self.check_stall(self.now())
        while True:
            msg = self.bus.timed_pop_filtered(
                0, Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING
                | Gst.MessageType.STATE_CHANGED)
            if msg is None:
                break
            if msg.type == Gst.MessageType.WARNING:
                warning, debug = msg.parse_warning()
                LOG.warning("GStreamer %s: %s (%s)", msg.src.get_name(), warning.message, debug)
                continue
            if msg.type == Gst.MessageType.STATE_CHANGED:
                if msg.src == self.pipeline:
                    _old, new, _pending = msg.parse_state_changed()
                    LOG.info("Pipeline: %s", new.value_nick)
                continue
            if msg.type == self.Gst.MessageType.ERROR:
                error, _debug = msg.parse_error()
                raise RuntimeError(f"GStreamer: {error.message}")
            raise RuntimeError("RTSP stream ended")

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)
