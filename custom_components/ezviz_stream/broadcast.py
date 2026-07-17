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
import re
from typing import TYPE_CHECKING

from .decrypt_stream import StreamingPsDecryptor, decrypt_ps_audio, decrypt_ps_video
from .stream import iter_annexb, iter_ps_decrypted

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable

    from .api import EzvizCloudApi

_LOGGER = logging.getLogger(__name__)

_TS_READ = 65536  # bytes per read from FFmpeg's MPEG-TS output
_QUEUE_MAX = 512  # per-subscriber backlog; a slow consumer drops its oldest chunks
_FFMPEG_TERM_TIMEOUT = 5.0
# Playback pacing (see _Pacer): the RTP timestamp is a 90 kHz clock; a step that is
# negative or larger than this (2 s) means a discontinuity - a reconnect's fresh RTP
# base or a 32-bit wrap - so we rebase the schedule to "now" instead of replaying it.
_RTP_CLOCK = 90000
_RTP_DISCONTINUITY = 2 * _RTP_CLOCK
_MAX_PACE_SLEEP = 2.0  # safety cap on a single wait, so a bad timestamp can't stall us

# Per-clip decryption auto-detection (see maybe_decrypt_replay).
_PROBE_BYTES = 768 * 1024  # buffer roughly one keyframe before deciding
_PROBE_FRAMES = 24  # cap the decode probe; a valid interpretation reaches this easily
_PROBE_MIN_FRAMES = 2  # garbage decodes ~0 frames, cleanly separating it from valid


async def _spawn_ffmpeg(
    ffmpeg_bin: str, input_fmt: str, *, wallclock: bool, transcode: bool = False
) -> asyncio.subprocess.Process:
    """
    Start ``ffmpeg -f <input_fmt> -i pipe:0 <video codec> -f mpegts pipe:1``.

    ``wallclock`` stamps input frames with their arrival time - needed for raw HEVC
    (no container timestamps); MPEG-PS already carries PTS so it is left off.
    ``transcode`` re-encodes the video to H.264 (browser-universal) instead of copying
    the camera's native HEVC; audio is copied through either way. It is CPU-heavy, so
    it is opt-in per camera (see ``CONF_FORCE_H264``).
    """
    args = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer"]
    if wallclock:
        args += ["-use_wallclock_as_timestamps", "1"]
    args += ["-f", input_fmt, "-i", "pipe:0"]
    if transcode:
        # ultrafast/zerolatency keep encode latency and CPU as low as a live H.264
        # encode allows; -g 30 caps the keyframe gap (~2 s at 15 fps) for quick player
        # start and clean keyframe snapshots. Audio (if any) is copied untouched.
        args += [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "30",
            "-c:a",
            "copy",
        ]
    else:
        args += ["-c", "copy"]
    args += ["-f", "mpegts", "pipe:1"]
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def mpegts_source(  # noqa: PLR0913 - one live source needs api, camera + tuning
    api: EzvizCloudApi,
    serial: str,
    ffmpeg_bin: str,
    *,
    stream: int,
    verification_code: str,
    transcode: bool = False,
) -> AsyncIterator[bytes]:
    """
    Yield MPEG-TS chunks remuxed from the camera's live stream.

    Resolves the camera fresh (for current VTM routing), then picks the path by
    transport: battery cams stream raw RTP/HEVC (Annex-B, paced to the RTP clock and
    wall-clock stamped by :func:`_feed_rtp`); other cams stream MPEG-PS, which
    :func:`_feed_ps` decrypts on the fly (Image Encryption) and which already carries
    PTS. Both remux to MPEG-TS. With ``transcode`` the video is re-encoded to H.264
    (browser-universal, CPU-heavy) instead of copied as native HEVC. Runs until the
    consumer stops iterating; FFmpeg is torn down on exit.
    """
    camera = next(
        (cam for cam in await api.async_get_cameras() if cam.serial == serial), None
    )
    if camera is None:
        _LOGGER.warning("camera %s not found on the account", serial)
        return

    token_factory = api.async_get_vtdu_token
    if camera.is_battery:
        ffmpeg = await _spawn_ffmpeg(
            ffmpeg_bin, "hevc", wallclock=True, transcode=transcode
        )
        feeder = asyncio.create_task(_feed_rtp(camera, token_factory, ffmpeg, stream))
    else:
        ffmpeg = await _spawn_ffmpeg(
            ffmpeg_bin, "mpeg", wallclock=False, transcode=transcode
        )
        feeder = asyncio.create_task(
            _feed_ps(camera, token_factory, ffmpeg, stream, verification_code)
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


async def mp4_replay_source(
    ffmpeg_bin: str, ps_source: AsyncIterator[bytes], *, audio: bool = False
) -> AsyncIterator[bytes]:
    """
    Transcode a decrypted recording MPEG-PS clip to fragmented H.264 MP4.

    ``ps_source`` is the decrypted MPEG-PS from a cloud
    (:func:`cloud_replay.iter_cloud_replay_ps`) or SD (:func:`stream.iter_playback_ps`)
    recording. ffmpeg re-encodes the video HEVC->H.264 (browser-universal) into a
    fragmented MP4 that streams progressively to a browser ``<video>``. ``audio``
    re-encodes the clip's AAC track (plaintext on an unencrypted camera, decrypted
    upstream on an encrypted one); it is a no-op when the camera has audio disabled
    (no audio stream). Runs until ``ps_source`` is exhausted; ffmpeg is torn down on
    exit.

    ``-g 30`` caps the keyframe interval (~1-2 s). ``frag_keyframe`` flushes an MP4
    fragment only at each keyframe, so without this cap libx264's default 250-frame
    GOP means a short or static clip (no scene-cut keyframe) produces a single
    fragment that is never flushed until EOF - the browser only ever receives the
    init segment and playback never starts. The cap makes fragments flush regularly
    so progressive playback works for every clip.
    """
    audio_args = ["-c:a", "aac"] if audio else ["-an"]
    ffmpeg = await asyncio.create_subprocess_exec(
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "mpeg",
        "-i",
        "pipe:0",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "30",
        *audio_args,
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert ffmpeg.stdin is not None  # noqa: S101 - PIPE guarantees a writer
    assert ffmpeg.stdout is not None  # noqa: S101 - PIPE guarantees a reader
    stdin = ffmpeg.stdin

    async def _feed() -> None:
        try:
            async for chunk in ps_source:
                stdin.write(chunk)
                await stdin.drain()
        except BrokenPipeError, ConnectionResetError:
            pass  # ffmpeg exited; the reader side will see EOF and clean up
        finally:
            with contextlib.suppress(OSError):
                stdin.close()

    feeder = asyncio.create_task(_feed())
    try:
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


async def _probe_frame_count(ffmpeg_bin: str, ps: bytes) -> int:
    """
    Return how many video frames FFmpeg decodes from an MPEG-PS ``ps`` sample.

    A valid interpretation of the sample decodes many frames (capped at
    :data:`_PROBE_FRAMES`); garbage (wrong/absent decryption) decodes ~0. Audio is
    ignored (``-an``) so an undecryptable audio track cannot mask the video verdict.
    """
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_bin,
        "-hide_banner",
        "-v",
        "error",
        "-f",
        "mpeg",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-an",
        "-frames:v",
        str(_PROBE_FRAMES),
        "-f",
        "null",
        "-",
        "-progress",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await proc.communicate(ps)
    except BrokenPipeError, ConnectionResetError:
        return 0
    finally:
        # Always reap the probe process so its subprocess transport is closed on this
        # loop. Left to GC (e.g. when the clip is abandoned mid-probe), the transport's
        # __del__ can run on another thread and raise on Python 3.14.
        await _terminate(proc)
    matches = re.findall(rb"frame=\s*(\d+)", out)
    return int(matches[-1]) if matches else 0


async def _sample_needs_decrypt(
    ffmpeg_bin: str, sample: bytes, verification_code: str
) -> bool:
    """
    Decide whether a recording's first-keyframe ``sample`` must be decrypted.

    Returns False if it already decodes as valid video (plaintext - decrypting would
    corrupt it) or if it decodes neither raw nor decrypted (almost certainly an
    old/unknown key, logged and served best-effort); True only when decrypting with
    ``verification_code`` is what makes it decode.
    """
    if await _probe_frame_count(ffmpeg_bin, sample) >= _PROBE_MIN_FRAMES:
        return False
    video = await asyncio.to_thread(decrypt_ps_video, sample, verification_code)
    decrypted = await asyncio.to_thread(decrypt_ps_audio, video, verification_code)
    if await _probe_frame_count(ffmpeg_bin, decrypted) >= _PROBE_MIN_FRAMES:
        return True
    _LOGGER.warning(
        "Recording clip decoded neither raw nor decrypted - it was likely recorded "
        "with a different encryption code than the one configured; serving it "
        "undecrypted (playback may fail)",
    )
    return False


async def maybe_decrypt_replay(
    ffmpeg_bin: str,
    raw_source: AsyncIterator[bytes],
    verification_code: str,
) -> AsyncGenerator[bytes]:
    """
    Yield a recording's MPEG-PS, decrypting per-clip only when it is actually needed.

    A camera's verification code can change over its lifetime - encryption toggled
    on/off, or the code rotated - so a single camera's clips may be a mix of
    plaintext, encrypted with the current code, and encrypted with an old code we no
    longer hold. Trusting the per-clip ``crypt`` / per-camera ``is_encrypted`` flag and
    decrypting unconditionally corrupts the plaintext clips. Instead we buffer the
    first keyframe, decode-probe it (see :func:`_sample_needs_decrypt`), and stream it
    raw or decrypted accordingly. With no ``verification_code`` there is nothing to
    try, so the stream passes straight through.
    """
    if not verification_code:
        async for chunk in raw_source:
            yield chunk
        return

    buffer = bytearray()
    exhausted = True
    async for chunk in raw_source:
        buffer += chunk
        if len(buffer) >= _PROBE_BYTES:
            exhausted = False
            break
    sample = bytes(buffer)

    if not await _sample_needs_decrypt(ffmpeg_bin, sample, verification_code):
        yield sample
        if not exhausted:
            async for chunk in raw_source:
                yield chunk
        return

    decryptor = StreamingPsDecryptor(verification_code, decrypt_audio=True)
    first = await asyncio.to_thread(decryptor.feed, sample)
    if first:
        yield first
    if not exhausted:
        async for chunk in raw_source:
            out = await asyncio.to_thread(decryptor.feed, chunk)
            if out:
                yield out
    tail = await asyncio.to_thread(decryptor.flush)
    if tail:
        yield tail


def _s32(value: int) -> int:
    """Interpret a 32-bit RTP-timestamp delta as signed (handles the clock wrap)."""
    return ((value + 0x80000000) & 0xFFFFFFFF) - 0x80000000


class _Pacer:
    """
    Turn a camera's RTP 90 kHz timestamps into a real-time release schedule.

    :meth:`delay` returns how long to wait before releasing the frame stamped ``ts``
    so playback follows the camera's own cadence, smoothing the VTDU's bursty
    delivery. The first frame - and any discontinuity, i.e. a reconnect's fresh RTP
    base or a 32-bit wrap - rebases the schedule to ``now`` (play immediately) rather
    than trying to replay a gap.
    """

    def __init__(self) -> None:
        """Start unscheduled; the first frame establishes the base."""
        self._rtp_base: int | None = None
        self._wall_base = 0.0
        self._last_ts = 0

    def delay(self, ts: int, now: float) -> float:
        """Seconds to wait before releasing the frame stamped ``ts`` at time ``now``."""
        if self._rtp_base is None or not (
            0 <= _s32(ts - self._last_ts) <= _RTP_DISCONTINUITY
        ):
            self._rtp_base = ts
            self._wall_base = now
            self._last_ts = ts
            return 0.0
        self._last_ts = ts
        target = self._wall_base + _s32(ts - self._rtp_base) / _RTP_CLOCK
        return max(0.0, target - now)


async def _feed_rtp(
    camera: object,
    token_factory: object,
    ffmpeg: asyncio.subprocess.Process,
    stream: int,
) -> None:
    """Pace the camera's Annex-B HEVC into FFmpeg's stdin on its RTP clock."""
    assert ffmpeg.stdin is not None  # noqa: S101 - PIPE guarantees a writer
    loop = asyncio.get_running_loop()
    pacer = _Pacer()
    try:
        async for rtp_ts, chunk in iter_annexb(camera, token_factory, stream=stream):  # type: ignore[arg-type]
            delay = pacer.delay(rtp_ts, loop.time())
            if delay > 0:
                await asyncio.sleep(min(delay, _MAX_PACE_SLEEP))
            ffmpeg.stdin.write(chunk)
            await ffmpeg.stdin.drain()
    except BrokenPipeError, ConnectionResetError:
        pass  # FFmpeg went away; the reader side will notice EOF and clean up
    finally:
        with contextlib.suppress(OSError):
            ffmpeg.stdin.close()


async def _feed_ps(
    camera: object,
    token_factory: object,
    ffmpeg: asyncio.subprocess.Process,
    stream: int,
    verification_code: str,
) -> None:
    """Feed the camera's (decrypted) MPEG-PS into FFmpeg's stdin - PS carries PTS."""
    assert ffmpeg.stdin is not None  # noqa: S101 - PIPE guarantees a writer
    try:
        async for chunk in iter_ps_decrypted(
            camera,  # type: ignore[arg-type]
            token_factory,  # type: ignore[arg-type]
            stream=stream,
            verification_code=verification_code,
        ):
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

    @property
    def is_running(self) -> bool:
        """True while an upstream session is live (something is being served)."""
        return self._task is not None and not self._task.done()

    async def subscribe(self, *, start_if_idle: bool = True) -> AsyncGenerator[bytes]:
        """
        Yield MPEG-TS chunks for one consumer, sharing the single upstream session.

        The first subscriber starts the upstream; the last to leave stops it. With
        ``start_if_idle=False`` a caller only *taps* an already-running session and
        yields nothing when idle - used to grab a thumbnail from a live view without
        opening a cloud session of its own.
        """
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_QUEUE_MAX)
        async with self._lock:
            if not self.is_running and not start_if_idle:
                return  # nothing is streaming; do not start a session just to tap it
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
                for queue in self._subscribers:
                    _offer(queue, chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("broadcast upstream for camera failed")
        finally:
            for queue in self._subscribers:
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
