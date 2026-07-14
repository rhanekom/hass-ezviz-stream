"""Tests for the on-demand MPEG-TS broadcaster (fan-out + lifecycle).

The FFmpeg remux and the cloud socket path need the real cloud and are verified
live; here we exercise the fan-out/lifecycle logic with in-memory sources and the
camera-not-found guard in :func:`mpegts_source`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ezviz_stream import broadcast
from custom_components.ezviz_stream.api import EzvizCamera
from custom_components.ezviz_stream.broadcast import CameraBroadcast

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def _collect(agen: AsyncIterator[bytes], n: int) -> list[bytes]:
    """Take up to ``n`` chunks from ``agen``, then close it (unsubscribe)."""
    out: list[bytes] = []
    async for chunk in agen:
        out.append(chunk)
        if len(out) >= n:
            break
    return out


async def test_fanout_delivers_all_chunks_to_all_subscribers() -> None:
    """Two subscribers each receive every chunk from a single shared upstream."""
    gate = asyncio.Event()
    starts = 0

    async def source() -> AsyncIterator[bytes]:
        nonlocal starts
        starts += 1
        await gate.wait()  # hold until both subscribers are attached
        for chunk in (b"x", b"y", b"z"):
            yield chunk

    caster = CameraBroadcast(source)
    t1 = asyncio.create_task(_collect(caster.subscribe(), 3))
    t2 = asyncio.create_task(_collect(caster.subscribe(), 3))
    for _ in range(5):  # let both register and block on their queues
        await asyncio.sleep(0)
    gate.set()
    r1, r2 = await asyncio.gather(t1, t2)

    assert r1 == [b"x", b"y", b"z"]
    assert r2 == [b"x", b"y", b"z"]
    assert starts == 1  # one upstream session shared by both subscribers


async def test_upstream_stops_when_last_subscriber_leaves() -> None:
    """The shared upstream is cancelled once no subscribers remain."""
    cancelled = asyncio.Event()

    async def source() -> AsyncIterator[bytes]:
        try:
            while True:
                yield b"data"
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    caster = CameraBroadcast(source)
    agen = caster.subscribe()
    assert await agen.__anext__() == b"data"  # starts the upstream

    await agen.aclose()  # last (only) subscriber leaves
    assert caster._task is None
    for _ in range(5):
        await asyncio.sleep(0.01)  # let the cancellation propagate into the source
    assert cancelled.is_set()


def test_offer_drops_oldest_when_subscriber_full() -> None:
    """A slow subscriber drops its oldest chunk rather than blocking the broadcast."""
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2)
    broadcast._offer(queue, b"1")
    broadcast._offer(queue, b"2")
    broadcast._offer(queue, b"3")  # full -> drop oldest (b"1")

    assert queue.qsize() == 2
    assert queue.get_nowait() == b"2"
    assert queue.get_nowait() == b"3"


def test_pacer_schedules_on_the_rtp_clock() -> None:
    """Frames are released on the camera's 90 kHz cadence, not their arrival time."""
    pacer = broadcast._Pacer()
    # First frame rebases to "now" and plays immediately, whatever the wall time.
    assert pacer.delay(1_000_000, now=100.0) == pytest.approx(0.0, abs=1e-9)
    # A frame 9000 ticks (0.1 s) later should be released ~0.1 s after the base, even
    # if it arrived early (now advanced only 0.02 s) - i.e. we wait ~0.08 s.
    assert pacer.delay(1_009_000, now=100.02) == pytest.approx(0.08, abs=1e-6)
    # A frame that arrives late (past its target) is released immediately.
    assert pacer.delay(1_018_000, now=100.5) == pytest.approx(0.0, abs=1e-9)


def test_pacer_rebases_on_discontinuity() -> None:
    """A backwards jump or a >2 s gap (reconnect/wrap) rebases to now, not a replay."""
    pacer = broadcast._Pacer()
    assert pacer.delay(5_000_000, now=10.0) == pytest.approx(0.0, abs=1e-9)
    # Fresh RTP base from a reconnect (a big forward jump) -> rebase, play now.
    assert pacer.delay(9_000_000, now=40.0) == pytest.approx(0.0, abs=1e-9)
    # A small step after the rebase is scheduled normally again.
    assert pacer.delay(9_004_500, now=40.0) == pytest.approx(0.05, abs=1e-6)
    # A backwards step (reordering / new base) also rebases.
    assert pacer.delay(1_000, now=41.0) == pytest.approx(0.0, abs=1e-9)


async def test_mpegts_source_missing_camera_yields_nothing() -> None:
    """No FFmpeg is spawned and no chunks flow when the serial is not on the account."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(return_value=[])

    with patch(
        "custom_components.ezviz_stream.broadcast.asyncio.create_subprocess_exec",
        new=AsyncMock(),
    ) as spawn:
        chunks = [
            chunk
            async for chunk in broadcast.mpegts_source(
                api, "NOPE", "ffmpeg", stream=1, verification_code=""
            )
        ]

    assert chunks == []
    spawn.assert_not_called()


@pytest.mark.parametrize(
    ("category", "expected_fmt", "expected_wallclock"),
    [("BatteryCamera", "hevc", True), ("IPC", "mpeg", False)],
)
async def test_mpegts_source_selects_path_by_transport(
    category: str, expected_fmt: str, *, expected_wallclock: bool
) -> None:
    """Battery cams remux raw HEVC (wall-clock); other cams remux MPEG-PS."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(
        return_value=[EzvizCamera("SN1", "Cam", category, 1, 1, streamable=True)]
    )
    ffmpeg = MagicMock()
    ffmpeg.stdout.read = AsyncMock(return_value=b"")  # end the read loop immediately
    ffmpeg.returncode = 0  # so _terminate is a no-op

    with (
        patch(
            "custom_components.ezviz_stream.broadcast._spawn_ffmpeg",
            AsyncMock(return_value=ffmpeg),
        ) as spawn,
        patch("custom_components.ezviz_stream.broadcast._feed_rtp", AsyncMock()),
        patch("custom_components.ezviz_stream.broadcast._feed_ps", AsyncMock()),
    ):
        chunks = [
            chunk
            async for chunk in broadcast.mpegts_source(
                api, "SN1", "ffmpeg", stream=1, verification_code=""
            )
        ]

    assert chunks == []
    assert spawn.await_args.args[1] == expected_fmt
    assert spawn.await_args.kwargs["wallclock"] is expected_wallclock


def _ffmpeg_proc() -> MagicMock:
    """A fake FFmpeg process: sync write/close stdin, async drain, empty stdout."""
    proc = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()
    proc.stdout.read = AsyncMock(return_value=b"")
    return proc


@pytest.mark.parametrize("wallclock", [True, False])
async def test_spawn_ffmpeg_builds_args(*, wallclock: bool) -> None:
    """The remux command is a stream-copy to MPEG-TS; wall-clock is opt-in."""
    proc = MagicMock()
    with patch.object(
        broadcast.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    ) as spawn:
        result = await broadcast._spawn_ffmpeg("ffmpeg", "hevc", wallclock=wallclock)

    assert result is proc
    args = spawn.await_args.args
    assert args[0] == "ffmpeg"
    assert ("-use_wallclock_as_timestamps" in args) is wallclock
    assert args[-9:] == (
        "-f",
        "hevc",
        "-i",
        "pipe:0",
        "-c",
        "copy",
        "-f",
        "mpegts",
        "pipe:1",
    )


async def test_mpegts_source_yields_ffmpeg_output() -> None:
    """FFmpeg's stdout is streamed out until EOF, then the feeder + process stop."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(
        return_value=[EzvizCamera("SN1", "Cam", "BatteryCamera", 1, 1, streamable=True)]
    )
    ffmpeg = MagicMock()
    ffmpeg.stdout.read = AsyncMock(side_effect=[b"tsdata", b""])
    ffmpeg.returncode = 0  # so _terminate is a no-op

    with (
        patch.object(broadcast, "_spawn_ffmpeg", AsyncMock(return_value=ffmpeg)),
        patch.object(broadcast, "_feed_rtp", AsyncMock()),
    ):
        chunks = [
            chunk
            async for chunk in broadcast.mpegts_source(
                api, "SN1", "ffmpeg", stream=1, verification_code=""
            )
        ]

    assert chunks == [b"tsdata"]


async def test_feed_rtp_writes_paced_chunks() -> None:
    """Annex-B chunks are paced (delay respected) and written to FFmpeg's stdin."""
    proc = _ffmpeg_proc()

    async def fake_iter(*_args: object, **_kwargs: object):  # noqa: ANN202
        yield 0, b"aa"  # first frame rebases the pacer (no wait)
        yield 9000, b"bb"  # +0.1 s on the RTP clock -> a pace sleep

    with (
        patch.object(broadcast, "iter_annexb", fake_iter),
        patch.object(broadcast.asyncio, "sleep", AsyncMock()) as sleep,
    ):
        await broadcast._feed_rtp(MagicMock(), MagicMock(), proc, 1)

    assert [call.args[0] for call in proc.stdin.write.call_args_list] == [b"aa", b"bb"]
    sleep.assert_awaited()  # the second frame was paced
    proc.stdin.close.assert_called_once()  # stdin closed on exit


async def test_feed_rtp_swallows_broken_pipe() -> None:
    """If FFmpeg's stdin breaks, the feeder exits quietly and closes stdin."""
    proc = _ffmpeg_proc()
    proc.stdin.drain = AsyncMock(side_effect=BrokenPipeError)

    async def fake_iter(*_args: object, **_kwargs: object):  # noqa: ANN202
        yield 0, b"aa"

    with patch.object(broadcast, "iter_annexb", fake_iter):
        await broadcast._feed_rtp(MagicMock(), MagicMock(), proc, 1)  # no raise

    proc.stdin.close.assert_called_once()


async def test_feed_ps_writes_chunks() -> None:
    """Decrypted MPEG-PS chunks are written straight through (PS carries PTS)."""
    proc = _ffmpeg_proc()

    async def fake_iter(*_args: object, **_kwargs: object):  # noqa: ANN202
        yield b"p1"
        yield b"p2"

    with patch.object(broadcast, "iter_ps_decrypted", fake_iter):
        await broadcast._feed_ps(MagicMock(), MagicMock(), proc, 1, "code")

    assert [call.args[0] for call in proc.stdin.write.call_args_list] == [b"p1", b"p2"]
    proc.stdin.close.assert_called_once()


async def test_feed_ps_swallows_connection_reset() -> None:
    """A reset FFmpeg pipe ends the PS feeder quietly."""
    proc = _ffmpeg_proc()
    proc.stdin.drain = AsyncMock(side_effect=ConnectionResetError)

    async def fake_iter(*_args: object, **_kwargs: object):  # noqa: ANN202
        yield b"p1"

    with patch.object(broadcast, "iter_ps_decrypted", fake_iter):
        await broadcast._feed_ps(MagicMock(), MagicMock(), proc, 1, "code")  # no raise

    proc.stdin.close.assert_called_once()


async def test_terminate_noop_when_already_exited() -> None:
    proc = MagicMock()
    proc.returncode = 0
    await broadcast._terminate(proc)
    proc.terminate.assert_not_called()


async def test_terminate_graceful_no_kill() -> None:
    """A process that exits after SIGTERM is not killed."""
    proc = MagicMock()
    proc.returncode = None

    async def _wait() -> None:
        proc.returncode = 0  # exits promptly after terminate()

    proc.wait = AsyncMock(side_effect=_wait)

    await broadcast._terminate(proc)

    proc.terminate.assert_called_once()
    proc.kill.assert_not_called()


async def test_terminate_escalates_to_kill_on_timeout() -> None:
    """A process that ignores SIGTERM past the timeout is killed."""
    proc = MagicMock()
    proc.returncode = None  # never exits
    proc.wait = MagicMock()  # sync: wait_for is mocked, so no coroutine is created

    with patch.object(
        broadcast.asyncio, "wait_for", AsyncMock(side_effect=TimeoutError)
    ):
        await broadcast._terminate(proc)

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


async def test_subscribe_returns_when_upstream_ends() -> None:
    """When the upstream source ends, the subscriber sees the end sentinel and stops."""

    async def source() -> AsyncIterator[bytes]:
        yield b"a"

    caster = CameraBroadcast(source)
    chunks = [chunk async for chunk in caster.subscribe()]

    assert chunks == [b"a"]
    assert caster._task is None  # cleaned up after the last subscriber left


async def test_async_stop_cancels_task_and_releases_subscribers() -> None:
    """async_stop tears down the upstream and pushes the end sentinel to subscribers."""

    async def source() -> AsyncIterator[bytes]:
        yield b"x"
        await asyncio.Event().wait()  # then block forever

    caster = CameraBroadcast(source)
    agen = caster.subscribe()
    assert await agen.__anext__() == b"x"  # one subscriber, upstream running

    await caster.async_stop()

    assert caster._task is None
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()  # the None sentinel ends the subscriber


async def test_run_logs_and_ends_on_upstream_error() -> None:
    """An upstream exception is logged and turned into a clean end-of-stream."""

    async def source() -> AsyncIterator[bytes]:
        for _ in ():  # make this an async generator without an executable yield
            yield b""
        raise RuntimeError("upstream boom")

    caster = CameraBroadcast(source)
    chunks = [chunk async for chunk in caster.subscribe()]

    assert chunks == []  # error -> None sentinel -> no chunks delivered
