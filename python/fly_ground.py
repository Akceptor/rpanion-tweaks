#!/usr/bin/env python3
"""Unattended Pi Ground: Rpanion RTSP to WebRTC, browser RC to MAVLink."""

import asyncio
import json
import logging
import os
import signal
import time

from fly_ground_auth import host_token_from_env, validate_host_token
from fly_ground_bootstrap import HostLease, parse_session_ready
from fly_ground_config import Settings
from fly_ground_control import Arbiter
from fly_ground_mavlink import MavlinkAdapter
from fly_ground_peer import GstPeer, PeerProtocol, signal_url


LOG = logging.getLogger("fly-ground")
# Worker close codes: host already present, room locked/closed, join rate limit.
ROOM_REJECT_CODES = {4001, 4009, 4029, 4401}
REJECT_DELAY = 30


class RoomRejected(ConnectionError):
    def __init__(self, code):
        reason = 'host authentication failed; check FLY_HOST_TOKEN' if code == 4401 else 'signaling server rejected host'
        super().__init__(f"{reason} (close code {code})")
        self.code = code


def next_delay(delay, exc):
    # Each rejected join counts toward the worker's per-IP limiter, so back off hard.
    if isinstance(exc, RoomRejected):
        return REJECT_DELAY, 1
    return delay, min(delay * 2, 30)


def now_ms():
    return int(time.monotonic() * 1000)


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


async def run_session(settings, host_token):
    validate_host_token(host_token)
    import websockets
    from gi.repository import GLib

    writer, monitor = await asyncio.to_thread(mavlink_connections, settings)
    arbiter = Arbiter(settings.channels, settings.throttle_channel)
    outgoing = asyncio.Queue(maxsize=64)
    loop = asyncio.get_running_loop()
    peer = None
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
        async with websockets.connect(signal_url(settings), max_size=65536, open_timeout=15,
                                      additional_headers={'Authorization': 'Bearer ' + host_token}) as ws:
            last_ping = last_state = last_status = now_ms()
            lease = HostLease(last_ping)
            session_deadline = None
            connecting_at = None
            connected_sent = False
            offered = False
            context = GLib.MainContext.default()
            while not closed and not protocol.ended:
                now = now_ms()
                lease.check(now)
                heartbeat = lease.heartbeat(now)
                if heartbeat is not None:
                    await ws.send(json.dumps(heartbeat, separators=(',', ':')))
                if session_deadline is not None and now >= session_deadline:
                    protocol.close(now, 'relay authorization expired; reconnect')
                    break
                for _ in range(20):
                    if not context.pending():
                        break
                    context.iteration(False)
                if peer is not None:
                    peer.poll_bus()
                adapter.poll(now)
                protocol.tick(now)
                adapter.send(now)
                if protocol.started_ms is not None and not connected_sent:
                    await ws.send(json.dumps({'type': 'rtc-connected', 'generation': lease.generation}))
                    connected_sent = True
                if protocol.started_ms is not None and now - last_status >= 5000:
                    last_status = now
                    log_status(protocol, arbiter, adapter, now)
                if protocol.started_ms is not None and now - last_ping >= 500:
                    protocol.ping(now)
                    last_ping = now
                if protocol.started_ms is not None and now - last_state >= 1000:
                    protocol.state(now)
                    last_state = now
                while not outgoing.empty():
                    await ws.send(json.dumps(outgoing.get_nowait(), separators=(",", ":")))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=.02)
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        raise ValueError('Invalid signaling message')
                    kind = msg.get('type')
                    if kind == 'room-ready':
                        lease.admit(msg.get('generation'), now_ms())
                    elif kind == 'host-heartbeat-ack':
                        lease.ack(msg.get('generation'), now_ms())
                    elif kind == 'session-ready':
                        if peer is not None:
                            raise ValueError('Duplicate admitted session configuration')
                        epoch = time.time() * 1000
                        bootstrap = parse_session_ready(msg, lease.generation, epoch)
                        connecting_at = now_ms()
                        session_deadline = connecting_at + bootstrap['expiresAt'] - epoch
                        peer = GstPeer(settings.rtsp_url, bootstrap['iceServers'], queue_signal, protocol, on_closed)
                        if bootstrap['turnStatus'] == 'unavailable':
                            LOG.warning('Authorized session has no TURN relay; using STUN only')
                        await ws.send(json.dumps({'type': 'rtc-ready', 'generation': lease.generation}))
                    elif kind == 'peer-joined':
                        if peer is None or offered:
                            raise ValueError('Unexpected negotiation start')
                        offered = True
                        peer.offer()
                    elif kind in ('answer', 'ice'):
                        if peer is None:
                            raise ValueError('Signaling arrived before admitted session configuration')
                        peer.on_signal(msg)
                except asyncio.TimeoutError:
                    pass
                except websockets.ConnectionClosed as exc:
                    code = getattr(exc.rcvd, 'code', None)
                    LOG.info('Signaling closed (code %s)', code)
                    if code in ROOM_REJECT_CODES:
                        raise RoomRejected(code) from None
                    raise ConnectionError('Signaling closed; ending session') from None
                if protocol.started_ms is None and connecting_at is not None and now - connecting_at > 60_000:
                    raise TimeoutError("WebRTC peer did not connect")
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


async def run(settings, host_token):
    delay = 1
    while True:
        try:
            await run_session(settings, host_token)
            wait, delay = 1, 1
        except asyncio.CancelledError:
            raise
        except RoomRejected as exc:
            LOG.warning("%s; retrying in %ss", exc, REJECT_DELAY)
            wait, delay = next_delay(delay, exc)
        except Exception as exc:
            # Exception text can include transport details; never log bearer headers or ICE credentials.
            LOG.warning("Session stopped (%s)", type(exc).__name__)
            wait, delay = next_delay(delay, exc)
        await asyncio.sleep(wait)


async def serve(settings, host_token):
    # systemd stops with SIGTERM; cancelling lets run_session's finally release RC overrides
    # and close the websocket, so the worker does not keep a stale host socket.
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await run(settings, host_token)
    except asyncio.CancelledError:
        LOG.info("Stopped")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = Settings.from_env(os.environ)
    try:
        host_token = host_token_from_env(os.environ)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    asyncio.run(serve(settings, host_token))


if __name__ == "__main__":
    main()
