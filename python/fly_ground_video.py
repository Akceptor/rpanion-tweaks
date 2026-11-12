"""Video input selection and session change detection for the Pi bridge."""

import asyncio
import logging
import time

from video_stream_state import VideoSource, read_state


LOG = logging.getLogger('fly-ground')
VIDEO_CAPS = 'application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000'


class VideoUnavailable(RuntimeError):
    pass


class VideoSourceChanged(RuntimeError):
    pass


def resolve_source(settings):
    if settings.video_source == 'rtsp':
        return VideoSource('explicit', 'RTSP', 'H264', rtsp_url=settings.rtsp_url)
    source = read_state(settings.video_state_file)
    if source is None:
        raise VideoUnavailable('waiting for fresh Rpanion video state')
    if source.codec != 'H264':
        raise VideoUnavailable(f'Rpanion video codec {source.codec} is unsupported by WebRTC; select H264')
    return source


async def wait_for_source(settings):
    logged_at = None
    while True:
        try:
            return resolve_source(settings)
        except VideoUnavailable as exc:
            now = time.monotonic()
            if logged_at is None or now - logged_at >= 30:
                LOG.warning('%s', exc)
                logged_at = now
            await asyncio.sleep(1)


class SourceWatcher:
    def __init__(self, settings, source):
        self.settings, self.source = settings, source
        self.next_check_ms = 0

    def poll(self, now_ms):
        if self.settings.video_source != 'auto' or now_ms < self.next_check_ms:
            return
        self.next_check_ms = now_ms + 500
        try:
            source = resolve_source(self.settings)
        except VideoUnavailable as exc:
            raise VideoSourceChanged(str(exc)) from None
        if source != self.source:
            raise VideoSourceChanged('Rpanion video source changed')


def source_chain(source):
    if source.transport == 'RTP':
        # Set address/port through properties after parsing. No untrusted launch text.
        return ('udpsrc name=source reuse=false caps="' + VIDEO_CAPS + '" '
                '! rtpjitterbuffer latency=20 do-lost=true '
                '! rtph264depay name=depay ')
    return 'rtspsrc name=source protocols=tcp latency=0 ! rtph264depay name=depay '


def video_send_chain():
    # Bound latency on complete H.264 access units, before fragmenting into RTP.
    return ('! h264parse ! video/x-h264,alignment=au '
            '! queue name=q max-size-buffers=0 max-size-bytes=0 '
            'max-size-time=300000000 leaky=downstream '
            '! rtph264pay name=pay config-interval=1 pt=96 '
            '! ' + VIDEO_CAPS + ' ')
