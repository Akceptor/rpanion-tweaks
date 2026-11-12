# Phone H.264 negotiation startup race

Observed on Samsung S26 Chrome through TURN: intermittent 0–2 rendered fps,
zero reported packet loss, ~2 ms/frame decode time, and repeated keyframe requests.
The browser negotiated packetization-mode=0/profile-level-id=42e01f, while the
sender payloader reported packetization-mode=1/High Profile 64001e.

A standalone generated-image pipeline reproduced the startup race: requesting an
offer before the first encoded caps yielded no fmtp line; waiting for the encoder
produced an offer containing its actual profile and packetization mode.

GstPeer now records the offer request and completes it only after the payloader
has fixed caps containing both fields. The normal bus poll retries readiness;
existing video-stall detection bounds a missing source. The camera format,
queue placement, control protocol, authentication, and TURN preferences stay intact.

Verification: timing unit regressions failed on the original implementation.
All 89 bridge tests passed on the real Pi, including a generated-image GStreamer
regression verifying the negotiated SDP matches actual encoded caps. Phone
frame rate and delay after deployment remain an operator acceptance check.
