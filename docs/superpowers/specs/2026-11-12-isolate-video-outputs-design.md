# Isolate RTP video destinations

Approved scope: fix browser video stalls caused by a blocked Mission Planner UDP destination. Keep the camera, encoder, settings, authentication and WebRTC codec negotiation unchanged.

Use one encoded source, parse complete H264/H265 access units, then split into two bounded downstream-leaky queues. Each branch owns its RTP payloader and UDP sink/socket. Queues precede packetization so overload discards complete access units, never selected fragments. Both payloaders start with shared random RTP offsets for identical packets during healthy fanout; after branch-specific drops sequence counters advance independently. UDP sinks do not wait on the clock or preroll.

A shared multiudpsink cannot isolate socket backpressure. Splitting after RTP with leaky queues risks dropping partial frames. Splitting encoded access units adds a second lightweight payloader while keeping one encoder and bounded memory.

Legacy single-destination RTP and RTSP keep their existing pipelines. Duplicate destinations remain deduplicated. H264 and H265 use their corresponding parser/payloader.

Validate deterministic blocked-output isolation, healthy packet equality, late receivers, recovery without partial RTP frames, codec selection, and bridge regressions on the Pi. Deploy only video-server.py with a hash guard and rollback, restarting only the camera child. Verify passive loopback timing after deployment; browser performance requires a user session.
