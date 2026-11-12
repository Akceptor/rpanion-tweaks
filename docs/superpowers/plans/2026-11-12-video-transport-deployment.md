# Pi video transport deployment — 2026-11-12

Deployed source: `5fa9b74b83f0193ea7a00cf42614cc2e658616b0` on
`feat/video-transport-following`, including `origin/main` at `7b00d25` to preserve
private TURN authentication and WebRTC's Lima writer / separate monitor routing.
The merged build passed all 443 tests and independent merge review.

Pi: `pi@10.46.93.18`. Backup of affected files and resolved bridge environment:
`/var/backups/rpanion-video-following/20261112T145252Z-5fa9b74`.
The backup directory is root-only and includes the restoration manifest.
Installed-file hashes and source revision are recorded on the Pi in
`/var/lib/rpanion-video-release.json`.

Installed the shared state helper in both application Python directories, the
video producer and changed bridge modules, and the Rpanion runtime-directory
service drop-in. Enabled `FLY_VIDEO_SOURCE=auto` and the shared state path in the
resolved `/etc/fly-ground.env` target. Existing settings and host authentication
were verified preserved; unrelated authentication, settings and MAVLink modules
were checked against their pre-deployment hashes.

The live mode was RTP by deployment time. Preserved its destination
`10.46.90.178:5400`, codec H.264 and existing camera/HUD settings. Verified fresh
metadata with RTP loopback port 5601 and correct runtime-directory ownership.
Rpanion, fly-ground, fly-ground-settings and Lima were all active.

Passive five-second loopback capture observed 686 RTP packets, 316841 payload
bytes and 124 frame markers, without binding the bridge's UDP port. No WebRTC
viewer was connected during this check; no loopback UDP receiver was present,
which is expected because the authenticated bridge creates a peer on admission.
No authentication failures were observed after the final deployment.

Deployment recovery notes: an incorrect daemon-reload invocation initially
interrupted the restart sequence; originals were restored and services restarted.
A subsequent RTSP-only verification assertion rejected the now-selected RTP mode
and automatically rolled back. The final deployment derived the selected mode
from the live producer, preserved it and completed successfully.

The user confirmed the deployed setup works after the service and packet checks.

Not independently verified here: receiving/decoding on the Mission Planner PC, browser video
negotiation, live RTP-to-RTSP-to-RTP switching, Lima HUD visual transitions, or the
15-minute moving-image acceptance run. No driving commands were sent.
