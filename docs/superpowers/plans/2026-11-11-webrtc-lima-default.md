# WebRTC Lima default implementation plan

**Goal:** Route browser MAVLink traffic through Lima by default, including masked telemetry, without taking Mission Planner's Lima endpoint away.

**Architecture:** Add a loopback-only UDP endpoint at 127.0.0.1:14552 to the existing Lima process. It shares the rewriter and status with the public GCS endpoint but keeps its own client and expiry. Fly-ground uses one UDP connection for bidirectional Lima traffic and a separate read-only TCP connection for target discovery and Mission Planner priority. Browser telemetry comes exclusively from the configured writer route. Send a GCS heartbeat while a browser session is connected, including while control ownership is idle, to register/retain its Lima endpoint.

**Tech stack:** Python, pymavlink, UDP sockets, systemd, unittest.

## Steps

- [x] Extend `tests/test_lima_net.py` with a local endpoint fixture and regressions proving local/remote clients both receive masked telemetry, local expiry preserves a remote client, and loopback registration sets Lima status. Run on Pi and observe failures before adding the endpoint to `python/lima.py`. Keep existing public latest-client semantics and allowlist.
- [x] Add fly-ground defaults/connection tests: default `router_url == 'udpout:127.0.0.1:14552'`, `monitor_url == 'tcp:127.0.0.1:5760'`; reject input/listening UDP writer routes; verify writer and monitor construction and cleanup. Update `python/fly_ground_config.py`, `python/fly_ground.py`, and settings schema/page.
- [x] Add adapter regressions proving raw-monitor coordinates never reach browser callbacks, Lima coordinates do, and idle connected sessions send heartbeats without RC overrides. Retain target discovery and Mission Planner preemption on the raw monitor. Implement in `python/fly_ground_mavlink.py` and run the full fly-ground suite on Pi.
- [x] Update documentation and systemd Lima ordering. Run all Lima tests and fly-ground tests on Pi in a temporary directory; review code. Deploy only while control is idle, preserving current configuration and source backups. Update only the relevant endpoint settings without printing room codes or coordinates. Verify both services, local UDP listener, and non-coordinate Lima status; do not transmit driving commands for deployment verification.
- [x] Commit and open a Pi-repository PR. Report that the browser repository needs no Lima-routing changes, and include actual validation/deployment results.
