# AGPS: boot-time GPS assist without a u-blox account

## Intent

Get the rover's first GPS fix sooner after power-on. Nothing about the vehicle's location may leave the vehicle (Pi + FC), and nothing may need a u-blox account or token. This is "option A": turn on u-blox AssistNow Autonomous in the receiver, and at boot hand the receiver the current time and a rough position.

## Goals

- At every FC/GPS boot, the Pi sends the receiver, through the FC: enable AssistNow Autonomous (ANA), then UTC time (only if the Pi's clock is NTP-synced), then an approximate position (if there is one).
- Position sources, first that applies: (a) the last good fix the service cached on the Pi, if younger than `AGPS_CACHE_MAX_DAYS`; (b) a coarse position the user typed into `/etc/agps.env`; (c) none, so time only.
- Measure the result: the journal logs time from "GPS detected" to first 3D fix on every boot, so the effect can be compared with the service off.

## What option A does not do

- **First fix ever.** ANA builds its orbit predictions from ephemerides the receiver has already downloaded from the sky (MAX-M10S integration manual §3.12.5.1). A receiver that has never had a fix, or whose backup supercap has run flat, has no ANA data and no cache entry. On that boot it only gets the time and the manual position: a warm start instead of a cold one, not a hot start. One-time step: power the rover outdoors with open sky and leave it running ≥ 15 min after the fix. That fills ANA and the cache. ANA coverage grows with sky time: ~12 h of tracking covers the full GPS constellation at 47° N (§3.12.5.3).
- **Short power-offs.** With the supercap still holding the backup RAM (BBR) and real-time clock (RTC), the receiver already has time, position and ephemeris (hot start, < 4 h off). It ignores time assistance when its own time is better (§3.12.2). Option A matters after longer power-offs: ephemeris older than about 4 h, where ANA covers the gap (default ORBMAXERR ≈ 3 days, up to 6 days for satellites seen many times). And it matters after the supercap has drained, where the injected time and position are all the receiver has.
- **Future, option C (not planned here).** Real AssistNow Live or Predictive Orbits via Thingstream ZTP fixes first-ever cold starts. The user already has a Thingstream Evaluation ZTP device profile (chip type OTHER). The token sits on the Pi in `/etc/agps.env` (root, 0600) as `AGPS_ZTP_TOKEN`. The endpoint is `https://api.thingstream.io/ztp/assistnow/credentials`. That step would send the token (and, depending on the request, a coarse position) to u-blox, which breaks this spec's "nothing leaves the vehicle" rule, so it must be opt-in. This design leaves room for it: the unit already loads `EnvironmentFile=-/etc/agps.env`, and the assist bundle is built by one function (`build_assist`) that an online step can add frames to. The token never goes into the repo.

## Non-goals

- Cell-ID positioning. The E3372 HiLink API gives the cell ID and PLMN but not the TAC. OpenCellID lookups need registration and a download. Out of scope.
- HUD status. Branch `gps-hud` (PR #6, unmerged) is editing `python/video-server.py`. A tiny follow-up after it merges could show "AGPS: sent" there. Not now.
- Proof that the receiver accepted each message. See "Observability".

## Path to the receiver (verified)

`agps` connects to mavlink-router's TCP server `127.0.0.1:5760`, the same one `Batt2Sender` already uses, so Rpanion's config doesn't change. It sends `GPS_INJECT_DATA` (#123: `target_system`, `target_component`, `len`, `data[110]`) to 1/1.

- ArduPilot `GCS_Common.cpp` passes `GPS_INJECT_DATA` to `AP_GPS::handle_msg` → `handle_gps_inject` → `handle_gps_rtcm_fragment(0, …)` → `inject_data` → `AP_GPS_Backend::inject_data`. That calls `port->write(data, len)` unchanged, if `txspace() > len`. If there isn't room, the bytes are **dropped silently**. `GPS_INJECT_TO` (default 127) means all GPSes.
- **Use `GPS_INJECT_DATA`, not `GPS_RTCM_DATA`.** With RTCM decoding enabled, `GPS_RTCM_DATA` seen on more than one MAVLink channel goes through an RTCM3 parser (`parse_rtcm_injection`), which would eat UBX bytes. `GPS_INJECT_DATA` skips that parser. MAVLink marks it `superseded` (2022-05) but ArduPilot master still handles it.
- **One UBX frame per `GPS_INJECT_DATA` message, never split.** ArduPilot writes its own config messages to the same UART between calls, so a frame split across two injects could get interleaved with them. All our frames are ≤ 32 bytes. Send them ≥ 100 ms apart so TX space is never short.
- Routing: mavlink-router sends a message targeted at 1/1 only to endpoints that have seen 1/1 as a source (`Endpoint::accept_msg`). That means the FC, and video-server's battery-2 TCP socket, which pretends to be 1/1 and throws everything away. GCS endpoints and Lima don't get it, so the injected position doesn't reach the GCS. Lima also drops `GPS_INJECT_DATA` downlink. Our identity is sysid 1, compid 193 (`MAV_COMP_ID_ONBOARD_COMPUTER3`), so the router still forwards 1/1 telemetry to us. Any tlog Rpanion's router writes will contain the injected bytes, as Lima's spec allows for logs.

**ArduPilot's u-blox auto-config doesn't conflict** (Rover-4.6.3 `AP_GPS_UBLOX.cpp`):
- `config_M10` sets only `CFG_SIGNAL_*` and `CFG_NAVSPG_DYNMODEL`. ArduPilot sends no `CFG-ANA`, no `MGA-*` and no `CFG-RST`. `GPS_SAVE_CFG` sends `CFG-CFG` save, not clear.
- ACK-ACK for VALSET isn't handled. A NACK for VALSET is blamed on ArduPilot's current config key and at worst prints "unable to configure band" (it shouldn't happen: the M10 knows CFG-ANA).
- Config runs one step every 200 ms until done, then every 2 s. It never reports "done" over MAVLink. So we wait `AGPS_SETTLE_S` (15 s) after GPS_RAW_INT first shows the GPS present (`fix_type ≥ 1`).
- On master builds with CFGv2, `GPS_AUTO_CONFIG=3` resets the module's config at boot. The settle delay plus re-sending on every boot covers that. The README tells users not to set 3.

## UBX messages (verified against the M10 SPG 5.10 interface description, UBX-21035062 R03)

Frame: `B5 62 | class | id | len (U2 LE) | payload | CK_A CK_B`, 8-bit Fletcher over class..payload (§3.4).

| Message | Class/ID | Payload |
|---|---|---|
| `CFG-VALSET` | `06 8A` | `0` version=0, `1` layers=`0x03` (RAM+BBR), `2..3` reserved, then key U4 LE + value. `CFG-ANA-USE_ANA` = `0x10230001` (L, 1 byte). Default **0 (off)** |
| `MGA-INI-TIME_UTC` | `13 40` | 24 B: `0` type=`0x10`, `1` ver=0, `2` ref=0 (on receipt), `3` leapSecs I1, `4` year U2, `6` month, `7` day, `8` hour, `9` minute, `10` second, `11` bitfield0=0, `12` ns U4, `16` tAccS U2, `18` reserved, `20` tAccNs U4 |
| `MGA-INI-POS_LLH` | `13 40` | 20 B: `0` type=`0x01`, `1` ver=0, `2..3` reserved, `4` lat I4 1e-7°, `8` lon I4 1e-7°, `12` alt I4 cm (WGS84 ellipsoid), `16` posAcc U4 cm (stddev) |
| `MON-VER` poll | `0A 04` | empty → `B5 62 0A 04 00 00 0E 34` |
| `CFG-RST` (test only) | `06 04` | `0` navBbrMask X2 (`0x0000` hot, `0x0001` warm, `0xFFFF` cold, bit 15 = ANA data), `2` resetMode=`0x02` (GNSS only: keeps config), `3` reserved |

Don't send `CFG-ANA-ORBMAXERR` (`0x30230002`, U2 m, 5–1000, default 100). u-blox recommends the firmware default.

## Decided

- **Time.** Sent only if `timedatectl show -p NTPSynchronized --value` prints `yes`. tAcc = 1 s. Real error is NTP over 4G (tens of ms) plus router, FC loop and UART latency (≲ 0.2 s). u-blox warns never to claim better than the truth, and its own online service uses 10 s. leapSecs = 18 (GPS−UTC since 2017).
- **Position.** Cached fix within 14 days, posAcc 25 km. Otherwise manual `AGPS_APPROX_LAT`/`LON` (user rounds to 0.1°), posAcc 50 km. Otherwise nothing. u-blox only warns that a posAcc smaller than the real error hurts. It publishes no upper limit for the M10. Satellite visibility barely changes over 50 km, so a loose value costs almost nothing, while a tight one that's wrong (the rover was driven somewhere else) does harm.
- **Cache** `/var/lib/agps/last_fix.json` (systemd `StateDirectory`, 0700 dir, 0600 file, user `rpanion`), with `lat`/`lon` rounded to 0.01° (~1 km), `alt_m`, `t`. It is written from GPS_RAW_INT with `fix_type ≥ 3` and nonzero lat/lon, on the first such fix and then at most every 15 min. This is the real area stored on the Pi's SD card. `AGPS_CACHE=0` stops reads and writes and deletes the file. A cache with `t` in the future or older than max age is ignored. Logs never contain coordinates.
- **Timing.** Wait for a HEARTBEAT from 1/1 → GPS_RAW_INT `fix_type ≥ 1` → wait `AGPS_SETTLE_S`. If there's already a 3D fix (service restarted mid-session), send only VALSET. Otherwise send VALSET, TIME, POS. If there's still no 3D fix 30 s later, send the bundle once more with fresh time (cheap insurance, since we can't see acks). If the FC heartbeat is gone for > 10 s (FC reboot), start over.
- **Packaging.** Separate `python/agps.py` + `systemd/agps.service`, long-running because it writes the cache. Not folded into Lima: Lima is a fail-closed GCS-side proxy on its own UDP endpoint that can be off, while agps needs real FC data and has its own on/off. Kill switch: `AGPS_ENABLE=0` (exits 0) or `systemctl disable --now agps`.
- **CLI.** `agps.py` runs the service. `agps.py probe` sends a MON-VER poll and prints STATUSTEXT. `agps.py reset {hot,warm,warm-noana,cold}` is for testing, refuses if the vehicle is armed, and sends `CFG-RST` with masks `0x0000/0x0001/0x8001/0xFFFF`. `agps.py inject` sends the bundle once now.

| Variable | Default | Meaning |
|---|---|---|
| `AGPS_ENABLE` | `1` | `0` → exit at once |
| `AGPS_MAVLINK` | `tcp:127.0.0.1:5760` | pymavlink connection string |
| `AGPS_SETTLE_S` | `15` | wait after GPS detected |
| `AGPS_APPROX_LAT`, `AGPS_APPROX_LON` | unset | manual coarse position, only in `/etc/agps.env` |
| `AGPS_APPROX_ALT_M` | `0` | ellipsoid altitude for manual |
| `AGPS_APPROX_ACC_KM` | `50` | posAcc for manual; valid 1..500, invalid → default with a log line |
| `AGPS_STATUS` | `127.0.0.1:14572` | `host:port` for a 1 Hz UDP status line `AGPS <source> <acc_m> <lat> <lon>` while SENT (bundle sent, no 3D fix) and a position was chosen; empty disables; invalid → exit 2. Coordinates go only there, never to logs |
| `AGPS_CACHE` | `1` | `0` → no cache, delete file |
| `AGPS_CACHE_FILE` | `/var/lib/agps/last_fix.json` | |
| `AGPS_CACHE_MAX_DAYS` | `14` | |
| `AGPS_CACHE_ACC_KM` | `25` | posAcc for cache |
| `AGPS_ZTP_TOKEN` | — | reserved for option C; ignored |

## Observability

The receiver's MGA-ACK and ACK-ACK replies come back on the GPS UART. ArduPilot's parser ignores them and doesn't forward them, so acceptance can't be seen from the Pi. What can be seen: ArduPilot parses MON-VER replies and sends STATUSTEXT `u-blox [MOD ]1 HW: <hw> SW: <sw>`. A MON-VER poll injected by us that produces a fresh STATUSTEXT proves the inject path works end to end (the spike). After that, effectiveness is measured, not confirmed: TTFF in the journal, on/off, over several runs. To read `NAV-AOPSTATUS`/`CFG-ANA` directly, connect u-center through ArduPilot serial passthrough (`SERIAL_PASS1/2`) from a laptop on FC USB (optional; unverified here).

## Verified by the spike (Task 0)

An injected MON-VER poll (`GPS_INJECT_DATA` as 1/193 over `tcp:127.0.0.1:5760`) produced STATUSTEXT `u-blox 1 HW: 000A0000 SW: ROM SPG 5.10 (7b202e)` within 0.04 s. The control run without the inject produced no STATUSTEXT in 8 s. So the inject path reaches the receiver, and the firmware is SPG 5.10, so the key IDs from UBX-21035062 apply.

## Unverified

- Supercap hold time.
- No published posAcc upper limit for the M10. 25 and 50 km are engineering choices.
- leapSecs 18 still current (no leap second announced as of writing; check IERS Bulletin C).
- The FC runs ArduPilot 4.6-era u-blox code. Checked: Rover-4.6.3 and master.
- GPS_RAW_INT `alt_ellipsoid` filled by ArduPilot (fallback: `alt`, error ≪ posAcc).
- `SERIAL_PASS*` u-center passthrough details.
