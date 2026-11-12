# Isolate video outputs implementation plan

**Goal:** A blocked external RTP destination must not stall browser video.

**Architecture:** Share capture/encode, split complete encoded access units into independent bounded queues, payloaders and UDP sockets.

**Tech stack:** Python, GStreamer, unittest, Raspberry Pi system service.

- [x] Establish bridge baseline (89 tests, one local ICE skip).
- [x] Add real GStreamer blocked-output regression in tests/test_video_fanout.py; observe zero local frames on the old shared sink.
- [x] Add optional payload=False to getPipeline and build independent AU queues/payloader/sinks in python/video-server.py; keep legacy output unchanged.
- [x] Verify blocked-output regression passes, healthy datagrams remain equal and late receivers work.
- [x] Extend fragment-integrity, inverse blocking, H265 selection and destination deduplication coverage; real H265 runtime remains untested.
- [x] Run all 110 affected video/transport and bridge tests on real Pi; all pass.
- [x] Independent review approved after adding live synthetic-source pacing and its regression test.
- [ ] Commit, publish and merge the fix.
- [ ] Hash-guard a single-file deployment with backup/rollback; restart only the camera child with its exact existing arguments and service identity.
- [ ] Verify installed bytes, protected files, fresh video state, and passive camera frame timing.
