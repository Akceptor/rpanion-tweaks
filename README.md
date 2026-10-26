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
