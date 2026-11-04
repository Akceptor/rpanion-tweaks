"""Signaling and session protocol for the Pi's GStreamer WebRTC peer."""

import json
from urllib.parse import quote, urlsplit


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


class PeerProtocol:
    def __init__(self, arbiter, send, session_seconds=3600):
        self.arbiter = arbiter
        self.send = send
        self.session_seconds = session_seconds
        self.started_ms = None
        self.offset_ms = None
        self.ping_id = 0
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
        self.send({"t": "ping", "id": self.ping_id, "t0": now_ms})

    def control(self, data, now_ms):
        if self.ended:
            return False
        return self.arbiter.accept_packet(data, now_ms, self.offset_ms)

    def message(self, value, now_ms):
        if not isinstance(value, dict) or self.ended:
            return False
        if value.get("t") == "pong" and value.get("id") == self.ping_id:
            t0, tp = value.get("t0"), value.get("tp")
            if isinstance(t0, (int, float)) and isinstance(tp, (int, float)) and t0 <= now_ms:
                self.offset_ms = tp - (t0 + now_ms) / 2
                return True
        if value.get("t") == "resume":
            granted = self.arbiter.resume(now_ms)
            self.state(now_ms)
            return granted
        if value.get("t") == "leave":
            self.close(now_ms)
            return True
        return False

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
        self.loop = asyncio.get_running_loop()
        self.Gst, self.GstSdp, self.GstWebRTC = Gst, GstSdp, GstWebRTC
        self.signal, self.protocol, self.on_closed = signal, protocol, on_closed
        self.pipeline = Gst.parse_launch(
            "rtspsrc name=source protocols=tcp latency=0 ! rtph264depay ! h264parse "
            "! rtph264pay config-interval=1 pt=96 ! queue max-size-buffers=4 leaky=downstream "
            "! application/x-rtp,media=video,encoding-name=H264,payload=96 ! webrtcbin name=webrtc bundle-policy=max-bundle")
        self.pipeline.get_by_name("source").set_property("location", rtsp_url)
        self.webrtc = self.pipeline.get_by_name("webrtc")
        self.webrtc.set_property("stun-server", "stun://stun.cloudflare.com:3478")
        for uri in turn_uris(ice_servers):
            self.webrtc.emit("add-turn-server", uri)
        self.webrtc.connect("on-ice-candidate", self._ice)
        self.webrtc.connect("notify::connection-state", self._connection)
        self.control = self.webrtc.emit("create-data-channel", "control", self._control_config())
        self.session = self.webrtc.emit("create-data-channel", "session", None)
        self.control.connect("on-message-data", self._control)
        self.session.connect("on-message-string", self._message)
        self.session.connect("on-open", self._opened)
        self.bus = self.pipeline.get_bus()
        self.pipeline.set_state(Gst.State.PLAYING)

    def _control_config(self):
        config = self.Gst.Structure.new_empty("data-channel")
        config.set_value("ordered", False)
        config.set_value("max-retransmits", 0)
        return config

    def _opened(self, _channel):
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

    def _connection(self, peer, _pspec):
        state = peer.get_property("connection-state")
        if state in (self.GstWebRTC.WebRTCPeerConnectionState.FAILED,
                     self.GstWebRTC.WebRTCPeerConnectionState.CLOSED):
            self.loop.call_soon_threadsafe(self.on_closed)

    def offer(self):
        promise = self.Gst.Promise.new_with_change_func(self._offer_ready, None, None)
        self.webrtc.emit("create-offer", None, promise)

    def _offer_ready(self, promise, _data):
        offer = promise.get_reply().get_value("offer")
        self.webrtc.emit("set-local-description", offer, self.Gst.Promise.new())
        self.signal({"type": "offer", "sdp": offer.sdp.as_text()})

    def on_signal(self, msg):
        if msg.get("type") == "answer":
            result, sdp = self.GstSdp.SDPMessage.new()
            if result != self.GstSdp.SDPResult.OK or self.GstSdp.sdp_message_parse_buffer(msg["sdp"].encode(), sdp) != self.GstSdp.SDPResult.OK:
                raise ValueError("invalid SDP answer")
            answer = self.GstWebRTC.WebRTCSessionDescription.new(self.GstWebRTC.WebRTCSDPType.ANSWER, sdp)
            self.webrtc.emit("set-remote-description", answer, self.Gst.Promise.new())
        elif msg.get("type") == "ice":
            candidate = msg.get("candidate", {})
            if isinstance(candidate.get("candidate"), str) and isinstance(candidate.get("sdpMLineIndex"), int):
                self.webrtc.emit("add-ice-candidate", candidate["sdpMLineIndex"], candidate["candidate"])

    def poll_bus(self):
        while True:
            msg = self.bus.timed_pop_filtered(0, self.Gst.MessageType.ERROR | self.Gst.MessageType.EOS)
            if msg is None:
                break
            if msg.type == self.Gst.MessageType.ERROR:
                error, _debug = msg.parse_error()
                raise RuntimeError(f"GStreamer: {error.message}")
            raise RuntimeError("RTSP stream ended")

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)
