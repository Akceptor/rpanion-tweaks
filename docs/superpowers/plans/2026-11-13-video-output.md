# Video output selection implementation plan

> Implement inline with independent settings work delegated using the subagent-driven-development skill. Use test-driven-development and verification-before-completion.

**Goal:** Choose WebRTC, native RTP/RTSP, or both on port 4001.

**Architecture:** A separate atomic non-secret mode file controls the camera's
output paths. The existing source state carries video availability; data-only
bridge sessions preserve controls when native-only video is selected.

**Tech stack:** Python, GStreamer, existing HTTP settings service, plain HTML/JS.

- [ ] Settings: add `video_output.py` with `OUTPUT_FILE`, `read_output_mode()` and
  `VideoOutputStore.read/save`, strict validation, atomic writes and revisions.
  Expose GET/POST `/api/video-output` and a separate selector/apply card. Preserve
  existing configuration APIs. Cover invalid mode, revision conflict and CSRF.
- [ ] Producer: test mode-specific RTP destinations, RTSP interface binding,
  camera self-restart on mode change and native-only state. Add routing and
  one-second file watcher. Default missing files to both; reject malformed files.
- [ ] Bridge: add compatible producer-state fields and test native-only source
  resolution plus data-only peer setup/offer. Preserve control/session channels
  and guard video-specific negotiation, counts and stall checks.
- [ ] Integration: run settings and pure source tests locally; run real GI video
  and bridge tests on the Pi from an isolated temporary directory. Validate JS
  syntax and unit permissions, update install docs, request independent review,
  fix findings and commit the completed feature branch.

Deployment is separate from implementation; keep the currently working phone
session untouched while developing and testing in temporary directories.
