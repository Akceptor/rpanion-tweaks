#!/usr/bin/env python3
"""Unattended Pi Ground: Rpanion RTP/RTSP to WebRTC, browser RC to MAVLink."""

import asyncio
import json
import logging
import os
import signal
import time
from urllib.request import Request, urlopen

from fly_ground_config import Settings
from fly_ground_control import Arbiter
from fly_ground_mavlink import MavlinkAdapter
from fly_ground_peer import GstPeer, PeerProtocol, signal_url
from fly_ground_video import SourceWatcher, VideoSourceChanged, wait_for_source


LOG = logging.getLogger("fly-ground")
STUN_FALLBACK = [{"urls": "stun:stun.cloudflare.com:3478"}]
# Worker close codes: host already present, room locked/closed, join rate limit.
ROOM_REJECT_CODES = {4001, 4009, 4029}
REJECT_DELAY = 30


class RoomRejected(ConnectionError):
    def __init__(self, code):
        super().__init__(f"signaling server rejected host (close code {code})")
        self.code = code


def next_delay(delay, exc):
    # Each rejected join counts toward the worker's per-IP limiter, so back off hard.
    if isinstance(exc, RoomRejected):
        return REJECT_DELAY, 1
    return delay, min(delay * 2, 30)


def now_ms():
    return int(time.monotonic() * 1000)


def ice_servers(site_url):
    try:
        with urlopen(Request(site_url + "/turn", headers={"Accept": "application/json"}), timeout=15) as response:
            servers = json.load(response)["iceServers"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("TURN unavailable (%s); using STUN only", type(exc).__name__)
        return STUN_FALLBACK
    if isinstance(servers, list) and any(isinstance(server, dict) and server.get("username")
                                             and server.get("credential") for server in servers):
        return servers
    LOG.warning("TURN response has no relay credentials; using STUN only")
    return STUN_FALLBACK


def mavlink_connections(settings):
    from pymavlink import mavutil
    writer = mavutil.mavlink_connection(settings.router_url, source_system=settings.gcs_sysid,
                                       source_component=settings.gcs_compid, dialect="ardupilotmega")
    try:
        monitor = mavutil.mavlink_connection(settings.router_url, source_system=254,
                                            source_component=192, dialect="ardupilotmega")
    except Exception:
        writer.close()
        raise
    return writer, monitor


def telemetry_message(msg):
    fields = msg.to_dict()
    fields.pop("mavpackettype", None)
    return {"t": "telemetry", "kind": msg.get_type(), "fields": fields}


def log_status(protocol, arbiter, adapter, now):
    planner = arbiter.last_planner_ms
    LOG.info("Control: owner=%s target=%s packets ok=%d bad=%d %s last_age=%s offset=%s planner_seen=%s",
             arbiter.owner_at(now), adapter.target, protocol.accepted, protocol.rejected,
             arbiter.rejects, arbiter.last_age_ms,
             "set" if protocol.offset_ms is not None else "none",
             "never" if planner is None else f"{(now - planner) / 1000:.1f}s ago")


async def run_session(settings, source):
    import websockets
    from gi.repository import GLib

    servers = await asyncio.to_thread(ice_servers, settings.site_url)
    writer, monitor = await asyncio.to_thread(mavlink_connections, settings)
    arbiter = Arbiter(settings.channels, settings.throttle_channel)
    outgoing = asyncio.Queue(maxsize=64)
    loop = asyncio.get_running_loop()
    peer = None
    ws = None
    closed = False

    def queue_signal(obj):
        def enqueue():
            if not outgoing.full():
                outgoing.put_nowait(obj)
        loop.call_soon_threadsafe(enqueue)

    def on_closed():
        nonlocal closed
        closed = True

    def send_session(obj):
        if peer is not None:
            peer.send_session(obj)

    protocol = PeerProtocol(arbiter, send_session, settings.session_seconds)
    adapter = MavlinkAdapter(writer, monitor, arbiter, settings,
                             on_telemetry=lambda msg: send_session(telemetry_message(msg)))
    try:
        async with websockets.connect(signal_url(settings), max_size=65536, open_timeout=15) as ws:
            peer = GstPeer(source, servers, queue_signal, protocol, on_closed)
            watcher = SourceWatcher(settings, source)
            last_ping = last_state = last_status = now_ms()
            signal_open = True
            connecting_at = None
            context = GLib.MainContext.default()
            while not closed and not protocol.ended:
                now = now_ms()
                watcher.poll(now)
                for _ in range(20):
                    if not context.pending():
                        break
                    context.iteration(False)
                peer.poll_bus()
                adapter.poll(now)
                protocol.tick(now)
                adapter.send(now)
                if protocol.started_ms is not None and now - last_status >= 5000:
                    last_status = now
                    log_status(protocol, arbiter, adapter, now)
                if protocol.started_ms is not None and now - last_ping >= 500:
                    protocol.ping(now)
                    last_ping = now
                if protocol.started_ms is not None and now - last_state >= 1000:
                    protocol.state(now)
                    last_state = now
                while signal_open and not outgoing.empty():
                    await ws.send(json.dumps(outgoing.get_nowait(), separators=(",", ":")))
                if signal_open:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=.02)
                        msg = json.loads(raw)
                        if msg.get("type") != "ice":
                            LOG.info("Signal: %s", msg.get("type"))
                        if msg.get("type") == "peer-joined":
                            connecting_at = now_ms()
                            peer.offer()
                        else:
                            peer.on_signal(msg)
                    except asyncio.TimeoutError:
                        pass
                    except websockets.ConnectionClosed as exc:
                        code = getattr(exc.rcvd, "code", None)
                        LOG.info("Signaling closed: %s %s", code, getattr(exc.rcvd, "reason", ""))
                        signal_open = False
                        if connecting_at is None and code in ROOM_REJECT_CODES:
                            raise RoomRejected(code) from None
                    except (ValueError, TypeError, KeyError) as exc:
                        LOG.warning("Invalid signaling message: %s", type(exc).__name__)
                else:
                    await asyncio.sleep(.02)
                if protocol.started_ms is None and connecting_at is not None and now - connecting_at > 60_000:
                    raise TimeoutError("WebRTC peer did not connect")
                # The browser drops signaling once ICE connects, which can precede the data channel opening.
                if not signal_open and protocol.started_ms is None and connecting_at is None:
                    raise ConnectionError("signaling closed before WebRTC connected")
    finally:
        protocol.close(now_ms())
        if peer is not None:
            peer.close()
        try:
            adapter.send(now_ms())
        except Exception:
            pass
        writer.close()
        monitor.close()


async def run(settings):
    delay = 1
    while True:
        try:
            source = await wait_for_source(settings)
            await run_session(settings, source)
            wait, delay = 1, 1
        except asyncio.CancelledError:
            raise
        except VideoSourceChanged as exc:
            LOG.info("%s; following current source", exc)
            wait, delay = 1, 1
        except RoomRejected as exc:
            LOG.warning("%s; retrying in %ss", exc, REJECT_DELAY)
            wait, delay = next_delay(delay, exc)
        except Exception as exc:
            LOG.exception("Session stopped (%s): %s", type(exc).__name__, exc)
            wait, delay = next_delay(delay, exc)
        await asyncio.sleep(wait)


async def serve(settings):
    # systemd stops with SIGTERM; cancelling lets run_session's finally release RC overrides
    # and close the websocket, so the worker does not keep a stale host socket.
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await run(settings)
    except asyncio.CancelledError:
        LOG.info("Stopped")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = Settings.from_env(os.environ)
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
