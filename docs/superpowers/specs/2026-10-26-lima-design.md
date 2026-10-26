# Lima: fake rover position on the ground station map

## Intent

The user drives a rover in Lviv, Ukraine, and shares their ground station screen (QGroundControl or Mission Planner) on video. They don't want viewers to see where the rover really is. Lima makes the GCS map show the rover in Lima, Peru, moving exactly as it really moves, while driving from the GCS (missions, "drive here", set home, fences) keeps working against the real position.

Who is being hidden from: people watching the shared GCS screen. Nothing else.

## Goals

- Every position the GCS receives is shifted from the real area to Lima.
- Every position the GCS sends down is shifted back to real coordinates before it reaches the FC. Clicking a spot 20 m NE of the rover icon drives the rover 20 m NE in reality.
- Distances and headings on the map match reality (the shift is in metres, not degrees).
- No setup of a real reference point: the rover's first good GPS fix maps to the Lima point.
- Works with any MAVLink GCS (QGC, Mission Planner).

## Non-goals

- Hiding the real position from logs (Rpanion DataFlash logger, FC SD card), NTRIP, cloud upload, or the Rpanion web UI. The user confirmed these can stay real.
- Hiding what the camera shows.
- A runtime on/off toggle. Planned later; see "Room for the toggle".
- GCS connections that don't go through Lima (Rpanion TCP server `5760`, Rpanion "UDP server", any direct GCS UDP output). Setup instructions turn these off; Lima doesn't police them.
- MAVLink2 signing. Rewriting breaks signatures; signing must be off (ArduPilot default).
- Faking altitude. AMSL altitude passes through unchanged (my call: the Lviv/Lima elevation difference isn't visible on a map).

## Anti-goals (failure even if it "works")

- A real coordinate ever reaching the GCS. Lima fails closed: if it crashes, isn't configured, or sees a message carrying a position it doesn't know how to convert, the GCS gets nothing for that message (or no link at all), never the real value.
- A GCS position command reaching the FC unconverted (i.e. still in Lima coordinates). That would send the rover toward Peru.
- Real coordinates in the repo or in any config file. The repo is public on GitHub.

## Constraints and environment

- Pi 5, Rpanion 0.12.0 from the `.deb`, app in `/usr/share/rpanion-server/app`, service `rpanion-server` running as user `rpanion`.
- Rpanion runs `mavlink-routerd` with the FC as input and fans out to `127.0.0.1:14540` (Rpanion itself), `127.0.0.1:14541` (DataFlash logger), optional TCP `5760`, optional UDP server, and the user-configured UDP outputs. Today those outputs are the GCS PC over ZeroTier and `127.0.0.1:14570` (the HUD in `python/video-server.py`).
- The Rpanion `.deb` installs `pymavlink` for the `rpanion` user (`debian/postinst`), so Lima may use it.
- Ground station: QGC on a Windows PC over ZeroTier; Mission Planner possible.
- Vehicle: probably ArduRover (unconfirmed). Rover, not aircraft: terrain-following doesn't matter.
- The Pi is often offline from the dev Mac, so everything must be testable without it.

## Decided

**Placement.** Lima is a separate service on the Pi, on the GCS link only:

```
FC ─ mavlink-routerd ─┬─ 14540 Rpanion, 14541 DF logger, 14570 HUD   (real)
                      └─ UDP output 127.0.0.1:<LIMA_LISTEN port> ─ Lima ─ UDP ─ GCS PC   (fake)
```

In Rpanion's Flight Controller page the user removes the direct GCS output, adds Lima's listen address as an output, and turns off the TCP server and UDP server. Lima forwards to the GCS address and relays the GCS's replies back to mavlink-router, converting both ways. Being a separate file and systemd unit, it survives Rpanion `.deb` upgrades.

**Anchor.** The user chose: the rover's first good GPS fix (3D fix or better) after Lima starts maps to the Lima point. Default Lima point: Campo de Marte park, Jesús María, Lima, about `-12.0700, -77.0435`. My call: an open area so real paths don't visibly cross buildings.

- Until the first good fix, every position-carrying message is dropped in both directions. The GCS shows no position rather than a real one.
- My call: the anchor is kept in `/run/lima/` (tmpfs, cleared on Pi reboot, readable only by `rpanion`). If Lima restarts mid-drive it reuses the anchor, so the icon doesn't jump. After a Pi reboot, a new anchor is taken.
- Consequence the user accepted: a mission saved in the GCS one session and uploaded in another lands shifted by however far apart the two start points were. Missions stored on the FC are unaffected (converted fresh on each download). The README must warn about this. The uplink guard only catches shifts over `LIMA_MAX_TARGET_KM`.

**Transform.** Local flat-earth around the anchors: real offset from the real anchor in metres north/east, applied at the Lima anchor. Exact inverse for the uplink. North/east velocities, course and heading are unchanged. Accurate within tens of km of the anchor, which covers rover range.

**Safety guard on the uplink (my call).** After converting back, any target more than `LIMA_MAX_TARGET_KM` (default 5) from the rover's current real position is dropped, and the GCS gets a `STATUSTEXT` warning. If there's no real position yet, position commands are dropped with a warning.

**Unknown positions are dropped.** Messages known to carry no position pass through untouched. Messages with a position that Lima converts are rewritten. Anything else that carries a position, in either direction, is dropped.

**Settings** (environment variables, set with `sudo systemctl edit lima`, like the existing README settings):

| Variable | Default | Meaning |
|---|---|---|
| `LIMA_FAKE_ANCHOR` | `-12.0700,-77.0435` | Lima point the first fix maps to |
| `LIMA_LISTEN` | `127.0.0.1:14580` | Where mavlink-router sends to (Rpanion UDP output) |
| `LIMA_GCS` | none, required | `ip:port` of the GCS (e.g. the PC's ZeroTier IP and `14550`) |
| `LIMA_MAX_TARGET_KM` | `5` | Uplink guard distance |

**Deliverables:**
- `python/lima.py`: the service.
- A systemd unit file, run as `rpanion`.
- Tests under `tests/`, runnable on the Mac without the Pi, in the style of `tests/test_battery.py`.
- A README section: what it does and what it doesn't hide, install, Rpanion output changes (remove direct GCS output, add Lima, TCP and UDP server off), settings, how to check it works, revert.

## Messages

The builder must cover at least these, and must make the drop rule hold for anything not listed.

Downlink (FC → GCS), convert: `GLOBAL_POSITION_INT`, `GPS_RAW_INT`, `GPS2_RAW`, `HOME_POSITION`, `GPS_GLOBAL_ORIGIN`, `MISSION_ITEM_INT` / `MISSION_ITEM` (mission, fence and rally download), `POSITION_TARGET_GLOBAL_INT`, `AHRS2`, `AHRS3`, `FENCE_POINT`, `RALLY_POINT`, `HIGH_LATENCY2`, `CAMERA_FEEDBACK`, `GLOBAL_POSITION_INT_COV`, `TERRAIN_REQUEST`, `TERRAIN_REPORT`, `SIMSTATE` / `SIM_STATE` (SITL testing).

Uplink (GCS → FC), convert back and guard: `MISSION_ITEM_INT` / `MISSION_ITEM`, `SET_POSITION_TARGET_GLOBAL_INT`, `COMMAND_INT`, `COMMAND_LONG` (only commands whose params 5/6 are lat/lon, e.g. `DO_SET_HOME`, `DO_SET_ROI_LOCATION`, `NAV_*`), `SET_HOME_POSITION`, `SET_GPS_GLOBAL_ORIGIN`, `FENCE_POINT`, `RALLY_POINT`.

Rules that matter:
- Convert only coordinates that really are global lat/lon. Check the frame (global and `MAV_FRAME_MISSION` convert; a position command in any other frame is dropped) and the command (a command the dialect knows that is not a position command passes untouched; an unknown command with x/y set is dropped) (many `MAV_CMD_DO_*` use x/y or params 5/6 for other things).
- Leave special values alone: `0,0` ("not set / use current") and `INT32_MAX` "unknown" markers.
- Integer `1e7` degrees and float degrees both appear. Round-trip error: at most 1 cm on integer fields, at most 1 m on float-degree fields (`MISSION_ITEM`, `COMMAND_LONG` params 5/6, `FENCE_POINT`). A float32 can't do better at these longitudes, and 1 m is below rover GPS error. My call; keep converting both forms.
- Recompute CRCs. MAVLink1 and MAVLink2 both possible.

Messages with opaque byte payloads (my calls, all following "fail closed"):

| Message | Direction | Rule | Effect for the user |
|---|---|---|---|
| `FILE_TRANSFER_PROTOCOL` (MAVFTP) | both | drop | GCS falls back to the normal parameter and mission protocols, which Lima converts. QGC parameter load is slower; Lua script upload through the GCS doesn't work |
| `LOG_ENTRY`, `LOG_DATA` and the log request messages | both | drop | No DataFlash download through the GCS while Lima is in the path. Use the Rpanion web UI logs instead |
| `SERIAL_CONTROL` (MAVLink console) | both | drop | No MAVLink console through Lima |
| `GPS_RTCM_DATA`, `GPS_INJECT_DATA` | uplink only | pass | RTK corrections from the GCS still reach the FC. Never shown on screen |
| `FOLLOW_TARGET` | both | drop | No GCS follow-me |
| `ADSB_VEHICLE` | both | drop | No nearby traffic on the map; position and callsign would give away the area |

Not hidden by Lima (README must warn): the GCS's own location. QGC and Mission Planner can show the PC's own position on the map (Windows location services, a USB GPS). That has to be turned off in the GCS before streaming.

## Room for the toggle

Later the user wants to switch Lima on and off. Keep the conversion behind one switch so "off" can be added without restructuring. Don't build the toggle now.

## Left to the builder

- Internal structure, the pymavlink vs hand-rolled parsing choice, and the systemd unit details.
- How the "carries no position" allow-list is built and kept honest. A test that walks the dialect's message definitions and fails on any position-carrying message that is neither converted nor dropped is strongly preferred.
- How Lima learns the GCS's reply address and handles the GCS reconnecting.
- How Lima picks up the rover's real position for the guard.
- Log wording, and whether the drop warnings are rate-limited.
