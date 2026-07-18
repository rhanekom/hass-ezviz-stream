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


async def test_spawn_ffmpeg_transcodes_video_to_h264() -> None:
    """With transcode on, video is re-encoded to H.264 (audio still copied)."""
    proc = MagicMock()
    with patch.object(
        broadcast.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    ) as spawn:
        await broadcast._spawn_ffmpeg("ffmpeg", "hevc", wallclock=True, transcode=True)

    args = spawn.await_args.args
    assert "libx264" in args
    assert "-c" not in args  # not a plain stream copy (-c:v / -c:a are distinct)
    assert args[-5:] == ("-c:a", "copy", "-f", "mpegts", "pipe:1")


async def test_mpegts_source_forwards_transcode_flag() -> None:
    """The transcode flag reaches _spawn_ffmpeg for the chosen transport path."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(
        return_value=[EzvizCamera("SN1", "Cam", "BatteryCamera", 1, 1, streamable=True)]
    )
    ffmpeg = MagicMock()
    ffmpeg.stdout.read = AsyncMock(return_value=b"")
    ffmpeg.returncode = 0

    with (
        patch.object(
            broadcast, "_spawn_ffmpeg", AsyncMock(return_value=ffmpeg)
        ) as spawn,
        patch.object(broadcast, "_feed_rtp", AsyncMock()),
    ):
        async for _ in broadcast.mpegts_source(
            api, "SN1", "ffmpeg", stream=1, verification_code="", transcode=True
        ):
            # Drain the generator; the assertions below check spawn args, not chunks.
            pass

    assert spawn.await_args.kwargs["transcode"] is True


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

    def _wait() -> None:
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


async def test_terminate_reaps_process_after_kill() -> None:
    """After escalating to kill, the process is awaited so its transport is reaped."""
    proc = MagicMock()
    proc.returncode = None  # AsyncMock wait never sets it, so terminate escalates
    proc.wait = AsyncMock()

    await broadcast._terminate(proc)

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.wait.await_count >= 2  # graceful wait, then reap after the kill


async def test_subscribe_returns_when_upstream_ends() -> None:
    """When the upstream source ends, the subscriber sees the end sentinel and stops."""

    async def source() -> AsyncIterator[bytes]:
        yield b"a"

    caster = CameraBroadcast(source)
    chunks = [chunk async for chunk in caster.subscribe()]

    assert chunks == [b"a"]
    assert caster._task is None  # cleaned up after the last subscriber left


async def test_subscribe_start_if_idle_false_taps_only_when_running() -> None:
    """start_if_idle=False yields nothing (and never starts the upstream) when idle."""
    started = False

    async def source() -> AsyncIterator[bytes]:
        nonlocal started
        started = True
        yield b"x"

    caster = CameraBroadcast(source)
    chunks = [chunk async for chunk in caster.subscribe(start_if_idle=False)]

    assert chunks == []  # nothing was streaming, so nothing to tap
    assert started is False  # the upstream session was never started
    assert caster._task is None


async def test_offline_cooldown_skips_restart_after_empty_session() -> None:
    """A session that streams no media sets a cooldown; the next pull won't restart."""
    calls = 0
    media: list[bytes] = []  # empty -> the session produces nothing

    async def source() -> AsyncIterator[bytes]:
        nonlocal calls
        calls += 1
        for chunk in media:
            yield chunk

    caster = CameraBroadcast(source)
    with patch.object(broadcast, "_OFFLINE_COOLDOWN", 1000.0):
        first = await _collect(caster.subscribe(), 5)
        second = await _collect(caster.subscribe(), 5)

    assert first == []
    assert second == []
    assert calls == 1  # the cooldown blocked the second session from starting
    assert caster._offline_until > 0


async def test_productive_session_clears_cooldown() -> None:
    """A session that streams media leaves no cooldown; the next pull restarts."""
    calls = 0

    async def source() -> AsyncIterator[bytes]:
        nonlocal calls
        calls += 1
        yield b"frame"

    caster = CameraBroadcast(source)
    with patch.object(broadcast, "_OFFLINE_COOLDOWN", 1000.0):
        first = await _collect(caster.subscribe(), 5)
        second = await _collect(caster.subscribe(), 5)

    assert first == [b"frame"]
    assert second == [b"frame"]
    assert calls == 2  # media flowed, so no cooldown - both pulls started a session
    assert not caster._offline_until  # 0.0 sentinel (no float equality check)


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


# --- per-clip decrypt + audio decision (_prepare_replay / replay_mp4_source) -- #
async def _drain(agen: AsyncIterator[bytes]) -> bytes:
    """Concatenate every chunk an async byte source yields."""
    return b"".join([chunk async for chunk in agen])


async def _src(*chunks: bytes) -> AsyncIterator[bytes]:
    """A trivial async byte source."""
    for chunk in chunks:
        yield chunk


async def test_prepare_replay_passthrough_without_code() -> None:
    """With no verification code there is nothing to decrypt, so bytes pass through."""
    with patch.object(
        broadcast, "_probe_audio_encodable", AsyncMock(return_value=True)
    ):
        audio_ok, ps = await broadcast._prepare_replay("ffmpeg", _src(b"ab", b"cd"), "")
    assert audio_ok is True
    assert await _drain(ps) == b"abcd"


async def test_prepare_replay_serves_plaintext_unchanged() -> None:
    """A clip that already decodes raw is served as-is (decrypting would corrupt it)."""
    with (
        patch.object(broadcast, "_probe_frame_count", AsyncMock(return_value=10)),
        patch.object(broadcast, "_probe_audio_encodable", AsyncMock(return_value=True)),
    ):
        _audio_ok, ps = await broadcast._prepare_replay(
            "ffmpeg", _src(b"plain"), "CODE"
        )
    assert await _drain(ps) == b"plain"


async def test_prepare_replay_decrypts_when_only_decrypted_decodes() -> None:
    """Raw fails to decode but decrypted decodes, so the stream is decrypted."""
    fake = MagicMock()
    fake.feed.side_effect = lambda b: b"|" + b
    fake.flush.return_value = b"END"
    with (
        patch.object(broadcast, "_probe_frame_count", AsyncMock(side_effect=[0, 10])),
        patch.object(broadcast, "_probe_audio_encodable", AsyncMock(return_value=True)),
        patch.object(broadcast, "decrypt_ps_video", lambda b, _k: b),
        patch.object(broadcast, "decrypt_ps_audio", lambda b, _k: b),
        patch.object(broadcast, "StreamingPsDecryptor", return_value=fake),
    ):
        _audio_ok, ps = await broadcast._prepare_replay("ffmpeg", _src(b"enc"), "CODE")
        # Drain inside the patch context: _ps_source resolves StreamingPsDecryptor
        # lazily, so it must still be patched when the generator runs.
        assert await _drain(ps) == b"|encEND"


async def test_prepare_replay_unknown_key_serves_raw_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When neither raw nor decrypted decodes (old key), serve raw and warn."""
    with (
        patch.object(broadcast, "_probe_frame_count", AsyncMock(side_effect=[0, 0])),
        patch.object(
            broadcast, "_probe_audio_encodable", AsyncMock(return_value=False)
        ),
        patch.object(broadcast, "decrypt_ps_video", lambda b, _k: b),
        patch.object(broadcast, "decrypt_ps_audio", lambda b, _k: b),
        caplog.at_level("WARNING"),
    ):
        _audio_ok, ps = await broadcast._prepare_replay("ffmpeg", _src(b"raw"), "CODE")
    assert await _drain(ps) == b"raw"
    assert "different encryption code" in caplog.text


async def test_prepare_replay_drops_undecodable_audio() -> None:
    """A clip whose audio can't be AAC-encoded reports audio_ok False (video only)."""
    with (
        patch.object(broadcast, "_probe_frame_count", AsyncMock(return_value=10)),
        patch.object(
            broadcast, "_probe_audio_encodable", AsyncMock(return_value=False)
        ),
    ):
        audio_ok, ps = await broadcast._prepare_replay("ffmpeg", _src(b"plain"), "CODE")
    assert audio_ok is False
    assert await _drain(ps) == b"plain"  # video bytes still served


async def test_prepare_replay_streams_chunks_past_probe_boundary() -> None:
    """Chunks buffered beyond the probe window still reach the output (plaintext)."""
    with (
        patch.object(broadcast, "_PROBE_BYTES", 3),
        patch.object(broadcast, "_probe_frame_count", AsyncMock(return_value=10)),
        patch.object(broadcast, "_probe_audio_encodable", AsyncMock(return_value=True)),
    ):
        _audio_ok, ps = await broadcast._prepare_replay(
            "ffmpeg", _src(b"abc", b"def"), "CODE"
        )
    assert await _drain(ps) == b"abcdef"


async def test_replay_mp4_source_threads_audio_flag() -> None:
    """replay_mp4_source passes the audio decision through to mp4_replay_source."""
    with (
        patch.object(
            broadcast,
            "_prepare_replay",
            AsyncMock(return_value=(False, _src(b"raw"))),
        ),
        patch.object(broadcast, "mp4_replay_source", return_value=_src(b"OUT")) as mp4,
    ):
        out = await _drain(broadcast.replay_mp4_source("ffmpeg", _src(b"x"), "CODE"))
    assert out == b"OUT"
    assert mp4.call_args.kwargs["audio"] is False  # undecodable audio -> -an


async def test_probe_audio_encodable_true_on_zero_rc() -> None:
    """Audio that the AAC encoder accepts (rc 0) is reported encodable."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 0
    with patch.object(
        broadcast.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    ):
        assert await broadcast._probe_audio_encodable("ffmpeg", b"data") is True


async def test_probe_audio_encodable_false_on_nonzero_rc() -> None:
    """Undecodable / absent audio (nonzero rc) is reported not encodable."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 69  # ffmpeg's AAC-encode failure on the failing clip
    with patch.object(
        broadcast.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    ):
        assert await broadcast._probe_audio_encodable("ffmpeg", b"data") is False


async def test_probe_frame_count_parses_progress() -> None:
    """The probe returns the last frame= count FFmpeg reports."""
    proc = MagicMock()
    proc.communicate = AsyncMock(
        return_value=(b"frame=3\nprogress=continue\nframe=  17\nprogress=end\n", b"")
    )
    with patch.object(
        broadcast.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    ):
        assert await broadcast._probe_frame_count("ffmpeg", b"data") == 17


async def test_probe_frame_count_zero_when_no_frames() -> None:
    """No frame= output (undecodable input) yields a zero count."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", b"Invalid data"))
    with patch.object(
        broadcast.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    ):
        assert await broadcast._probe_frame_count("ffmpeg", b"data") == 0


async def test_mp4_replay_source_caps_keyframe_interval() -> None:
    """mp4_replay_source must set -g so fragments flush (short/static clips play)."""
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()
    proc.stdout.read = AsyncMock(return_value=b"")  # end the read loop immediately
    proc.returncode = 0
    with patch.object(
        broadcast.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    ) as spawn:
        async for _ in broadcast.mp4_replay_source("ffmpeg", _src(b""), audio=True):
            # Drain the generator; the assertions below check spawn args, not chunks.
            pass
    args = spawn.call_args.args
    assert "-g" in args  # keyframe-interval cap present
    assert args[args.index("-g") + 1] == "30"


async def test_probe_frame_count_reaps_process_on_cancel() -> None:
    """A probe cancelled mid-run still reaps its ffmpeg (no orphaned transport)."""
    proc = MagicMock()
    started = asyncio.Event()

    async def _block(_: bytes) -> tuple[bytes, bytes]:
        started.set()
        await asyncio.Event().wait()  # never completes; force a cancel
        return b"", b""

    proc.communicate = _block
    proc.returncode = None
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    def _reap() -> None:
        proc.returncode = 0

    proc.wait = AsyncMock(side_effect=_reap)

    with patch.object(
        broadcast.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    ):
        task = asyncio.create_task(broadcast._probe_frame_count("ffmpeg", b"x"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    proc.terminate.assert_called_once()  # cleaned up despite the cancellation
