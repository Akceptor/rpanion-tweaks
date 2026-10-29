# Approximate Map Icon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Before the first GPS fix, the GCS shows the vehicle icon at agps's approximate position (fake point through Lima), and the HUD shows the same point as `≈ lat lon`.

**Architecture:** agps sends a 1/1 `GLOBAL_POSITION_INT` at the approximate centre over a second, write-only TCP connection to mavlink-router while it is in state `SENT` without a fix. The router's loop rule keeps it off the FC, and ArduPilot drops its own sysid/compid anyway. Lima replaces any non-zero `GLOBAL_POSITION_INT` with its fake anchor point while it has no anchor, and takes `real_pos` from `GPS_RAW_INT` only. The HUD formats the approximate point through a new `approx_display` policy that matches the map.

**Tech Stack:** Python 3.11, stdlib + `pymavlink` (agps, Lima), stdlib only (video-server), `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-29-approx-icon-design.md`

## Global Constraints

- Nothing new reaches the FC: only `GLOBAL_POSITION_INT` from 1/1, untargeted, on the separate connection. Never `GPS_RAW_INT`, `GPS_INPUT`, `SET_GPS_GLOBAL_ORIGIN`, `SET_HOME_POSITION`, `COMMAND_*`.
- Lima anchors only on `GPS_RAW_INT` `fix_type >= 3` with non-zero lat/lon. `real_pos` comes only from the same messages.
- Lima output never contains a real coordinate without an anchor: `GLOBAL_POSITION_INT` gets the fake point; every other position message is dropped.
- No coordinates in logs or STATUSTEXT. STATUSTEXT text exactly `AGPS: map icon approximate until GPS fix`, severity `MAV_SEVERITY_NOTICE`.
- Spoof fields: lat/lon = `round(pos * 1e7)`; `alt`, `relative_alt`, `time_boot_ms` copied from the FC's latest non-echo `GLOBAL_POSITION_INT` (fallback `round(pos.alt_m*1000)`, `0`, `0`); `vx=vy=vz=0`; `hdg=65535`.
- Active: `state == "SENT"` and `chosen_pos` set and not `good_fix` and FC HEARTBEAT ≤ 3 s old and `AGPS_MAP_ICON != "0"`. Event send ≥ 0.1 s apart; floor 1 s.
- Settings `AGPS_MAP_ICON` (default `1`), `AGPS_MAP_TCP` (default `127.0.0.1:5760`, invalid → exit 2).
- Lima status datagram `LIMA <0|1> <fake_lat:.5f> <fake_lon:.5f>`. Deploy and revert Lima and video-server together: the current HUD rejects the new datagram, reads `Lima: OFF` and would print real coordinates on the video while Lima is ON. HUD approx line `≈ {lat:.2f} {lon:.2f}`, no source or accuracy.
- Repo style: comments only for real traps; tests in `tests/`, coordinates `47.0, 8.0` real and `-12.07, -77.0435` fake; run `.venv/bin/python -m unittest discover -s tests -v` (create with `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt` if missing).

## Review Focus

1. The router echoes the spoof back to agps's 1/193 connection → agps must not copy its own `alt`/`time_boot_ms` back into the next spoof. Test in Task 1 (`test_own_echo_ignored`).
2. A fix arrives, then a spoof sent just before it reaches Lima after the fix → anchor and `real_pos` stay at the real fix. Test in Task 4 (`test_late_spoof_after_fix_does_not_move_anchor_or_guard`).
3. Lima restarted mid-session with a stored anchor while agps spoofs → the GCS gets `to_fake(approx)` and the uplink guard still rejects a target near the approx point. Test in Task 4 (`test_stored_anchor_spoof_converted_guard_unmoved`).
4. FC reboots during `SENT` (heartbeat gap) → spoof stops at once and starts again only after the new `SENT`. Test in Task 2 (`test_stops_on_heartbeat_loss`).
5. Old-format `LIMA 1` datagram (Lima not yet updated) with no anchor → HUD shows no `≈` line rather than real coordinates. Test in Task 5 (`test_approx_hidden_when_fake_unknown`).

---

### Task 1: agps — spoof frame builder and FC field tracking

**Files:**
- Modify: `python/agps.py` (new section after `status_datagram`)
- Test: `tests/test_agps_icon.py` (new; reuse `msg`, `heartbeat`, `gps` from `test_agps_runner`)

**Interfaces — Produces:**
- `STATUSTEXT_ICON = b"AGPS: map icon approximate until GPS fix"`
- `class ApproxIcon(send, clock=time.monotonic)`: `send(frame: bytes)`.
  - `fc_position(m) -> None`: remembers `alt`, `relative_alt`, `time_boot_ms` of a 1/1 `GLOBAL_POSITION_INT`, unless its lat/lon equal the last spoofed lat/lon (own echo).
  - `update(pos: Position | None, active: bool, event: bool) -> None`: if `active` and `pos`, sends a frame when `event` and ≥ 0.1 s since the last send, or when ≥ 1 s since the last send. Not active → resets the timer so the next activation sends at once.
  - `frame(pos) -> bytes`: MAVLink 2 `GLOBAL_POSITION_INT` packed with `mavlink.MAVLink(None, srcSystem=1, srcComponent=1)` (own seq increments).

- [ ] **Step 1: Write failing tests** in `tests/test_agps_icon.py` (`POS = agps.Position(47.0, 8.0, 400.0, 25000, "cache")`, decode frames with a fresh `mavlink.MAVLink(None)`):
  - `test_frame_fields`: no FC data → `msgid 33`, src `(1, 1)`, `lat == 470000000`, `lon == 80000000`, `alt == 400000`, `relative_alt == 0`, `time_boot_ms == 0`, `(vx, vy, vz) == (0, 0, 0)`, `hdg == 65535`, first byte `0xFD`.
  - `test_copies_fc_altitude`: after `fc_position(gpi(lat=0, lon=0, alt=123456, relative_alt=-50, time_boot_ms=99000))` → frame has `alt 123456`, `relative_alt -50`, `time_boot_ms 99000`, still `vx=vy=vz=0`, `hdg 65535`.
  - `test_own_echo_ignored`: after one `frame(POS)`, `fc_position(gpi(lat=470000000, lon=80000000, alt=1, relative_alt=1, time_boot_ms=1))` → next frame keeps the previous FC values (or the fallbacks).
  - `test_seq_increments`: two frames have seq 0 and 1.
  - `test_never_gps_raw_int`: over 50 `update(...)` calls with events, every sent frame decodes to `GLOBAL_POSITION_INT` only.
  - `test_rate`: clock-driven: `update(POS, True, event=True)` at t=0 sends; at t=0.05 with event does not; at t=0.1 with event does; with no events, sends at t=1.1 and t=2.1, not between.
  - `test_inactive_sends_nothing_and_rearms`: `update(POS, False, True)` sends nothing; `update(None, True, True)` sends nothing; after inactive, the first active call sends at once.
- [ ] **Step 2:** Run `.venv/bin/python -m unittest tests.test_agps_icon -v` → FAIL (`ApproxIcon` missing).
- [ ] **Step 3: Implement** `ApproxIcon` and `STATUSTEXT_ICON` in `python/agps.py`. Use `mavlink.MAVLink_global_position_int_message(...).pack(mav)`.
- [ ] **Step 4:** Run the same command → PASS.
- [ ] **Step 5: Commit** `feat(agps): approximate-position GLOBAL_POSITION_INT builder`.

### Task 2: agps — Runner drives the icon

**Files:**
- Modify: `python/agps.py` (`Runner.__init__`, `_restart`, `handle`, `tick`)
- Test: `tests/test_agps_runner.py` (new `IconTest(RunnerBase)`; extend `make()` with `icon=True` that builds `agps.ApproxIcon(lambda f: self.icon_frames.append((self.t, f)), clock=lambda: self.t)`)

**Interfaces:**
- Consumes: `ApproxIcon`, `STATUSTEXT_ICON` (Task 1).
- Produces: `Runner(..., icon=None)`; `Runner.icon_active(now) -> bool`; `ICON_HB_MAX_S = 3`.

- [ ] **Step 1: Write failing tests** (use `RunnerBase.at`; FC GPI helper `gpi(lat=0, lon=0, src=(1,1))`):
  - `test_icon_while_sent_only`: no frames before settle (t < 15 with fix 1); frames from t=15 (bundle sent with manual position); decode lat/lon `47.0, 8.0`.
  - `test_event_send_on_fc_messages`: in `SENT`, an FC GPS_RAW_INT fix 1 at t=20.5 produces a frame stamped 20.5; an FC GPI at t=20.55 does not (0.1 s gap); at t=20.7 it does.
  - `test_stops_on_first_fix`: `gps(3)` at t=40 → no frame at or after t=40, state `DONE`.
  - `test_stops_on_heartbeat_loss`: in `SENT`, stop heartbeats (`hb=False`) → no frame once the last HEARTBEAT is > 3 s old; after the >10 s gap restart and a new `SENT`, frames resume.
  - `test_no_icon_without_position`: env with no manual and no cache → no frames.
  - `test_runner_without_icon`: `make(icon=False)` (Runner gets `icon=None`) runs a full boot → no error, no STATUSTEXT. The `AGPS_MAP_ICON=0` wiring is tested in Task 3.
  - `test_foreign_gpi_ignored`: GPI from `(1, 193)` or `(255, 190)` with `alt=777` does not change the copied altitude.
  - `test_statustext_once_per_sent`: `conn.mav.statustext_send` called once with `(MAV_SEVERITY_NOTICE, STATUSTEXT_ICON)` on the first active tick; not again in the same `SENT`; once more after FC reboot → new `SENT`. Extend `FakeMav` with `statustext_send(sev, text)` recording calls.
  - `test_no_coordinates_logged` (extend the existing logs test): with the icon on, log output has no `47.` / `8.0`.
- [ ] **Step 2:** Run `.venv/bin/python -m unittest tests.test_agps_runner -v` → new tests FAIL.
- [ ] **Step 3: Implement.** `handle`: on a 1/1 `GLOBAL_POSITION_INT` call `icon.fc_position(m)` and mark an event; on a 1/1 `GPS_RAW_INT` mark an event after `_on_gps`. `tick`: `icon.update(self.chosen_pos, self.icon_active(now), event)` and clear the event; send the STATUSTEXT on the first active tick per `_restart` cycle (`self.icon_announced`). `icon_active(now)`: the condition in Global Constraints (without the setting; the setting decides whether `icon` exists). `handle` must call `icon.update` immediately for events (not wait for `tick`) so the send follows the FC message; `tick` covers the 1 s floor.
- [ ] **Step 4:** Run the full suite → PASS (old Runner tests unchanged with `icon=None`).
- [ ] **Step 5: Commit** `feat(agps): show approximate position on the GCS map until first fix`.

### Task 3: agps — separate TCP connection, settings, wiring, README

**Files:**
- Modify: `python/agps.py` (`Settings`, new `TcpFrameSender`, `main`)
- Modify: `README.md` (AGPS section: "Map icon" subsection, settings rows, rollback line)
- Test: `tests/test_agps_settings.py`, `tests/test_agps_cli.py`, `tests/test_agps_icon.py`

**Interfaces:**
- Consumes: `ApproxIcon`, `Runner(icon=...)`.
- Produces: `Settings.map_icon: bool`, `Settings.map_tcp: tuple[str, int]`; `class TcpFrameSender(threading.Thread)` with `(host, port)`, `connected` property, `send(frame)`. Same behaviour as `Batt2Sender` in `python/video-server.py:365-425` (connect timeout 3 s, recv drain with 2 s timeout, reconnect at most every 10 s, `send` is a no-op while disconnected, drops the socket on `OSError`). Copy the Batt2Sender trap comment in a short form: the router stops forwarding 1/1 to a connection that has sent 1/1, so the spoof must never use the 1/193 telemetry connection.

- [ ] **Step 1: Write failing tests:**
  - settings: `test_map_icon_defaults` (`map_icon is True`, `map_tcp == ("127.0.0.1", 5760)`); `test_map_icon_off` (`AGPS_MAP_ICON=0`); `test_map_tcp_invalid` (`"nope"`, `"h:0"`, `"h:x"` → `ValueError`).
  - cli: `test_bad_map_tcp_exits_2`; `test_run_builds_icon_on_separate_connection`: patch `agps.TcpFrameSender` with a fake that records `(host, port)` and `start()`; `main(["run"], env)` with a `QueueConn` that stops the loop → fake constructed with `("127.0.0.1", 5760)` and started, Runner's `icon` not None; with `AGPS_MAP_ICON=0` the fake is never constructed. `connect` is still called exactly once with `source_component=193`.
  - icon: `test_tcp_sender_real_socket`: a local listening socket; `TcpFrameSender` connects, `send(b"abc")` arrives; server writes 10 kB → sender keeps working (drained); server closes → `connected` turns False (pattern: `tests/test_battery.py::test_real_socket_drains_and_delivers_frame`).
- [ ] **Step 2:** Run the three test modules → FAIL.
- [ ] **Step 3: Implement** settings parsing (reuse the `_status_addr` host:port rules), `TcpFrameSender`, and in `main` `run`: if `settings.map_icon`, start a `TcpFrameSender(*settings.map_tcp)` and pass `ApproxIcon(sender.send)` to `Runner`.
- [ ] **Step 4:** Run the full suite → PASS.
- [ ] **Step 5: README:** in the AGPS section add "Map icon": what the GCS shows per Lima state (the spec's "What the user sees" table, shortened), that it never reaches the FC (router loop rule + ArduPilot drops its own sysid/compid), MP may flash to 0,0, the QGC icon lingers until the EKF has a position, the risk note (not for navigation), and the `AGPS_MAP_ICON` / `AGPS_MAP_TCP` rows. Rollback: `AGPS_MAP_ICON=0` + `sudo systemctl restart agps`.
- [ ] **Step 6: Commit** `feat(agps): send the map icon over its own router connection`.

### Task 4: Lima — fake point before the anchor, guard from GPS only, status with fake point

**Files:**
- Modify: `python/lima.py` (`Rewriter._handle`, `Rewriter._track`, `run` status send)
- Test: `tests/test_lima_downlink.py`, `tests/test_lima_uplink.py`, `tests/test_lima_net.py`
- Modify: `README.md` (Lima section: before-the-first-fix behaviour; status format)

**Interfaces — Produces:** `lima.status_datagram(on: bool, fake: tuple[float, float]) -> bytes` → `b"LIMA 1 -12.07000 -77.04350"`.

- [ ] **Step 1: Write failing tests** in `tests/test_lima_downlink.py`:
  - `test_no_anchor_gpi_shows_fake_point`: `rewriter().downlink(frame(gpi(*REAL)))` → one message, `lat == round(FAKE[0]*1e7)`, `lon == round(FAKE[1]*1e7)`, `alt/relative_alt/vx/vy/vz/hdg` unchanged, header `(1, 1, seq)` kept; and `struct.pack("<i", round(REAL[0]*1e7))` and the lon equivalent are not in the output bytes.
  - `test_no_anchor_gpi_v1_stays_v1`: same with `v1=True` → first byte `0xFE`, fake point.
  - `test_no_anchor_zero_gpi_untouched`: `gpi(0, 0)` → bytes identical to input.
  - `test_no_anchor_other_positions_still_dropped`: replace the GPI assertion in `test_no_anchor_drops_positions` with `HOME_POSITION` at REAL and `GPS_RAW_INT` fix 2 at REAL → `[]` (keep the HEARTBEAT pass assertion).
  - `test_gpi_never_anchors`: 100 × `gpi(50.0, 30.0)` → `rw.anchor is None`, anchor file does not exist, `rw.real_pos is None`.
  - `test_spoof_then_fix_anchors_on_fix`: `gpi(50.0, 30.0)` ×3, then `gps(*REAL, 3)` → `anchor.real_lat/real_lon == REAL`, stored anchor equal.
  - `test_late_spoof_after_fix_does_not_move_anchor_or_guard`: `gps(*REAL, 3)` then `gpi(50.0, 30.0)` → anchor real == REAL, `real_pos == REAL`; the GPI output is `to_fake(50, 30)` (converted, as for any position with an anchor).
  - `test_real_pos_from_gps_only`: replaces `test_tracks_real_pos`: after anchoring, `gpi(47.001, 8.002)` leaves `real_pos == REAL`; `gps(47.001, 8.002, 3)` sets it; `gps(47.5, 8.5, 2)` doesn't.
  - Keep `test_track_ignores_invalid_latlon` passing as is.
  In `tests/test_lima_uplink.py`:
  - `test_stored_anchor_spoof_converted_guard_unmoved`: store `Anchor(*REAL, *FAKE)`, new Rewriter, `gps(*REAL, 3)` (sets `real_pos`), then downlink `gpi(47.2, 8.0)` (~22 km north, the spoof) → uplink `MISSION_ITEM_INT` at `to_fake(47.2, 8.0)` is dropped with a `km` warning, and one at `to_fake` of a point 100 m from REAL passes.
  - Fix any helper that seeds `real_pos` via GPI (none today; `anchored()` uses `gps`).
  In `tests/test_lima_net.py`: change every `b"LIMA 0"`/`b"LIMA 1"` expectation to `lima.status_datagram(False/True, FAKE)` (the test harness's Rewriter fake anchor), and add `test_status_datagram_format`: `status_datagram(True, (-12.07, -77.0435)) == b"LIMA 1 -12.07000 -77.04350"`.
- [ ] **Step 2:** Run `.venv/bin/python -m unittest tests.test_lima_downlink tests.test_lima_uplink tests.test_lima_net -v` → FAIL.
- [ ] **Step 3: Implement.** In `_handle`, in the `convert` branch: if `to_fake` and `name == "GLOBAL_POSITION_INT"` and not `_unset(...)` and `self.anchor is None` → set `lat`/`lon` to `round(fake * 1e7)` and `return repack(msg, self._out)`. In `_track`: drop the `GLOBAL_POSITION_INT` branch; `real_pos` and the anchor come from `GPS_RAW_INT` fix ≥ 3 only. In `run`: send `status_datagram(state, rewriter.fake_anchor)`. One comment on the `_track` change: GLOBAL_POSITION_INT may be agps's approximate spoof, so it must never feed the anchor or the guard.
- [ ] **Step 4:** Run the full suite (including `test_lima_policy`) → PASS.
- [ ] **Step 5: README** (Lima): before the first fix the icon sits on `LIMA_FAKE_ANCHOR` when agps has an approximate position; the uplink guard uses GPS fixes only; the status line format.
- [ ] **Step 6: Commit** `feat(lima): show the fake point before the first fix, never anchor on GLOBAL_POSITION_INT`.

### Task 5: HUD — `≈ lat lon` matching the map

**Files:**
- Modify: `python/video-server.py` (`apply_lima`, new `lima_fake`, new `approx_display`, `approx_line`, `gps_line`, `SystemStats.run`)
- Test: `tests/test_battery.py` (`HudLimaCoordsTests`)
- Modify: `README.md` (HUD table row for GPS)

**Interfaces:**
- Consumes: Lima status `LIMA <0|1> <lat> <lon>` (Task 4).
- Produces: `apply_lima(state, data, now)` also sets `state["fake"] = (lat, lon)` or `None` (two-token form); `lima_fake(state, now) -> tuple | None` (same 3 s freshness as `lima_on`); `approx_display(approx, on: bool, anchor, fake) -> tuple[float, float] | None`; `approx_line(latlon) -> str`; `gps_line(state, now, coord, approx_latlon=None)`.

- [ ] **Step 1: Write failing tests** (replace `test_approx_line_only_without_fix`, `test_approx_line_km_rounding`, `test_approx_line_shifted_and_hidden`):
  - `test_apply_lima_new_and_old_format`: `b"LIMA 1 -12.07000 -77.04350"` → on, fake `(-12.07, -77.0435)`; `b"LIMA 1"` → on, fake None; `b"LIMA 1 x y"`, `b"LIMA 1 -91 0"`, `b"LIMA 1 nan 0"`, `b"LIMA 2 0 0"` → state unchanged.
  - `test_approx_display`: `ap = ("cache", 25000, 50.01, 24.0)`; OFF → `(50.01, 24.0)` (also with an anchor); ON + anchor `HudAnchor(50, 24, -12, -77)` → `to_fake(50.01, 24.0)`; ON + no anchor + fake → `(-12.07, -77.0435)`; ON + no anchor + fake None → None; `approx=None` → None.
  - `test_approx_line_format`: `approx_line((-12.07, -77.0435)) == "≈ -12.07 -77.04"`; `approx_line((50.5, 24.25)) == "≈ 50.50 24.25"`; no `±`, `km`, `cache`, `manual` in either.
  - `test_approx_only_without_fix`: `gps_line(fix=1 state, 100, IDENT, (50.5, 24.25))` last line `"≈ 50.50 24.25"`; fix 2 and 3 → no `≈`; `approx_latlon=None` → no `≈`.
  - `test_fix_coords_never_use_fake_point`: ON + no anchor + fake known, fix 3 → `gps_line(st, 100, coord_policy(True, None), ...)` has no coordinate line (unchanged hiding).
  - `test_approx_hidden_when_fake_unknown`: old `b"LIMA 1"` + no anchor + approx → `gps_line` has no `≈` line.
- [ ] **Step 2:** Run `.venv/bin/python -m unittest tests.test_battery -v` → FAIL.
- [ ] **Step 3: Implement** the functions. In `SystemStats.run`: `on = lima_on(...)`, `approx = approx_display(agps_approx(...), on, anchor, lima_fake(...))`, `gps_line(self.mavlink.state, now, coord_policy(on, anchor), approx)`. `parse_agps` stays as is (datagram format unchanged; source/acc are no longer displayed).
- [ ] **Step 4:** Run the full suite → PASS.
- [ ] **Step 5: README:** HUD GPS row: `≈ lat lon` before the fix, the same point as the GCS map (real with Lima OFF, Lima's fake point or shifted with Lima ON). Remove `(cache ±N km)`.
- [ ] **Step 6: Commit** `feat(hud): approximate position as plain ≈ lat lon, matching the map`.

### Task 6: Hardware verification (user runs on the Pi; no repo changes unless something fails)

Deploy (Lima and video-server in the same step, see Global Constraints): copy `python/agps.py` → `/usr/local/lib/agps/agps.py`, `python/lima.py` and `python/video-server.py` to their installed paths, `sudo systemctl restart agps lima` and the video service. GPS indoors (no fix) or antenna covered. Cache or manual position present (`journalctl -u agps` shows `pos=cache|manual`).

- [ ] **Step 1: FC untouched.** `journalctl -u agps -f` shows `sent valset,time,pos=…` then no errors. In QGC MAVLink Inspector (Lima OFF), `GLOBAL_POSITION_INT` from 1/1 alternates between the FC's `0,0` and ours. ArduPilot stays healthy: no new prearm or EKF messages, `HOME_POSITION` absent/unchanged, `EKF_STATUS_REPORT` flags unchanged versus `AGPS_MAP_ICON=0`.
- [ ] **Step 2: QGC, Lima OFF** (14550): icon appears ~15 s after GPS detected at the approx point, steady for ≥ 2 min, no jumps to 0,0. Altitude readout steady. Notification `AGPS: map icon approximate until GPS fix` once (record if QGC doesn't show it → spec Unverified). HUD: `≈ <same 2 decimals>`.
- [ ] **Step 3: QGC, Lima ON** (14551), right after a Pi reboot (no `/run/lima/anchor.json`): icon at the Lima fake point; HUD `≈ -12.07 -77.04`. MAVLink Inspector shows no non-fake coordinate in any message. `/run/lima/anchor.json` still absent while the spoof runs.
- [ ] **Step 4: Fix transition.** Take the rover outside. At the first 3D fix: HUD switches to the fix line; QGC Lima ON: icon stays near the fake point and starts tracking; anchor file now exists. Check (as `rpanion`) that its `real_lat/real_lon` is within ~50 m of the HUD fix shown with Lima OFF (compare in private, don't log it). Note how long QGC Lima OFF kept the approx icon after the fix.
- [ ] **Step 5: Lima ON with anchor**: with the Pi up, power-cycle only the FC indoors. After `SENT`, QGC Lima ON shows `to_fake(approx)`; HUD the same 2 decimals. A "Go to location" click near that icon is refused by Lima (`Lima: dropped … km` or `no position`), never sent.
- [ ] **Step 6: Mission Planner** (secondary), Lima OFF: icon at the approx point; note how often it flashes to 0,0.
- [ ] **Step 7: Rollback check:** `AGPS_MAP_ICON=0`, restart agps → after an FC power-cycle no icon before the fix; the HUD `≈` line remains.
