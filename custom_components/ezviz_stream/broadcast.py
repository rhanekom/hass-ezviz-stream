"""
On-demand MPEG-TS broadcaster: one EZVIZ cloud session per camera, fanned out.

HA opens ``stream_source()`` from two places at once - go2rtc (for WebRTC) and the
built-in ``stream`` component (for HLS) - and go2rtc fans the result out to every
browser. Opening a separate cloud session per consumer would trip EZVIZ's VTDU
concurrency limits (result 5405/5452), so each camera has a single upstream session
here: its RTP/HEVC stream is remuxed to MPEG-TS by FFmpeg (stream copy, no
transcode) and broadcast to all current HTTP subscribers. The session starts on the
first subscriber and stops when the last one leaves, so a camera only streams while
something is watching (battery-friendly).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from .stream import iter_annexb

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from .api import EzvizCloudApi

_LOGGER = logging.getLogger(__name__)

_TS_READ = 65536  # bytes per read from FFmpeg's MPEG-TS output
_QUEUE_MAX = 512  # per-subscriber backlog; a slow consumer drops its oldest chunks
_FFMPEG_TERM_TIMEOUT = 5.0
# EZVIZ battery cams stream constant-rate 15 fps on the main stream. Raw Annex-B HEVC
# carries no timing, so we declare the rate to FFmpeg for a clean CFR timeline; TODO
# (D): detect per stream / make configurable when sub-stream + codec options land.
_STREAM_FPS = 15


async def mpegts_source(
    api: EzvizCloudApi,
    serial: str,
    ffmpeg_bin: str,
    *,
    stream: int,
    verification_code: str,  # noqa: ARG001 - reserved for the encrypted-IPC path (C.2b)
) -> AsyncIterator[bytes]:
    """
    Yield MPEG-TS chunks: the camera's RTP/HEVC remuxed by FFmpeg (copy, no transcode).

    Resolves the camera fresh (for current VTM routing), feeds its Annex-B HEVC into
    ``ffmpeg -f hevc -i pipe:0 -c copy -f mpegts pipe:1``, and yields the TS output.
    Runs until the consumer stops iterating; FFmpeg is torn down on exit. RTP/HEVC
    (battery cams) only for now - encrypted MPEG-PS (IPC) needs continuous decryption
    plus a remux (C.2b), which is why ``verification_code`` is threaded through.
    """
    camera = next(
        (cam for cam in await api.async_get_cameras() if cam.serial == serial), None
    )
    if camera is None:
        _LOGGER.warning("camera %s not found on the account", serial)
        return

    ffmpeg = await asyncio.create_subprocess_exec(
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "nobuffer",
        # Raw Annex-B HEVC carries no container timestamps, so with -c copy ffmpeg
        # would otherwise invent a PTS timeline at its default 25 fps - too fast for a
        # 15 fps camera (buffer drains, periodic rebuffer). Declaring the real input
        # frame rate gives a clean constant-rate PTS timeline, which plays smoother
        # than wall-clock stamping (the VTDU delivers in bursts, so arrival-time
        # stamps jitter and the player skips to catch up).
        "-r",
        str(_STREAM_FPS),
        "-f",
        "hevc",
        "-i",
        "pipe:0",
        "-c:v",
        "copy",
        "-f",
        "mpegts",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    feeder = asyncio.create_task(
        _feed_hevc(camera, api.async_get_vtdu_token, ffmpeg, stream)
    )
    try:
        assert ffmpeg.stdout is not None  # noqa: S101 - PIPE guarantees a reader
        while True:
            chunk = await ffmpeg.stdout.read(_TS_READ)
            if not chunk:
                break
            yield chunk
    finally:
        feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feeder
        await _terminate(ffmpeg)


async def _feed_hevc(
    camera: object,
    token_factory: object,
    ffmpeg: asyncio.subprocess.Process,
    stream: int,
) -> None:
    """Pump the camera's Annex-B HEVC into FFmpeg's stdin until cancelled."""
    assert ffmpeg.stdin is not None  # noqa: S101 - PIPE guarantees a writer
    try:
        async for chunk in iter_annexb(camera, token_factory, stream=stream):  # type: ignore[arg-type]
            ffmpeg.stdin.write(chunk)
            await ffmpeg.stdin.drain()
    except BrokenPipeError, ConnectionResetError:
        pass  # FFmpeg went away; the reader side will notice EOF and clean up
    finally:
        with contextlib.suppress(OSError):
            ffmpeg.stdin.close()


async def _terminate(ffmpeg: asyncio.subprocess.Process) -> None:
    """Stop FFmpeg, escalating to kill if it does not exit promptly."""
    if ffmpeg.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        ffmpeg.terminate()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(ffmpeg.wait(), _FFMPEG_TERM_TIMEOUT)
    if ffmpeg.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            ffmpeg.kill()


class CameraBroadcast:
    """Fan one on-demand MPEG-TS source out to many subscribers."""

    def __init__(self, source_factory: Callable[[], AsyncIterator[bytes]]) -> None:
        """Store the factory that (re)creates the upstream MPEG-TS source."""
        self._source_factory = source_factory
        self._subscribers: set[asyncio.Queue[bytes | None]] = set()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def subscribe(self) -> AsyncIterator[bytes]:
        """
        Yield MPEG-TS chunks for one consumer, sharing the single upstream session.

        The first subscriber starts the upstream; the last to leave stops it.
        """
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_QUEUE_MAX)
        async with self._lock:
            self._subscribers.add(queue)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:  # upstream ended
                    return
                yield chunk
        finally:
            async with self._lock:
                self._subscribers.discard(queue)
                if not self._subscribers and self._task is not None:
                    self._task.cancel()
                    self._task = None

    async def async_stop(self) -> None:
        """Stop the upstream and release all subscribers (on entity removal)."""
        async with self._lock:
            task, self._task = self._task, None
            subs = list(self._subscribers)
            self._subscribers.clear()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for queue in subs:
            _offer(queue, None)

    async def _run(self) -> None:
        """Pull the upstream source and push each chunk to every subscriber."""
        try:
            async for chunk in self._source_factory():
                for queue in list(self._subscribers):
                    _offer(queue, chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("broadcast upstream for camera failed")
        finally:
            for queue in list(self._subscribers):
                _offer(queue, None)  # signal end-of-stream to consumers


def _offer(queue: asyncio.Queue[bytes | None], item: bytes | None) -> None:
    """Enqueue ``item``, dropping the subscriber's oldest chunk when it is full."""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(item)
