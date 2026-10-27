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
| `fps` | frames reaching the encoder | Per viewer |
| `FC ...` (bottom left) | MAVLink `SYS_STATUS` / `BATTERY_STATUS` over UDP | Flight controller battery. Hidden if no data for 5 s. See [Battery overlay](#battery-overlay) |
| `PI ...` (bottom left) | INA226 on I2C | Companion battery, measured before the BEC. Hidden if the sensor is absent. See [Battery overlay](#battery-overlay) |

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

Enable I2C, give the service user access and check that the sensor is seen (`0x40` by default):

```bash
sudo raspi-config nonint do_i2c 0
sudo usermod -aG i2c rpanion
sudo apt install i2c-tools
i2cdetect -y 1
```

Log out/in or restart `rpanion-server` after the group change.

**Replace the shunt resistor.** Most modules ship with R100 (0.1 Ω). The INA226 maxes out at 81.92 mV across the shunt, so that is only ~0.82 A, while a Pi 5 plus a modem on 2S draws about 1.3 A. Swap it for R010 (0.01 Ω, ~8 A range) and set `INA226_SHUNT_OHMS=0.01`. When the reading is clipped, the overlay shows `0.8A+` instead of a wrong number.

### Settings

Set them like `STATS_IFACE` above (`sudo systemctl edit rpanion-server`, then add `Environment=...` lines under `[Service]`):

| Variable | Default | Meaning |
|---|---|---|
| `STATS_MAVLINK_PORT` | `14570` | UDP port on 127.0.0.1 for the FC battery |
| `INA226_BUS` | `1` | I2C bus (`/dev/i2c-N`) |
| `INA226_ADDR` | `0x40` | I2C address |
| `INA226_SHUNT_OHMS` | `0.1` | Shunt resistance. `0.01` for R010 |
| `PI_BATT_CELLS` | `2` | Cells in the Pi battery, for the %. `0` hides the % |

The Pi battery % is a voltage estimate. It sags under load, so expect it to read low while the modem is busy. The table is for LiPo; Li-ion packs will read low.

## Lima (fake position on the GCS map)

`python/lima.py` is a MAVLink proxy that sits on the GCS link only. The ground station (QGC, Mission Planner) shows the rover in Lima, Peru, moving exactly as it really moves. Commands from the GCS (missions, "drive here", set home, fences) are shifted back, so the rover drives where you meant.

```
FC ─ mavlink-routerd ─┬─ 14540 Rpanion, 14541 DF logger, 14570 HUD   (real)
                      └─ 127.0.0.1:14580 ─ Lima ─ UDP ─ GCS PC       (fake)
```

- The rover's first good GPS fix (3D or better) after Lima starts maps to the Lima point. No reference point to set.
- Until that fix, position messages are dropped in both directions: the GCS shows no position rather than the real one.
- The shift is in metres, so distances and headings on the map match reality. Altitude is unchanged.
- Fails closed: a crash, a missing setting, or a message with a position Lima can't convert means the GCS gets nothing, never the real value.
- After converting a GCS target back, anything more than `LIMA_MAX_TARGET_KM` from the rover is dropped and the GCS gets a `STATUSTEXT` warning.
- MAVLink2 signing must be off (the ArduPilot default), because rewriting breaks signatures.

### What it doesn't hide

- Logs (Rpanion DataFlash logger, FC SD card), NTRIP, cloud upload, the Rpanion web UI: all stay real.
- The camera image.
- **The GCS's own location.** Turn off Windows location services and any USB GPS in QGC/Mission Planner before streaming, or the PC's real position shows on the map.
- Any GCS connection that bypasses Lima (Rpanion TCP server `5760`, UDP server, direct GCS UDP output). The setup below turns these off; Lima doesn't police them.

### Saved missions shift

The anchor lives in `/run/lima/` (tmpfs), so it is cleared when the Pi reboots and a new one is taken at the next first fix. A mission saved in the GCS and loaded after a Pi reboot lands shifted by the distance between the two start points. Only the `LIMA_MAX_TARGET_KM` guard catches it, and only for big shifts. Missions stored on the FC are fine (converted fresh on each download).

If Lima alone restarts, it reuses the anchor and the icon doesn't jump.

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

# optional: only accept GCS packets from the PC's ZeroTier IP
# sudo systemctl edit lima
# [Service]
# Environment=LIMA_GCS_ALLOW=<pc-zerotier-ip>

sudo systemctl enable --now lima
```

Lima is a UDP server on port 14550, like Rpanion's own UDP Server. The Pi never sends to a GCS first: nothing goes out until a GCS sends Lima a packet, and the GCS must connect to the Pi (see below). Lima is a separate service, so Rpanion `.deb` upgrades don't touch it.

Then in Rpanion's web UI, **Flight Controller** page (stop telemetry first):

1. Remove the direct GCS UDP output.
2. **Telemetry Destinations** → **UDP Client** → add `127.0.0.1:14580`.
3. Keep `127.0.0.1:14570` (the HUD) and `127.0.0.1:14540`.
4. Untick **Enable UDP Server** and **Enable TCP Server at port 5760**. Both bypass Lima, and the UDP server would also clash with Lima on port 14550.
5. Start telemetry.

On the GCS (the PC connects to the Pi):

- QGroundControl: Application Settings → Comm Links → Add → Type UDP, add Server `<Pi ZeroTier IP>:14550` (e.g. `10.46.93.18:14550`), Connect.
- Mission Planner: connection type UDPCI, host the Pi IP, port 14550.

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
| `LIMA_SERVE` | `0.0.0.0:14550` | Where Lima listens for the GCS. Replies go to the source of the latest GCS packet |
| `LIMA_GCS_ALLOW` | unset (any IP) | Comma-separated IPv4 list, e.g. the PC's ZeroTier IP. Packets from other IPs are ignored |
| `LIMA_LISTEN` | `127.0.0.1:14580` | Where mavlink-router sends to (the Rpanion UDP output) |
| `LIMA_FAKE_ANCHOR` | `-12.0700,-77.0435` | Lima point the first fix maps to (Campo de Marte park, an open area) |
| `LIMA_MAX_TARGET_KM` | `5` | Uplink guard: drop GCS targets farther than this from the rover |

Changing `LIMA_FAKE_ANCHOR` takes effect on the next Lima restart. The real-side anchor only resets on a Pi reboot, so reboot the Pi when moving to a new site.

### Revert

```bash
sudo systemctl disable --now lima
```

In the Rpanion web UI, remove `127.0.0.1:14580` and add the direct GCS UDP output back.

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
