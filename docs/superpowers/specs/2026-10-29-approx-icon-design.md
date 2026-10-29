# Approximate vehicle position on the GCS map before the first GPS fix

## Intent

After power-on the rover has no GPS fix for tens of seconds to minutes, and the GCS map (QGroundControl first, Mission Planner second) shows no vehicle at all. The agps service already knows roughly where the rover is (the cached last fix, or the manual region from `/etc/agps.env`). Show the vehicle icon itself at that approximate point until the real fix arrives, with Lima's privacy rules intact.

## Goals

- Before the first 3D fix, while agps is in state `SENT` with a chosen position, the GCS shows the vehicle icon at:
  - **Lima OFF** (GCS on mavlink-router's UDP server `14550`): the real approximate position (`Runner.chosen_pos`).
  - **Lima ON** (GCS on Lima `:14551`), no anchor (always the case before the first fix after a Pi boot): Lima's fake anchor point `LIMA_FAKE_ANCHOR` (default `-12.0700,-77.0435`).
  - **Lima ON with an anchor** (Pi stayed up, FC rebooted; or Lima restarted mid-session): `anchor.to_fake(approx)`, as for any other position.
- The video HUD shows the same point as the map: one line `≈ <lat:.2f> <lon:.2f>`. The `≈` alone marks it approximate. No source, no `±N km`. ON + no anchor → `≈ -12.07 -77.04`; ON + anchor → `≈ <to_fake(approx)>`; OFF → `≈ <real approx>`. The old `≈ <source> ±N km` form goes away.
- When the GPS gets a 3D fix, the approximate display stops and everything is as today.

## Non-goals

- Making the approximate icon look different from a real one. QGC and MP have no "approximate" icon style; the HUD `≈` and a one-time STATUSTEXT are the only markers.
- Showing an accuracy circle on the map.
- Hiding the approximate position from Rpanion's own endpoints (`14540` web UI, `14541` logger). They get the same broadcast as the GCS, like the real position after a fix. Lima's spec already excludes them.

## Anti-goals (failure even if it "works")

- **Any approximate position reaching the autopilot**: no `GPS_INPUT`, fake GPS, `SET_GPS_GLOBAL_ORIGIN`, `SET_HOME_POSITION`, home change, nothing that touches EKF, navigation, failsafes or fences. GCS display only.
- **Lima anchoring on the approximate position.** That would shift Peru by up to the accuracy radius (≤ 500 km) until Pi reboot.
- **A real coordinate reaching a Lima GCS.** Lima stays fail-closed.
- **Coordinates in logs** (journal, STATUSTEXT).

## Findings (primary sources)

Source revisions read on 2026-10-29: QGC `master` `ba28bf886f6a`, Mission Planner `master` `82dd96dfb44b`, ArduPilot `master` `6743a33579b4` and tag `Rover-4.6.3`, mavlink-router `master` `5e38c7a32391`.

**1. QGC map position.** `Vehicle::_mavlinkMessageReceived` drops messages whose sysid isn't the vehicle's. `_handleGlobalPositionInt` returns if `compid != _defaultComponentId`, and `_defaultComponentId` is the compid of the autopilot HEARTBEAT (`MultiVehicleManager::_vehicleHeartbeatInfo` ignores heartbeats with compid ≠ `MAV_COMP_ID_AUTOPILOT1`). So the message must be **1/1**. It writes `altitudeRelative`/`altitudeAMSL` from every GLOBAL_POSITION_INT, then **ignores lat = lon = 0** for the position (comment: "ArduPilot sends bogus GLOBAL_POSITION_INT messages with lat/lat 0/0"), otherwise sets `_coordinate` and `_globalPositionIntMessageAvailable = true`. `_handleGpsRawInt` sets the coordinate only when `fix_type >= 3` **and** no non-zero GLOBAL_POSITION_INT has ever arrived. HIGH_LATENCY2 is used only on high-latency links. The coordinate is never cleared; it keeps its last value.
- https://github.com/mavlink/qgroundcontrol/blob/ba28bf886f6a/src/Vehicle/Vehicle.cc#L854-L905 (`_handleGpsRawInt`, `_handleGlobalPositionInt`), #L530-L548 (sysid filter)
- https://github.com/mavlink/qgroundcontrol/blob/ba28bf886f6a/src/Vehicle/MultiVehicleManager.cc#L68-L119

**1b. Mission Planner.** `CurrentState.Parent_OnPacketReceived` only takes messages from `parent.sysid`/`parent.compid` (1/1). GLOBAL_POSITION_INT: `alt = relative_alt`, and if lat or lon is 0 or `int.MaxValue` it sets `useLocation = false` and keeps `lat`/`lng` unchanged. Otherwise it sets `lat`/`lng`/`altasl`/`vx..vz` and `useLocation = true`. GPS_RAW_INT: **if `!useLocation`, it copies the GPS lat/lon whatever the fix type**, so the FC's `0,0` overwrites the position.
- https://github.com/ArduPilot/MissionPlanner/blob/82dd96dfb44b/ExtLibs/ArduPilot/CurrentState.cs#L2278-L2287 (filter), #L3261-L3300 (GLOBAL_POSITION_INT, GPS_RAW_INT)

**2. Flicker.** The FC keeps sending GLOBAL_POSITION_INT `0,0` and GPS_RAW_INT fix 1 `0,0`.
- QGC: no flicker. The FC's zeros are ignored for the position; our non-zero 1/1 GLOBAL_POSITION_INT wins and also disables the GPS_RAW_INT path. Altitude readouts would flicker if ours differ, so we copy `alt`/`relative_alt` from the FC's latest GLOBAL_POSITION_INT.
- MP: an FC `GLOBAL_POSITION_INT 0,0` sets `useLocation=false`; if the FC's `GPS_RAW_INT 0,0` comes before our next message, MP moves the icon to `0,0`. We send right after each FC GLOBAL_POSITION_INT and GPS_RAW_INT we see, so the `0,0` window is a few ms (router + loopback). A brief flash is possible when the FC sends both in one burst. Accepted; MP is secondary.
- Option (b), a rewriting proxy on the direct path, needs Rpanion's router outputs changed and doubles Lima's job. Rejected. Option (c) is part of the chosen design: Lima rewrites, the spoof feeds both paths.

**3. ArduPilot never acts on a spoofed 1/1 message.** Two independent guards:
- mavlink-router does not forward it to the FC. `Endpoint::accept_msg` rejects a message whose source sysid/compid that endpoint has itself sent ("This endpoint sent the message, we don't want to send it back over the same channel to avoid loops"). The FC UART endpoint learned 1/1 from the FC's first message. This is the mechanism already seen on hardware with Batt2. https://github.com/mavlink-router/mavlink-router/blob/5e38c7a32391/src/mavlink-router/endpoint.cpp#L520-L524 (learning: #L246, #L461-L480)
- If it got there anyway (e.g. a router restart before the FC has sent anything), `MAVLink_routing::check_and_forward` returns false at once when `msg.sysid == mavlink_system.sysid && msg.compid == mavlink_system.compid`: "do not process locally" and not forwarded. `GCS_MAVLINK::raw_packetReceived` returns before the GLOBAL_POSITION_INT mount hook, scripting and `handle_message`, so AP_Follow, AP_Mount, scripting, ADSB and the rest never see it. Same in Rover-4.6.3. https://github.com/ArduPilot/ardupilot/blob/6743a33579b4/libraries/GCS_MAVLink/MAVLink_routing.cpp#L101-L106, https://github.com/ArduPilot/ardupilot/blob/6743a33579b4/libraries/GCS_MAVLink/GCS_Common.cpp#L1884-L1901, https://github.com/ArduPilot/ardupilot/blob/Rover-4.6.3/libraries/GCS_MAVLink/MAVLink_routing.cpp#L108-L113
- The spoof also carries no target and is GLOBAL_POSITION_INT only. agps sends nothing new to the FC.

**4. Lima.** Today `Rewriter._track` sets the anchor **only from GPS_RAW_INT** with `fix_type >= 3` and non-zero lat/lon. GLOBAL_POSITION_INT updates only `real_pos` (the uplink-guard reference). With no anchor, every non-zero position is dropped. The spoof is a 1/1 GLOBAL_POSITION_INT and can't be told apart from the FC's by header. The design needs no marker:
- **Anchor:** the spoofer never sends GPS_RAW_INT (test), and Lima keeps anchoring only on GPS_RAW_INT (test). Poisoning the anchor is impossible by construction.
- **`real_pos`:** taken from GPS_RAW_INT fix ≥ 3 only, no longer from GLOBAL_POSITION_INT. The guard then can't be pointed at the approximate point. The cost: the guard reference updates at the GPS_RAW_INT rate and holds the last fix during a GPS outage. That is irrelevant for a 5 km guard.
- **Display:** a non-zero GLOBAL_POSITION_INT with no anchor is no longer dropped. Its lat/lon is replaced with the fake anchor point; other fields pass. Whatever its origin (spoof, or an EKF position before the first fix), the output holds only the fake point, so it fails closed. With an anchor it is converted as today. Every other position message without an anchor is still dropped.
- Nice side effect: the first real fix maps to the fake anchor point, so through Lima the icon stays where it was when the fix arrives.

## Decided

**Component: agps** (`python/agps.py`). It already has `chosen_pos`, the state machine (`SENT` → `DONE` on the first good fix) and FC telemetry. Nothing new to deploy. The spoof goes out on a **second** TCP connection to `127.0.0.1:5760` (the Batt2Sender lesson): once that connection has sent 1/1, the router stops forwarding 1/1 to it. agps's existing 1/193 connection keeps receiving FC telemetry. The second connection is write-only, and incoming bytes are drained and discarded.

**Active when** all hold: `state == "SENT"`, `chosen_pos` is not None, the last FC GPS_RAW_INT had no good fix, an FC HEARTBEAT arrived within 3 s, and `AGPS_MAP_ICON` ≠ `0`. The first good fix moves the state to `DONE`, so it stops on the same GPS_RAW_INT that agps already uses for TTFF. FC heartbeat lost → `_restart` → stops.

**Rate:** right after each FC GLOBAL_POSITION_INT or GPS_RAW_INT seen on the 1/193 connection (at most one per 0.1 s), and at least once per second.

**Message:** GLOBAL_POSITION_INT, MAVLink 2, src 1/1, own seq counter:

| Field | Value | Why |
|---|---|---|
| `lat`, `lon` | `round(chosen_pos.lat/lon * 1e7)` | approx centre; constant, so the icon never moves |
| `alt`, `relative_alt` | copied from the FC's latest GLOBAL_POSITION_INT; before any, `round(alt_m*1000)` and `0` | no altitude flicker in QGC/MP |
| `time_boot_ms` | the FC's latest, else 0 | |
| `vx`, `vy`, `vz` | `0` | implies no motion |
| `hdg` | `65535` (UINT16_MAX, "unknown" per MAVLink) | QGC/MP take heading from ATTITUDE anyway |

**Own echo:** the router forwards the spoof to agps's 1/193 connection. A 1/1 GLOBAL_POSITION_INT whose lat/lon equals the spoof's exactly is ignored when copying FC fields.

**STATUSTEXT once** per `SENT` cycle on the 1/193 connection, severity NOTICE: `AGPS: map icon approximate until GPS fix`. No coordinates.

**Lima** (`python/lima.py`): the GLOBAL_POSITION_INT rule above; `real_pos` from GPS_RAW_INT only. Status datagram becomes `LIMA <0|1> <fake_lat:.5f> <fake_lon:.5f>` so the HUD knows the fake point without its own copy of `LIMA_FAKE_ANCHOR`.

**HUD** (`python/video-server.py`): `apply_lima` accepts the new form (and the old two-token one, with no fake point). New `approx_display(approx, on, anchor, fake)` → `(lat, lon)` or None: OFF → real; ON + anchor → `to_fake`; ON + no anchor + fake known → fake point; ON + no anchor + fake unknown → None (no line). `approx_line((lat, lon))` → `≈ {:.2f} {:.2f}`. Fix coordinates keep using `coord_policy` unchanged; the fake point is never used for real fixes.

**Settings** (agps, `/etc/agps.env`):

| Variable | Default | Meaning |
|---|---|---|
| `AGPS_MAP_ICON` | `1` | `0` → no map icon (HUD line and status datagram unaffected) |
| `AGPS_MAP_TCP` | `127.0.0.1:5760` | router TCP server for the separate 1/1 connection; invalid → exit 2 |

## What the user sees

| Phase | QGC, Lima OFF | QGC, Lima ON | MP (either) | HUD |
|---|---|---|---|---|
| Boot → agps `SENT` (~15 s after GPS detected) | no icon (as today) | no icon | icon at 0,0 / none (as today) | no `≈` line |
| `SENT`, no fix | icon at real approx, steady | icon at fake point (or `to_fake` with anchor), steady | same points; rare brief flash to 0,0 | `≈ lat lon` matching the map |
| First 3D fix | spoof stops. QGC keeps the approx icon until the FC's EKF reports a position (usually a few s), then jumps to real | same; through Lima, the first fix maps to the fake point, so it hardly moves | moves to the GPS position at once (`GPS_RAW_INT`) | `≈` line replaced by the fix |
| agps stopped or FC lost while `SENT` | icon frozen at last approx | frozen | back to 0,0 | `≈` gone after 3 s |

## Risks accepted by the user

- An icon up to the accuracy radius (cache 25 km, manual up to 500 km) from the real rover looks like a real position on the map. The mitigations are the HUD `≈`, the one-time STATUSTEXT, and the fact that the icon never moves. Operators must not navigate by it.
- QGC keeps showing the approx icon for a few seconds after the first fix (until the EKF has a position), and freezes it if agps stops.
- The 1/1 spoof interleaves seq numbers with the FC's, so GCS link-quality / packet-loss stats read worse, as with Batt2.
- Rpanion web UI and the router's tlog see the approximate position.

## Rollback

`AGPS_MAP_ICON=0` in `/etc/agps.env` + `sudo systemctl restart agps` turns the icon off at once. Full rollback: revert the commits; the agps change can be reverted alone. **Lima and the HUD must be deployed and reverted together**: the current HUD rejects the new `LIMA 1 <lat> <lon>` datagram, reads `Lima: OFF`, and would print real coordinates on the video while Lima is ON. The new HUD also accepts the old `LIMA 0/1`, so updating the HUD first is safe.

## Unverified

- Rpanion 0.12's bundled mavlink-router has the same `accept_msg` loop rule as `master` (consistent with the Batt2 hardware observation, not read from its pinned source).
- QGC shows STATUSTEXT from compid 193 of the vehicle's sysid.
- How often MP actually flashes to 0,0 (depends on FC stream timing over ZeroTier).
- How long QGC keeps the approx icon after the fix (EKF3 GPS checks; seconds, not measured).
- ArduPilot sends no `ALTITUDE` message (QGC prefers it for altitude; irrelevant to the position).
