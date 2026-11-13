# Video output selection implementation plan

> Implement inline with independent settings work delegated using the subagent-driven-development skill. Use test-driven-development and verification-before-completion.

**Goal:** Choose WebRTC, native RTP/RTSP, or both on port 4001.

**Architecture:** A separate atomic non-secret mode file controls the camera's
output paths. The existing source state carries video availability; data-only
bridge sessions preserve controls when native-only video is selected.

**Tech stack:** Python, GStreamer, existing HTTP settings service, plain HTML/JS.

- [x] Settings: add `video_output.py` with `OUTPUT_FILE`, `read_output_mode()` and
  `VideoOutputStore.read/save`, strict validation, atomic writes and revisions.
  Expose GET/POST `/api/video-output` and a separate selector/apply card. Preserve
  existing configuration APIs. Cover invalid mode, revision conflict and CSRF.
- [x] Producer: test mode-specific RTP destinations, RTSP interface binding,
  camera self-restart on mode change and native-only state. Add routing and
  one-second file watcher. Default missing files to both; reject malformed files.
- [x] Bridge: add compatible producer-state fields and test native-only source
  resolution plus data-only peer setup/offer. Preserve control/session channels
  and guard video-specific negotiation, counts and stall checks.
- [x] Integration: run settings and pure source tests locally; run real GI video
  and bridge tests on the Pi from an isolated temporary directory. Validate JS
  syntax and unit permissions, update install docs, request independent review,
  fix findings and commit the completed feature branch.

Deployment is separate from implementation; keep the currently working phone
session untouched while developing and testing in temporary directories.

## Verification

- Final affected-suite run: 132 tests passed on the actual Pi from an isolated
  temporary directory (`/tmp/fanout-tests-z_rln59m`); no deployed files changed.
- Real GStreamer tests include selected UDP delivery, simultaneous shared RTSP
  clients, application-only WebRTC SDP with preserved data channels, and producer
  self-reexec using isolated state/config paths and synthetic video.
- Settings JavaScript syntax, concurrent-edit regression, and diff checks passed.
- Independent spec and code-quality reviews approved after fixing unchecked RTSP
  bind failure, legacy explicit RTSP selection, basic-install module omission and
  selector polling revision handling.
- Earlier full-suite runs intermittently failed the existing exact-RTP-datagram
  comparison; isolated fanout tests and the final full run passed. Added safe
  sequence-number diagnostics without weakening its assertion. Independent AU
  drops can advance the branches' sequence counters separately; the observed
  failures were not proven to be caused by that mechanism.
- Native visual preview was not completed: Chrome was in use and the separate
  browser host was unavailable. API, markup and JavaScript behavior were checked.
- Implementation is committed on the feature branch; production deployment and
  live camera/phone acceptance of the new selector remain separate work.
