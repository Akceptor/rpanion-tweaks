# RTP output with automatic WebRTC input selection

Date: 2026-11-12
Status: approved for implementation on 2026-11-12.

## Goal

Let Mission Planner receive pushed RTP video and resume receiving after a temporary
network interruption, while WebRTC remains available. Preserve Rpanion's existing
RTP/RTSP selector so the operator can switch back to RTSP. WebRTC follows that
selection automatically. One Lima coordinate mode applies to all viewers at a time.

## Existing behavior and boundaries

- `python/video-server.py:getPipeline()` builds the camera, HUD, encoder and RTP
  payloader. The HUD already reads Lima status and burns the selected coordinates
  into the image before encoding.
- The RTP branch uses one `udpsink` with the destination from `--udp`.
- The RTSP branch derives the mount path from the camera source. It explicitly
  uses `set_shared(False)`, so different RTSP clients can create separate camera
  pipelines. This change does not promise one encoder across RTSP clients.
- `python/fly_ground_peer.py:GstPeer` currently reads H.264 through `rtspsrc`,
  depayloads and parses it, bounds the queue on complete H.264 access units, then
  payloads it for `webrtcbin`. Preserve that complete-frame queue behavior.
- `python/fly_ground.py` owns session retries and RC release during cleanup.
  Its current recovery creates a new peer/session. A transport change may require
  the browser's existing reconnect flow; automatic input selection does not mean
  uninterrupted WebRTC sessions or automatic driving resumption.
- Mission Planner's MAVLink port choice (direct or Lima) remains separate from
  its video receiver port. Rpanion's existing RTP destination fields still control
  the remote video destination.
- No application source is changed or deployed by this planning document.

## Chosen approach and alternatives

Add native RTP input to the Pi's WebRTC bridge. In RTP mode, duplicate the encoded
RTP packets to Mission Planner and a private loopback receiver. In RTSP mode,
retain the existing RTSP server and client input.

A local RTP-to-RTSP relay would preserve the bridge's current RTSP input, but adds
another component and lifecycle to manage. Keeping RTSP as the only camera output
and forwarding it to Mission Planner would not make Rpanion's selected RTP mode
work with WebRTC. Neither alternative is selected.

GStreamer's existing `multiudpsink` supports sending RTP to multiple destinations:
https://gstreamer.freedesktop.org/documentation/udp/multiudpsink.html

## Data flow

RTP mode:

```text
camera -> existing Lima-aware HUD -> encoder -> RTP payloader -> multiudpsink
                                                              |-> configured Mission Planner IP:port
                                                              `-> 127.0.0.1:5601 -> WebRTC bridge
```

RTSP mode:

```text
existing Rpanion RTSP server -> Mission Planner RTSP client
                            `-> WebRTC bridge RTSP client
```

The RTP path has one camera/overlay/encoder pipeline. The extra destination needs
no decode or encode and adds loopback traffic rather than another remote upload.
WebRTC still sends its own network stream as it does today. Existing RTSP client
sharing behavior is outside this change.

## RTP output

Replace the RTP branch's single destination sink with `multiudpsink`. Configure
destinations with element properties or action signals, rather than inserting
unvalidated host text into a GStreamer launch string. Keep the remote destination
from `--udp`; add `127.0.0.1:5601` when local WebRTC fanout is enabled. Deduplicate
identical destinations and preserve supported remote multicast behavior.

Use `RPANION_WEBRTC_RTP_PORT` for the local port; the installation sets 5601.
An absent variable disables fanout. Validate a supplied port as 1..65535. Enable the feature explicitly in the
installation service drop-in; standalone replacement-script users retain their
existing single-destination behavior until configured. Reject conflicting or
invalid configuration before starting the camera. Verify the chosen port is free
on the deployment Pi before enabling it.

Keep periodic codec headers and the current encoder keyframe settings. Receiving
after link restoration is expected to resume at a subsequent decodable keyframe;
it is not guaranteed to resume on the first packet. Validate the behavior with
the actual Mission Planner receiver and codec settings.

## Authoritative local stream description

Add a small standard-library module, `python/video_stream_state.py`, shared by
the video server and bridge. Install it beside `video-server.py` and in the bridge's
Python directory. It owns JSON serialization, schema validation, endpoint parsing,
freshness checks, and atomic publication. It has no GI imports.

Publish `/run/rpanion-video/state.json` through an atomic replacement in the same
directory. The Rpanion service drop-in creates the directory using
`RuntimeDirectory=rpanion-video` and mode 0755. Confirm the installed service's
user and child-process identity can write there; the bridge needs read access only.
Do not rely on a shared `/tmp` directory because the bridge uses `PrivateTmp=true`.

Schema version 1 contains:

```json
{
  "version": 1,
  "instance": "unique-id-for-this-video-process",
  "updated_monotonic": 12345.0,
  "transport": "RTP",
  "codec": "H264",
  "rtp_host": "127.0.0.1",
  "rtp_port": 5601,
  "rtsp_url": null
}
```

RTSP descriptions instead contain a loopback RTSP URL with the actual mount path;
RTP fields are null. Derive the mount path through one helper used by both the
RTSP registration and publisher. Report the effective codec, including compressed
camera passthrough, rather than assuming `--compression` always describes output.

Publication is enabled by `RPANION_VIDEO_STATE_FILE` in the service drop-in. Refresh
once per second from the GLib loop. A description older than three seconds, from
the future, malformed, unsupported, or missing is unavailable in automatic mode.
The timestamp uses the same Pi's monotonic clock; `/run` clears on reboot.
The heartbeat describes the running video process, not proof that camera frames
are flowing. Keep the separate five-second frame-stall check.

Generate a new instance identifier on each video-process start. Compare descriptor
identity using instance, mode, codec and endpoint; heartbeat timestamps alone must
not cause input rebuilds. On orderly shutdown, remove state only if its instance
still belongs to this process, preventing an old process from deleting newer state.
An unclean exit expires through the three-second freshness check. Publication and
cleanup serialize using a persistent sibling lock file with a producer owner ID;
a superseded producer cannot republish, and its loop stops on the next refresh.

This first version follows the single-camera selector. Do not publish a misleading
single endpoint for `--multirtsp`; log that automatic following is unsupported for
that mode, and leave explicit RTSP input available for those installations.

## Bridge input selection

Introduce `FLY_VIDEO_SOURCE=auto|rtsp`. Preserve `rtsp` as the backward-compatible
default; installation of this feature sets `auto` once. Operators subsequently
change only Rpanion's existing mode selector. Preserve `FLY_RTSP_URL` for explicit
RTSP mode and rollback.

In automatic mode, read the state path from `FLY_VIDEO_STATE_FILE`, default
`/run/rpanion-video/state.json`. Do not silently fall back to a guessed RTSP URL
when state is absent or stale. Wait locally for a valid H.264 descriptor before
joining the signaling room. Unsupported codecs produce a clear, rate-limited
diagnostic; they do not crash Rpanion or stop its Mission Planner output.

For RTP use a source chain equivalent to:

```text
udpsrc address=127.0.0.1 port=<published-port> reuse=false caps=<H264 RTP caps>
  -> rtpjitterbuffer latency=20 do-lost=true
  -> rtph264depay -> h264parse -> existing complete-access-unit queue
  -> existing RTP payloader -> webrtcbin
```

Use a small bounded jitterbuffer delay for the local hop. Do not introduce a
leaky queue on fragmented RTP packets. Bind only loopback and disable socket reuse
so a competing receiver fails visibly rather than silently sharing packets.
Start with 20 ms jitterbuffer latency and measure recovery/latency on the Pi.
GStreamer documents reordering, lost-packet events and configurable delay here:
https://gstreamer.freedesktop.org/documentation/rtpmanager/rtpjitterbuffer.html

For RTSP retain the existing TCP source chain, using the published URL in automatic
mode or `FLY_RTSP_URL` in explicit mode. Keep H.264-only WebRTC negotiation,
diagnostics, queue limits, ICE handling and data-channel configuration.

Poll state at most once per 500 ms during a session. A changed identity or
unavailable descriptor ends the current session using its existing cleanup path,
releases any browser RC overrides and closes the UDP socket before another peer
is created. Re-read state before the next session. Distinguish a source change
from an ordinary connection failure so it resets recovery to a one-second delay;
retain the signaling server's rejection backoff. A temporary missing file must
not create a signaling join loop.

After a five-second frame stall or a GStreamer error, use existing cleanup and
retry behavior, then re-read the descriptor. Preserve fresh neutral control and
explicit Resume driving after reconnect. Do not add browser code or promise
automatic browser rejoining in this change; exercise the existing reconnect flow
and document whether the operator must use Refresh connection.

## Lima and coordinate behavior

Keep coordinate transformation, status freshness, anchor handling and Timestamp
checkbox behavior unchanged. Fanout happens after the HUD and encoder, so Mission
Planner and WebRTC receive identical burned-in coordinates in RTP mode. Validate
both Lima ON and OFF, including the existing no-anchor behavior. There is no
per-viewer coordinate choice and no second encoding pipeline for coordinates.

## Implementation sequence

1. Add the pure stream-state module and tests for descriptor validation, heartbeat
   freshness, stable identities, atomic writes and instance-owned cleanup.
2. Integrate descriptor publication and RTP fanout into `python/video-server.py`.
   Extract only mount-name/effective-codec helpers needed by this feature. Test
   preserved RTSP mount naming, correct codec reporting and destination handling.
3. Add source configuration and descriptor resolution to the bridge; preserve
   explicit RTSP behavior and existing settings-page round trips. The settings
   page preserves unknown environment assignments, so no new UI is required.
4. Update `GstPeer` for RTP or RTSP source descriptors. Keep the common encoded
   send chain unchanged. Generalize RTSP-only stall/error wording to video input.
5. Add mode-change monitoring and local-wait handling in `fly_ground.py`, exercising
   RC cleanup, UDP socket ownership, signaling rejection backoff and new-instance
   recovery even when transport and endpoint are unchanged.
6. Add `systemd/rpanion-video-following.conf`, install instructions, compatibility
   notes, Mission Planner setup, switching instructions and rollback in README.
7. Run repository tests and GStreamer integration checks, then validate on the Pi
   before considering the behavior ready for operational use.

## Verification and acceptance

Pure tests must cover missing/corrupt/stale/future state, unknown schema/codec,
invalid ports, loopback-only local endpoints, heartbeat-only changes, producer
restart identities, old-producer cleanup, effective H.264/H.265 reporting, and
deduplicated destinations. Test metadata-disabled legacy behavior explicitly.

Bridge tests must cover RTSP default compatibility, automatic mode waiting without
signaling churn, both source chains, changed/disappearing descriptors, frame-stall
recovery, release of browser overrides and the existing explicit Resume requirement.
Preserve all existing settings-page tests, including unknown environment lines.

On a GStreamer host, exercise production pipeline builders with `videotestsrc`
rather than a camera: two UDP receivers receive RTP from one sender; the local
receiver starts after the sender; stopping/restarting the local receiver does not
stop the remote receiver; an unreachable remote destination does not stop local
video. Continue the complete-H.264-frame queue regression test using the production
common send-chain builder instead of brittle constructor AST extraction if needed.

Run the complete standard-library suite with:

```sh
python3 -m unittest discover -s tests -v
git diff --check
```

Report integration-test skips as skips, not hardware verification. On the Pi, check
required GStreamer plugins and runtime-directory ownership, then validate:

1. RTP reaches Mission Planner and WebRTC simultaneously with matching HUD data.
2. A temporary Mission Planner network interruption followed by restoration recovers
   video without reopening its RTP receiver, once a decodable keyframe arrives.
3. WebRTC connects after RTP has already started and recovers after video restart.
4. RTP -> RTSP -> RTP changes are selected automatically by the bridge; record the
   browser reconnect action required and confirm each reconnect needs Resume.
5. Lima ON/OFF, missing anchor and direct-vs-Lima telemetry choices preserve the
   existing coordinate behavior in both video transports.
6. H.265 can continue serving Mission Planner while WebRTC reports unsupported codec.
7. Repeat a 15-minute moving-image run; compare CPU, latency and corruption counters
   with the existing bridge and confirm there is one camera encoder in RTP mode.

## Deployment and rollback

Deploy the shared module before either dependent script. Back up the current video
server and bridge files. Verify installed service identity, install the runtime
directory drop-in, and enable publication plus local fanout. Set the bridge to
automatic mode only after observing a valid state file. Restart the relevant
services and verify both transport choices from the existing Rpanion page.

Keep installation paths consistent with the existing README: video server and
shared module under `/usr/share/rpanion-server/app/python`, bridge Python under
`/opt/fly-ground/python`. Verify the video script's exact installed location before
copying to account for installations that differ from the documented Debian package.

Rollback sets `FLY_VIDEO_SOURCE=rtsp`, retains/restores `FLY_RTSP_URL`, selects RTSP
in Rpanion and restarts the bridge. Removing fanout/publication environment settings
restores the video server's legacy transport behavior. Keep the untracked existing
private-TURN plan and unrelated application settings untouched.
