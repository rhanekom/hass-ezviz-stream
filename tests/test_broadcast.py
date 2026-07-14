"""Tests for the on-demand MPEG-TS broadcaster (fan-out + lifecycle).

The FFmpeg remux and the cloud socket path need the real cloud and are verified
live; here we exercise the fan-out/lifecycle logic with in-memory sources and the
camera-not-found guard in :func:`mpegts_source`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.ezviz_stream import broadcast
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


async def test_offer_drops_oldest_when_subscriber_full() -> None:
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
    assert pacer.delay(1_000_000, now=100.0) == 0.0
    # A frame 9000 ticks (0.1 s) later should be released ~0.1 s after the base, even
    # if it arrived early (now advanced only 0.02 s) - i.e. we wait ~0.08 s.
    assert pacer.delay(1_009_000, now=100.02) == pytest.approx(0.08, abs=1e-6)
    # A frame that arrives late (past its target) is released immediately.
    assert pacer.delay(1_018_000, now=100.5) == 0.0


def test_pacer_rebases_on_discontinuity() -> None:
    """A backwards jump or a >2 s gap (reconnect/wrap) rebases to now, not a replay."""
    pacer = broadcast._Pacer()
    assert pacer.delay(5_000_000, now=10.0) == 0.0
    # Fresh RTP base from a reconnect (a big forward jump) -> rebase, play now.
    assert pacer.delay(9_000_000, now=40.0) == 0.0
    # A small step after the rebase is scheduled normally again.
    assert pacer.delay(9_004_500, now=40.0) == pytest.approx(0.05, abs=1e-6)
    # A backwards step (reordering / new base) also rebases.
    assert pacer.delay(1_000, now=41.0) == 0.0


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
