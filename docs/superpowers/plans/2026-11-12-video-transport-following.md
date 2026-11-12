# Video Transport Following Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement the tasks below in order. The approved specification is `docs/superpowers/specs/2026-11-12-video-transport-following-design.md`.

**Goal:** Keep pushed Mission Planner RTP and WebRTC available together, with automatic following of Rpanion's RTP/RTSP selector.

**Architecture:** The video producer atomically publishes a fresh local descriptor and duplicates RTP to loopback. The bridge resolves a descriptor before joining signaling and restarts its session when the descriptor changes. Explicit RTSP remains the compatibility default.

**Tech Stack:** Python standard library, GStreamer/PyGObject, existing systemd services.

## Task 1: Producer/consumer contract

Files: create `python/video_stream_state.py`, `tests/test_video_stream_state.py`.

- [x] Write tests for valid RTP/RTSP descriptors, invalid schema/ports/endpoints/codecs, stale/future timestamps, stable identities, mount naming and passthrough codec selection.
- [x] Run `/private/tmp/rpanion-video-tests/bin/python -m unittest discover -s tests -p test_video_stream_state.py -v`; observe missing API failures.
- [x] Implement immutable `VideoSource`, `read_state`, `StatePublisher`, endpoint/mount/codec helpers. Publish with same-directory atomic replace, refresh heartbeat independently of identity, remove only the owning instance.
- [x] Test publisher replacement and ownership using temporary files, repeat focused suite, commit.

## Task 2: Camera output and metadata

Files: modify `python/video-server.py`; create `tests/test_video_transport.py`.

- [x] Test destination deduplication, legacy output without environment enablement, codec/path reporting and validated fanout settings before camera startup.
- [x] Run the focused tests and observe missing fanout/publication behavior.
- [x] Replace enabled RTP sink with `multiudpsink`, setting destinations through element actions. Keep camera/HUD/encoder chain unchanged. Publish descriptors in RTP and RTSP mode; disable publication for multi-camera mode. Monitor RTP bus errors, stop pipeline and remove own state on shutdown.
- [x] Run producer tests plus `test_battery.py`; commit.

## Task 3: WebRTC sources and source following

Files: create `python/fly_ground_video.py`; modify `python/fly_ground_config.py`, `python/fly_ground_peer.py`, `python/fly_ground.py`; create `tests/test_fly_ground_source.py`; modify peer/retry/video tests.

- [x] Test legacy RTSP default, validated auto setting, no fallback on unavailable auto metadata, H.265 waiting, mode/restart detection, and heartbeat-only stability.
- [x] Test session cleanup and fast retry on changed/disappearing descriptors, and no signaling join while waiting for local video.
- [x] Implement resolver/wait helpers and `VideoSourceChanged`; thread descriptor through `run_session` and `GstPeer`. Poll source at 500 ms; use existing RC cleanup; preserve rejection backoff.
- [x] Build a loopback-only RTP input with `reuse=false` and a 20 ms jitterbuffer. Preserve the common complete-access-unit queue and use it directly in regression tests. Generalize stall wording.
- [x] Run focused source, peer, retry, settings and config tests; commit.

## Task 4: Integration and installation

Files: create `tests/test_video_fanout.py`, `systemd/rpanion-video-following.conf`; modify `README.md`.

- [x] Exercise the production sink with two real UDP receivers and synthetic H.264 RTP. Start/restart a local receiver after the sender. Verify remote receiver continues and local video survives an unreachable remote destination. Skip explicitly when real GI/plugins are unavailable.
- [x] Run GStreamer tests in a separate invocation before mock-GI unit tests; correct existing mock contamination detection if necessary.
- [x] Document shared module installation in both Python directories, runtime-directory ownership, loopback port check, one-time auto configuration, operator RTP/RTSP selection, browser reconnect limitation, H.264 restriction and rollback.
- [x] Run `/private/tmp/rpanion-video-tests/bin/python -m unittest discover -s tests -v` and `git diff --check`; record pass/skip counts and remaining Pi acceptance steps.
- [x] Request an independent code review of the diff against the approved specification; fix material findings and re-run affected checks before committing.

Pi acceptance is a deployment follow-up: actual Mission Planner reconnection, both transports, Lima HUD transitions, service ownership and a 15-minute moving-image run. Do not claim these checks from synthetic tests.


## Implementation verification (2026-11-12)

- Full suite: 409 tests passed in 42.135 seconds using the temporary Python 3.13
  environment with real GStreamer and local sockets enabled. One existing socket
  ResourceWarning remains in the suite.
- Real GStreamer tests exercised identical fanout packets, late/restarted local
  receivers, an unreachable remote destination, loopback depayload/re-payload and
  the existing complete-frame queue regression.
- Python 3.9.6: all 7 state and 5 producer tests passed.
- Independent review identified and verified fixes for a producer state ownership
  race and evaluated union annotations on Python 3.9. Re-review found no remaining
  Critical or Important findings.
- Syntax compilation and whitespace checks passed. No Pi deployment or actual
  Mission Planner/browser acceptance run was performed.
