# rpanion-tweaks

Tweaks for [Rpanion Server](https://github.com/stephendade/Rpanion-server) **v0.12** (Debian package install).

## Video HUD overlay

A replacement `python/video-server.py` that adds a small on-screen display to the video stream:

```
20-Oct-2026 13:38:00                   wwan0 ↓0.42 ↑2.10 Mbps | LTE 78%
                              CPU 34% | 61°C | 4.1W | UNDERVOLT | 25 fps

FC 15.8V 12.3A 74% 1250mAh
PI 7.6V 1.2A 68%
```

| Field | Source | Notes |
|---|---|---|
| Clock | system time | Restyled to match the stats text (small font, dark background) |
| `↓ ↑ Mbps` | `/sys/class/net/<iface>/statistics` | Live throughput on the default-route interface (e.g. 4G modem). Not a speed test — upload is mostly your own video stream |
| `LTE 78%` | ModemManager (`mmcli`) | Only for QMI/MBIM modems. HiLink/RNDIS sticks (e.g. Huawei E3372) don't expose this |
| `CPU %`, `°C` | `/proc/stat`, `thermal_zone0` | Any Pi |
| `W` | `vcgencmd pmic_read_adc` | **Pi 5 only.** Board rails only — excludes USB devices such as the modem |
| `UNDERVOLT` / `THROTTLED` | `vcgencmd get_throttled` | Only shown when there's a problem |
| `UP 0:04:07` / `UP 2d 03:04:05` (top left, below the clock) | `/proc/uptime` | Time since the Pi booted |
| `fps` | frames reaching the encoder | Per viewer |
| `FC ...` (bottom left) | MAVLink `SYS_STATUS` / `BATTERY_STATUS` over UDP | Flight controller battery. Hidden if no data for 5 s. See [Battery overlay](#battery-overlay) |
| `PI ...` (bottom left) | INA226 on I2C | Companion battery, measured before the BEC. Hidden if the sensor is absent. See [Battery overlay](#battery-overlay) |
| `GPS 3D 12 sats`, `HDOP Good (1.2)`, lat/lon (bottom right) | MAVLink `GPS_RAW_INT` over UDP | FC GPS fix, satellites, HDOP with rating (Excellent <= 1, Good <= 2, Usable <= 5, else Poor); coordinates shown from 2D fix up. Hidden if no data for 5 s. Coordinates follow Lima state: shifted like the GCS map while `Lima: ON`, hidden if Lima has no anchor yet. Without a fix, an `≈ lat lon` line shows the same point as the GCS map (real approximate position with Lima OFF; Lima's fake point, or the shifted point with an anchor, with Lima ON; no line if Lima is ON and its fake point is unknown). Source: the agps service (UDP `127.0.0.1:14572`, `AGPS <cache\|manual> <acc_m> <lat> <lon>`) and Lima's status (`LIMA <0\|1> <fake_lat> <fake_lon>`) |
| `Lima: ON/OFF` (bottom centre) | Lima status UDP `127.0.0.1:14571` | ON while a GCS is connected to Lima, OFF otherwise (including Lima not installed or stopped). See [Lima](#lima-fake-position-on-the-gcs-map) |

Fields that aren't available on your hardware are hidden. Slow commands (`vcgencmd`, `mmcli`) run every 5 s in a background thread so they never stall the video.

The overlay is enabled by the existing **Timestamp** checkbox in Rpanion's Video page — no UI changes.

### Limitations

- Works only for raw camera sources (USB/UVC, libcamera/CSI, MJPEG) — same as Rpanion's timestamp. Cameras that output H.264 directly, RTSP sources, legacy `rpicamsrc` and Jetson boards can't be overlaid without re-encoding.
- Network interface is auto-detected from the default route. To force one, set `STATS_IFACE` (e.g. `wwan0`) in the service environment:
  ```bash
  sudo systemctl edit rpanion-server
  # add:
  # [Service]
  # Environment=STATS_IFACE=wwan0
  ```

## Battery overlay

A second overlay in the bottom-left corner shows two batteries. A line is hidden when its source has no data.

### FC battery (MAVLink)

Rpanion's mavlink-router has to send telemetry to the video server. In the web UI, **Flight Controller** page:

1. Stop telemetry (the destination list can't be edited while it runs).
2. **Telemetry Destinations** → **UDP Client** → **Add new destination**: `127.0.0.1:14570` → **Add**. Keep the existing `127.0.0.1:14540`, Rpanion needs it.
3. Start telemetry, then stop/start the video stream.

The server listens on that port; to use another one, set `STATS_MAVLINK_PORT`. Only the autopilot's messages (component 1) are used. If the line shows `FC 0.0V`, the FC has no battery monitor configured (`BATT_MONITOR`).

If the line never appears with no GCS connected, the FC isn't streaming battery messages to the Pi. On ArduPilot set `SRn_EXT_STAT` (n = the serial port wired to the Pi, e.g. `SR2_EXT_STAT` for SERIAL2) to `1` or higher.

### Pi battery (INA226)

The INA226 goes on the **battery side**, between the companion battery and the BEC. That way it sees the real battery voltage (needed for the %) and the total current of the Pi and everything on its 5V rail.

Power path (thick wires):

```
battery + ──▶ IN+ ═[shunt]═ IN− ──▶ BEC in +        BEC out 5V ──▶ Pi 5V (pin 2)
                             │
                            VBUS (tie to IN−)
battery − ─────────────────────────▶ BEC in −        BEC out GND ──▶ Pi GND
```

Signal side (thin wires, module → Pi header):

```
INA226   VCC ──▶ pin 1  (3.3V)
         GND ──▶ pin 6  (GND)
         SDA ──▶ pin 3  (GPIO2 / SDA)
         SCL ──▶ pin 5  (GPIO3 / SCL)
```

| INA226 pin | Connect to | Notes |
|---|---|---|
| IN+ | battery + | Full current flows IN+ → shunt → IN−. Use the same wire gauge as the battery lead |
| IN− | BEC input + | |
| VBUS (`VBS`) | IN− | Bus voltage sense. Some modules tie it internally; others (e.g. CJMCU-226) have a separate pad that **must** be wired, or the voltage reads 0 |
| VCC | Pi **3.3V** (pin 1) | **Not 5V.** The module's I2C pull-ups go to VCC; at 5V they would put 5V on the Pi's GPIO and can damage it |
| GND | Pi GND (pin 6) | |
| SDA | GPIO2 (pin 3) | |
| SCL | GPIO3 (pin 5) | |
| A0, A1 | leave as is | Both low = address `0x40`. Change them only for a second sensor, then set `INA226_ADDR` |

- Battery − goes straight to BEC −, not through the module: the INA226 measures on the + side.
- Keep SDA/SCL short (under ~20 cm) and away from ESC/motor wires.
- Swap the shunt first (see below), then check with `i2cdetect` before closing the airframe.

**Set up I2C on the Pi.** The overlay reads `/dev/i2c-N` directly and needs no Python packages. `i2c-tools` (`i2cdetect`, `i2cget`) is only for checking the wiring.

1. Install the tools, enable I2C, give the service user access, then reboot:

   ```bash
   sudo apt update
   sudo apt install -y i2c-tools
   sudo raspi-config nonint do_i2c 0      # 0 = enable
   sudo usermod -aG i2c rpanion
   sudo reboot
   ```

2. After the reboot, check that the bus exists and the sensor answers (`0x40` by default):

   ```bash
   ls -l /dev/i2c-1                          # missing = I2C still off
   i2cdetect -y 1                            # expect "40" in the grid
   sudo -u rpanion i2cget -y 1 0x40 0x02 w   # bus voltage register; an error here means the service can't read it either
   ```

   An empty grid means a wiring or power problem: check VCC on pin 1 (3.3V), GND on pin 6, and that SDA/SCL aren't swapped. If the sensor shows up as `41`, `44` or `45`, A0/A1 are strapped, so set `INA226_ADDR` to match.

The overlay hides the `PI` line on any I2C error without logging it, so run these checks first if it doesn't show.

**Replace the shunt resistor.** Most modules ship with R100 (0.1 Ω). The INA226 maxes out at 81.92 mV across the shunt, so that is only ~0.82 A, while a Pi 5 plus a modem on 2S draws about 1.3 A. Swap it for R010 (0.01 Ω, ~8 A range) and set `INA226_SHUNT_OHMS=0.01`. When the reading is clipped, the overlay shows `0.8A+` instead of a wrong number.

### Pi battery in Mission Planner

The Pi battery is also sent to the GCS as a MAVLink `BATTERY_STATUS` with `id=1`, through mavlink-router's TCP port `5760` (Rpanion has it on by default), so there is nothing more to set up. Mission Planner shows it as battery 2 (**Quick** tab: `battery_voltage2`, `current_battery2`).

- The FC must have `BATT2_MONITOR=0`, otherwise its own battery 2 clashes with this one.
- It is only sent while FC telemetry is arriving (the server copies the FC's system id) and the TCP connection is up (retried every 10 s).
- It is sent by the video server, so the video stream has to be started in Rpanion (no viewer needed).
- It is sent as the autopilot (1/1) because Mission Planner only shows battery 2 from the autopilot. Its sequence numbers interleave with the FC's, so Mission Planner's link quality may read low.

### Settings

Set them like `STATS_IFACE` above (`sudo systemctl edit rpanion-server`, then add `Environment=...` lines under `[Service]`):

| Variable | Default | Meaning |
|---|---|---|
| `STATS_MAVLINK_PORT` | `14570` | UDP port on 127.0.0.1 for the FC battery |
| `STATS_LIMA_PORT` | `14571` | UDP port on 127.0.0.1 for the Lima ON/OFF status |
| `STATS_AGPS_PORT` | `14572` | UDP port on 127.0.0.1 for the agps approximate position |
| `LIMA_ANCHOR_FILE` | `/run/lima/anchor.json` | Lima anchor file the HUD reads to shift coordinates |
| `MAVLINK_BATT2` | `1` | Send the Pi battery to the GCS as battery 2. `0` turns it off |
| `MAVLINK_BATT2_COMPID` | `1` | MAVLink component id used for that message. `191` (onboard computer) works for GCSs that show other components, not Mission Planner |
| `MAVLINK_BATT2_TCP` | `127.0.0.1:5760` | mavlink-router TCP server (`host:port`) used to send it |
| `INA226_BUS` | `1` | I2C bus (`/dev/i2c-N`) |
| `INA226_ADDR` | `0x40` | I2C address |
| `INA226_SHUNT_OHMS` | `0.1` | Shunt resistance. `0.01` for R010 |
| `PI_BATT_CELLS` | `2` | Cells in the Pi battery, for the %. `0` hides the % |
| `PI_MAH_FILE` | `/tmp/pi_batt_mah.json` | Where the consumed mAh is kept, so a video-server restart keeps the count. It resets on Pi reboot (new battery) |

The Pi battery also shows consumed mAh (`PI 7.2V 0.8A 5% 123mAh`) and sends it to the GCS as battery 2 `current_consumed`. While the shunt is R100 the current clips at ~0.82 A, so the count is a lower bound; a trailing `+` (`123mAh+`) means some samples were clipped.

The Pi battery % is a voltage estimate. It sags under load, so expect it to read low while the modem is busy. The table is for LiPo; Li-ion packs will read low.

## Lima (fake position on the GCS map)

`python/lima.py` is a MAVLink proxy that sits on the GCS link only. The ground station (QGC, Mission Planner) shows the rover in Lima, Peru, moving exactly as it really moves. Commands from the GCS (missions, "drive here", set home, fences) are shifted back, so the rover drives where you meant.

```
FC ─ mavlink-routerd ─┬─ 14540 Rpanion, 14541 DF logger, 14570 HUD   (real)
                      └─ 127.0.0.1:14580 ─ Lima ─ UDP ─ GCS PC       (fake)
```

- The rover's first good GPS fix (3D or better) after Lima starts maps to the Lima point. No reference point to set.
- Until that fix, position messages are dropped in both directions: the GCS shows no position rather than the real one. The one exception is `GLOBAL_POSITION_INT`: its coordinates are replaced with the Lima point, so the icon (for example agps's approximate-position icon, see AGPS "Map icon") sits on the Lima point before the first fix. The real coordinate never leaves Lima.
- The anchor and the uplink guard's reference position come only from `GPS_RAW_INT` fixes (3D or better), never from `GLOBAL_POSITION_INT`. The guard reference updates at the `GPS_RAW_INT` rate and keeps the last fix during a GPS outage.
- The shift is in metres, so distances and headings on the map match reality. Altitude is unchanged.
- Fails closed: a crash, a missing setting, or a message with a position Lima can't convert means the GCS gets nothing, never the real value.
- After converting a GCS target back, anything more than `LIMA_MAX_TARGET_KM` from the rover is dropped and the GCS gets a `STATUSTEXT` warning.
- MAVLink2 signing must be off (the ArduPilot default), because rewriting breaks signatures.

### What it doesn't hide

- Logs (Rpanion DataFlash logger, FC SD card), NTRIP, cloud upload, the Rpanion web UI: all stay real.
- The camera image.
- **The GCS's own location.** Turn off Windows location services and any USB GPS in QGC/Mission Planner before streaming, or the PC's real position shows on the map.
- Any GCS connection that bypasses Lima (Rpanion UDP server `14550`, TCP server `5760`, direct GCS UDP output). These stay enabled and carry the real position.

### Saved missions shift

The anchor lives in `/run/lima/` (tmpfs), so it is cleared when the Pi reboots and a new one is taken at the next first fix. A mission saved in the GCS and loaded after a Pi reboot lands shifted by the distance between the two start points. Only the `LIMA_MAX_TARGET_KM` guard catches it, and only for big shifts. Missions stored on the FC are fine (converted fresh on each download).

If Lima alone restarts, it reuses the anchor and the icon doesn't jump.

The video HUD shows `Lima: ON` while a GCS is connected to Lima and `Lima: OFF` otherwise (also if Lima stops).

### What's dropped

These carry data Lima can't safely convert, so they don't pass:

| Feature | Effect |
|---|---|
| MAVFTP (`FILE_TRANSFER_PROTOCOL`) | GCS falls back to the normal parameter/mission protocols. QGC parameter load is slower; Lua script upload through the GCS doesn't work |
| Log download (`LOG_ENTRY`, `LOG_DATA`, requests) | Use the Rpanion web UI logs instead |
| MAVLink console (`SERIAL_CONTROL`) | Not available through Lima |
| Follow-me (`FOLLOW_TARGET`) | Not available |
| ADS-B traffic (`ADSB_VEHICLE`) | No nearby traffic shown on the map (its position and callsign would give away the area) |

RTK corrections from the GCS (`GPS_RTCM_DATA`) still reach the FC.

### Install Lima

`pymavlink` comes with the Rpanion `.deb` for user `rpanion`. Check:

```bash
sudo -u rpanion python3 -c "from pymavlink.dialects.v20 import ardupilotmega as m; m.enums['MAV_CMD'][16].has_location"
```

On the Pi:

```bash
sudo mkdir -p /usr/local/lib/lima
sudo wget -O /usr/local/lib/lima/lima.py https://raw.githubusercontent.com/Akceptor/rpanion-tweaks/main/python/lima.py
sudo wget -O /etc/systemd/system/lima.service https://raw.githubusercontent.com/Akceptor/rpanion-tweaks/main/systemd/lima.service
sudo chown root:root /usr/local/lib/lima/lima.py
sudo chmod 644 /usr/local/lib/lima/lima.py
sudo systemctl daemon-reload

# optional: restrict which GCS IPs Lima accepts (see LIMA_GCS_ALLOW below)
# sudo systemctl edit lima
# [Service]
# Environment=LIMA_GCS_ALLOW=<pc-zerotier-ip>

sudo systemctl enable --now lima
```

Lima is a UDP server on port 14551, next to Rpanion's own UDP Server on 14550. The Pi never sends to a GCS first: nothing goes out until a GCS sends Lima a packet, and the GCS must connect to the Pi (see below). Lima is a separate service, so Rpanion `.deb` upgrades don't touch it.

One-time Rpanion setup, **Flight Controller** page: stop telemetry, add **Telemetry Destinations** → **UDP Client** `127.0.0.1:14580` (keep `14540` and `14570`), start telemetry. Rpanion's UDP/TCP servers can stay as they are.

Then connect the GCS to the Pi (e.g. `10.46.93.18`):

- `:14551` for the masked position (Mission Planner UDPCl, or a QGC Comm Links UDP server).
- `:14550` as usual for the real position.

Note: connect to one port at a time.

Since the Pi never sends first, no inbound firewall rule is needed on the PC. If the GCS goes quiet for 5 s, Lima forgets it and stops sending until the next packet arrives.

### Check

```bash
systemctl status lima
journalctl -u lima -f
```

- With the rover having a GPS fix, the journal shows `Lima: anchor set` (or `Lima: anchor loaded` after a Lima restart), and QGC shows the rover in Lima.
- `systemctl status lima` showing exit code 2 means a setting is invalid (see `journalctl -u lima`). The service doesn't restart-loop in that case.

### Settings

Set them like the others (`sudo systemctl edit lima`, `Environment=...` lines under `[Service]`):

| Variable | Default | Meaning |
|---|---|---|
| `LIMA_SERVE` | `0.0.0.0:14551` | Where Lima listens for the GCS. Replies go to the source of the latest GCS packet |
| `LIMA_GCS_ALLOW` | unset (any IP) | Comma-separated IPv4 list, e.g. the PC's ZeroTier IP. Packets from other IPs are ignored |
| `LIMA_STATUS` | `127.0.0.1:14571` | Where Lima sends its once-a-second status for the video HUD (`STATS_LIMA_PORT`): `LIMA <0\|1> <fake_lat> <fake_lon>`, 5 decimals. Deploy Lima and the video-server together: an old HUD rejects this format and shows `Lima: OFF` |
| `LIMA_LISTEN` | `127.0.0.1:14580` | Where mavlink-router sends to (the Rpanion UDP output) |
| `LIMA_FAKE_ANCHOR` | `-12.0700,-77.0435` | Lima point the first fix maps to (Campo de Marte park, an open area) |
| `LIMA_MAX_TARGET_KM` | `5` | Uplink guard: drop GCS targets farther than this from the rover |

Changing `LIMA_FAKE_ANCHOR` takes effect on the next Lima restart. The real-side anchor only resets on a Pi reboot, so reboot the Pi when moving to a new site.

### Revert

```bash
sudo systemctl disable --now lima
```

In the Rpanion web UI, remove `127.0.0.1:14580`. Connect the GCS to `:14550` as usual.

## AGPS (faster first GPS fix)

`python/agps.py` runs on the Pi and, at every FC/GPS boot, tells the u-blox M10 through ArduPilot (`GPS_INJECT_DATA`, targeted at the FC only) to turn on AssistNow Autonomous, then sends it the current UTC time (only if the Pi's clock is NTP-synced) and an approximate position (if it has one). Nothing needs an account or a token, and nothing about the position leaves the vehicle (Pi + FC).

Position source, first that applies: the last good fix the service cached on the Pi (younger than `AGPS_CACHE_MAX_DAYS`), then `AGPS_APPROX_LAT`/`AGPS_APPROX_LON` from `/etc/agps.env`, otherwise time only. The journal logs the time from "GPS detected" to first 3D fix on every boot, so you can compare with the service off.

### What it doesn't do

- First fix ever. AssistNow Autonomous builds its predictions from ephemerides the receiver has already downloaded. A receiver that never had a fix, or whose backup supercap has run flat, only gets time and position: a warm start, not a hot one. Once, power the rover outdoors with open sky and leave it running at least 15 min after the first fix.
- Short power-offs (under about 4 h). The receiver already has everything. It matters after longer power-offs and after the supercap has drained.
- Proof of acceptance. The receiver's replies never reach the Pi, so the effect is measured (time to first fix), not confirmed.

### Map icon

Before the first 3D fix, once agps has sent the assist data and has a position, it also shows the vehicle icon on the GCS map at that approximate position. It sends a `GLOBAL_POSITION_INT` from 1/1 over its own second TCP connection to mavlink-router (`AGPS_MAP_TCP`), about 10 times a second at most and at least once a second, and stops at the first 3D fix, when the FC heartbeat is lost, or when the service stops. The message carries no motion, and `alt`/`relative_alt` are copied from the FC's own, so altitude readouts stay steady. One `AGPS: map icon approximate until GPS fix` notice goes out per boot. Nothing in logs or the notice contains coordinates.

| Phase | GCS on UDP 14550 (Lima OFF) | GCS on Lima 14551 (Lima ON) | HUD |
|---|---|---|---|
| No fix, assist sent | icon at the real approximate position | icon at Lima's fake point (`to_fake` of it when Lima has an anchor) | `≈ lat lon`, the same point as the map |
| First 3D fix | QGC keeps the approximate icon until the EKF has a position (seconds), then jumps to the real one | icon barely moves: the first fix maps to the fake point | fix line replaces `≈` |

It never reaches the autopilot: mavlink-router does not forward a 1/1 message back toward the FC (loop rule, the same one Battery 2 relies on), and ArduPilot drops messages carrying its own sysid/compid. agps never sends `GPS_RAW_INT`, `GPS_INPUT`, a GPS origin or home position.

Limits:
- The icon is up to the accuracy radius (cache 25 km, manual up to 500 km) away from the rover and looks like a real position. Do not navigate by it. It never moves, and the HUD `≈` marks it.
- Mission Planner may flash to 0,0 for a few ms when the FC's own zero position arrives first.
- GCS link-quality stats read slightly worse (the 1/1 sequence numbers interleave with the FC's).
- Rpanion's web UI, logger and the router's tlog also see the approximate position.
- If agps stops during the approximate phase, QGC keeps the icon frozen.

Rollback: set `AGPS_MAP_ICON=0` in `/etc/agps.env` and `sudo systemctl restart agps`. The HUD line and the status datagram stay.

If mavlink-router restarts, agps logs `FC link lost`, restarts its state machine and reconnects with a 1 to 10 s backoff instead of exiting.

### Privacy

The cache stores your area (rounded to ~1 km) in `/var/lib/agps/last_fix.json` on the SD card, mode 0600. `AGPS_CACHE=0` turns it off and deletes the file. Injected data goes only to the FC. Logs never contain coordinates.

### Install (on the Pi)

`/etc/agps.env` may already exist (with tokens used by other things): edit it, don't overwrite it. The service ignores variables it doesn't know. The script needs `pymavlink` for the system `python3`.

```bash
sudo mkdir -p /usr/local/lib/agps
sudo wget -O /usr/local/lib/agps/agps.py https://raw.githubusercontent.com/Akceptor/rpanion-tweaks/main/python/agps.py
sudo wget -O /etc/systemd/system/agps.service https://raw.githubusercontent.com/Akceptor/rpanion-tweaks/main/systemd/agps.service
sudo chmod 644 /usr/local/lib/agps/agps.py
sudo touch /etc/agps.env && sudo chmod 600 /etc/agps.env
sudoedit /etc/agps.env      # optional, see the settings below
sudo systemctl daemon-reload && sudo systemctl enable --now agps
```

Example `/etc/agps.env` (placeholders):

```
AGPS_APPROX_LAT=<lat, rounded to 0.1 degree>
AGPS_APPROX_LON=<lon, rounded to 0.1 degree>
AGPS_APPROX_ACC_KM=<radius of the area in km, 1..500>
```

Check:

```bash
sudo -u rpanion python3 /usr/local/lib/agps/agps.py probe   # prints u-blox ... HW: 000A0000 ...
journalctl -u agps -b                                       # "sent valset,time,pos=..." and "first 3D fix ...s"
```

Don't set `GPS_AUTO_CONFIG=3` on the FC (newer ArduPilot builds then reset the receiver config at boot).

### Settings

Set in `/etc/agps.env` (no spaces around `=`). An invalid number makes the service exit with status 2, except `AGPS_APPROX_ACC_KM`, which falls back to its default with a log line.

| Variable | Default | Meaning |
|---|---|---|
| `AGPS_ENABLE` | `1` | `0` exits at once |
| `AGPS_MAVLINK` | `tcp:127.0.0.1:5760` | pymavlink connection string |
| `AGPS_SETTLE_S` | `15` | wait after the GPS is detected |
| `AGPS_APPROX_LAT`, `AGPS_APPROX_LON` | unset | manual coarse position |
| `AGPS_APPROX_ALT_M` | `0` | ellipsoid altitude for the manual position |
| `AGPS_APPROX_ACC_KM` | `50` | accuracy radius of the manual position, 1..500. Use the radius that covers every place the rover may start from, measured from the point you typed |
| `AGPS_STATUS` | `127.0.0.1:14572` | `host:port` that gets a 1 Hz UDP line `AGPS <cache\|manual> <acc_m> <lat> <lon>` while waiting for the first 3D fix, so the video HUD can show the approximate position used. Empty disables. Invalid exits with status 2. Never logged |
| `AGPS_MAP_ICON` | `1` | `0` turns the approximate map icon off (HUD line and status datagram are unaffected) |
| `AGPS_MAP_TCP` | `127.0.0.1:5760` | mavlink-router TCP server for the separate write-only connection that carries the icon. Invalid exits with status 2 |
| `AGPS_CACHE` | `1` | `0` disables the cache and deletes the file |
| `AGPS_CACHE_FILE` | `/var/lib/agps/last_fix.json` | |
| `AGPS_CACHE_MAX_DAYS` | `14` | older cache entries are ignored |
| `AGPS_CACHE_ACC_KM` | `25` | accuracy of a cached position |

### Test tools

```bash
sudo systemctl stop agps
sudo -u rpanion python3 /usr/local/lib/agps/agps.py inject            # send the bundle once now
sudo -u rpanion python3 /usr/local/lib/agps/agps.py reset warm-noana  # hot | warm | warm-noana | cold
```

`reset` sends a GPS reset (ArduPilot loses GPS until the fix is back), so use it on the bench only. It refuses when the vehicle is armed. `cold` also wipes the AssistNow Autonomous data.

### Measuring

Outdoors, disarmed, open sky, 5 runs each: baseline (`reset warm-noana`, agps stopped), cold without assist (`reset cold`, agps stopped), cold with assist (`reset cold`, then `inject` at once), and real life (overnight power-off, agps on and off on alternate days). Time to first fix comes from `journalctl -u agps` or a stopwatch to GPS fix 3 in the GCS.

### Remove

```bash
sudo systemctl disable --now agps
```

The receiver keeps `CFG-ANA-USE_ANA=1`, which is harmless.

## Install

Rpanion 0.12 from the `.deb` lives in `/usr/share/rpanion-server/app` and runs as the `rpanion-server` service.

On the Pi:

```bash
wget -O ~/video-server.py https://raw.githubusercontent.com/Akceptor/rpanion-tweaks/main/python/video-server.py

cd /usr/share/rpanion-server/app/python
sudo cp video-server.py video-server.py.orig        # backup
sudo cp ~/video-server.py video-server.py
sudo chown rpanion:rpanion video-server.py
sudo systemctl restart rpanion-server
```

Then in the web UI: **Video** → tick **Timestamp** → stop and start the stream.

### Revert

```bash
cd /usr/share/rpanion-server/app/python
sudo cp video-server.py.orig video-server.py
sudo systemctl restart rpanion-server
```

Upgrading the Rpanion `.deb` overwrites the file — re-apply afterwards.

## Tips

**Timezone** (the clock uses system local time):

```bash
sudo ln -sf /usr/share/zoneinfo/Europe/Kyiv /etc/localtime
echo "Europe/Kyiv" | sudo tee /etc/timezone
sudo dpkg-reconfigure -f noninteractive tzdata
sudo timedatectl set-ntp true
```

The Pi has no battery-backed clock — time is correct only after NTP sync. If the stream started before sync, restart it.

**Styling:** change `Sans 10` in `video-server.py` for text size; add `shading-value=120` (0–255) to the overlays for a darker background.

## License

Based on Rpanion Server by Stephen Dade, licensed under GPL-3.0. This modified file is distributed under the same license — see [LICENSE](LICENSE).
