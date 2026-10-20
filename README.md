# rpanion-tweaks

Tweaks for [Rpanion Server](https://github.com/stephendade/Rpanion-server) **v0.12** (Debian package install).

## Video HUD overlay

A replacement `python/video-server.py` that adds a small on-screen display to the video stream:

```
20-Oct-2026 13:38:00                   wwan0 ↓0.42 ↑2.10 Mbps | LTE 78%
                              CPU 34% | 61°C | 4.1W | UNDERVOLT | 25 fps
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
