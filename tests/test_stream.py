"""Tests for the async streaming client's frame reader.

The VTM/VTDU socket handshake and media loop need the real cloud and are verified
live (like the diagnostic scripts). The frame reader is the piece we can exercise
here with a plain ``asyncio.StreamReader``.
"""

from __future__ import annotations

import asyncio

from custom_components.ezviz_stream import stream, ysproto


async def test_frame_reader_reassembles_across_chunks() -> None:
    reader = asyncio.StreamReader()
    frame_reader = stream._FrameReader(reader)
    frame = ysproto.build_frame(ysproto.CH_STREAM, ysproto.MSG_STREAMINFO_RSP, b"hello")
    reader.feed_data(frame[:4])  # split the frame across two reads
    reader.feed_data(frame[4:])
    reader.feed_eof()

    loop = asyncio.get_running_loop()
    result = await frame_reader.next_frame(loop.time() + 1)
    assert result == (ysproto.CH_STREAM, ysproto.MSG_STREAMINFO_RSP, b"hello")


async def test_frame_reader_timeout_returns_none() -> None:
    reader = asyncio.StreamReader()
    frame_reader = stream._FrameReader(reader)
    loop = asyncio.get_running_loop()
    assert await frame_reader.next_frame(loop.time() + 0.05) is None


async def test_frame_reader_eof_sets_closed() -> None:
    reader = asyncio.StreamReader()
    frame_reader = stream._FrameReader(reader)
    reader.feed_eof()
    loop = asyncio.get_running_loop()
    assert await frame_reader.next_frame(loop.time() + 1) is None
    assert frame_reader.closed
