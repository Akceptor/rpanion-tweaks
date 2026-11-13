# Video output selection

Approved in conversation: port 4001 selects WebRTC only, Rpanion only, or both.
The native option is labelled RTP only or RTSP only from the current producer
state. Rpanion continues to own transport, camera format and saved destination.

Persist a strict three-value mode (`webrtc`, `rpanion`, `both`) in a separate
non-secret JSON file, defaulting to `both` when absent. The settings API uses its
existing CSRF/origin protections and optimistic revisions. Saving an output mode
must never rewrite the Rpanion destination or the bridge credentials.

The camera process watches the mode and restarts its own video pipeline when it
changes. RTP uses only loopback for WebRTC, only the saved destination for native
output, and isolated fanout for both. RTSP binds to loopback for WebRTC only and
to the usual interface for native/both. Multiple RTSP clients share the capture
pipeline so they do not compete for the same camera. Existing multirtsp mode
keeps native RTSP behavior and explicitly reports output selection unsupported.

Publish whether WebRTC video is enabled in the producer state. The bridge keeps
its data channels available in native-only mode but creates no video receiver or
video transceiver. Switching mode changes producer instance and reconnects the
browser session without driving or arming actions.

The page shows saved mode, current transport and whether the producer has applied
the selection. Applying may briefly interrupt video; no unrelated service is
restarted. Invalid settings leave the running producer unchanged and return a
clear API error. Existing installations with no output file retain both outputs.

Validate persistence/conflicts/CSRF, RTP destinations and actual UDP delivery,
RTSP binding/shared capture, state compatibility and data-only WebRTC negotiation.
This feature does not claim to fix the unresolved intermittent camera-source stall.
