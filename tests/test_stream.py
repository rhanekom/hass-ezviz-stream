"""Tests for the async streaming client.

The real VTM/VTDU socket I/O is still verified live against a real account (like the
diagnostic scripts). Here we exercise the orchestration - the handshake sequencing,
the media-capture loop, JPEG decode, and the reconnecting iterators - with fake
readers/writers and mocked low-level pieces, so the control flow is covered in CI
without the cloud.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.ezviz_stream import stream, ysproto
from custom_components.ezviz_stream.api import EzvizCamera


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
def _camera(**overrides: object) -> EzvizCamera:
    """A streamable camera with sane VTM routing; override any field."""
    defaults: dict[str, object] = {
        "serial": "SN1",
        "name": "cam",
        "category": "IPC",
        "channel": 1,
        "status": 1,
        "streamable": True,
        "vtm_ip": "10.0.0.1",
        "vtm_port": 6001,
        "biz": "",
    }
    defaults.update(overrides)
    return EzvizCamera(**defaults)  # type: ignore[arg-type]


def _writer() -> Mock:
    """A fake asyncio.StreamWriter (sync write/close, async drain/wait_closed)."""
    writer = Mock()
    writer.write = Mock()
    writer.drain = AsyncMock()
    writer.close = Mock()
    writer.wait_closed = AsyncMock()
    return writer


def _rtp_frame(timestamp: int = 6000) -> tuple[bytes, bytes]:
    """A ysproto media frame carrying one single-NAL RTP/HEVC packet.

    Returns (frame_bytes, expected_annexb) where expected_annexb is what the
    depacketiser should yield for that packet.
    """
    # 12-byte RTP header: v2, PT=96, seq=1, ts=<timestamp>, ssrc=0.
    header = bytes([0x80, 0x60, 0x00, 0x01]) + timestamp.to_bytes(4, "big") + bytes(4)
    payload = b"\x02\x00\x00"  # NAL header type 1 (single NAL unit) + 2 bytes
    body = header + payload
    frame = ysproto.build_frame(ysproto.CH_STREAM, 0, body)
    return frame, ysproto.ANNEX_B_START_CODE + payload


def _ps_frame(body: bytes = b"\x00\x00\x01\xbapsdata") -> bytes:
    """A ysproto media frame carrying an MPEG-PS body (pack start code prefix)."""
    return ysproto.build_frame(ysproto.CH_STREAM, 0, body)


def _pb_int(fn: int, n: int) -> bytes:
    """Encode a protobuf varint (wire type 0) field."""
    return bytes([(fn << 3)]) + ysproto._varint(n)


def _streaminfo_rsp(
    *,
    result: int = 0,
    ssn: str | None = None,
    vtmkey: str | None = None,
    redirect: str | None = None,
) -> bytes:
    """Build a StreamInfoRsp frame (1=result, 4=streamssn, 5=vtmkey, 7=redirect)."""
    body = _pb_int(1, result)
    if ssn is not None:
        body += ysproto._pb_string(4, ssn)
    if vtmkey is not None:
        body += ysproto._pb_string(5, vtmkey)
    if redirect is not None:
        body += ysproto._pb_string(7, redirect)
    return ysproto.build_frame(0x00, ysproto.MSG_STREAMINFO_RSP, body)


# --------------------------------------------------------------------------- #
# _FrameReader (existing coverage)
# --------------------------------------------------------------------------- #
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


def test_concurrency_limit_codes_recognised() -> None:
    """The concurrency/resource result codes are recognised; churn (5405) is not."""
    assert {5416, 5503, 5504, 5546} <= stream._CONCURRENCY_LIMIT_CODES
    assert 5405 not in stream._CONCURRENCY_LIMIT_CODES  # 5405 = churn/CAS timeout


# --------------------------------------------------------------------------- #
# _streaminfo_exchange
# --------------------------------------------------------------------------- #
async def test_streaminfo_exchange_returns_decoded_rsp() -> None:
    """Non-matching frames are skipped; the StreamInfoRsp is decoded and returned."""
    reader = asyncio.StreamReader()
    reader.feed_data(ysproto.build_frame(ysproto.CH_STREAM, 0x999, b"skip"))
    reader.feed_data(_streaminfo_rsp(result=0, ssn="S", redirect="ysproto://h/live"))
    reader.feed_eof()
    writer = _writer()

    fields = await stream._streaminfo_exchange(reader, writer, "url", None)

    assert fields[1] == [0]
    assert ysproto.field_str(fields, 4) == "S"
    writer.write.assert_called_once()  # the StreamInfoReq was sent
    writer.drain.assert_awaited_once()


async def test_streaminfo_exchange_timeout_raises() -> None:
    """No StreamInfoRsp before the handshake deadline surfaces a StreamError."""
    reader = asyncio.StreamReader()  # nothing fed, never eof
    writer = _writer()
    with (
        patch.object(stream, "_HANDSHAKE_TIMEOUT", 0.05),
        pytest.raises(stream.StreamError),
    ):
        await stream._streaminfo_exchange(reader, writer, "url", None)


# --------------------------------------------------------------------------- #
# _open_connection
# --------------------------------------------------------------------------- #
async def test_open_connection_success() -> None:
    reader, writer = asyncio.StreamReader(), _writer()
    with patch.object(
        stream.asyncio, "open_connection", AsyncMock(return_value=(reader, writer))
    ):
        assert await stream._open_connection("host", 1234) == (reader, writer)


async def test_open_connection_failure_raises() -> None:
    with (
        patch.object(
            stream.asyncio, "open_connection", AsyncMock(side_effect=OSError("nope"))
        ),
        pytest.raises(stream.StreamError),
    ):
        await stream._open_connection("host", 1234)


# --------------------------------------------------------------------------- #
# open_stream (VTM -> VTDU handshake)
# --------------------------------------------------------------------------- #
async def test_open_stream_no_vtm_routing_raises() -> None:
    camera = _camera(vtm_ip=None)
    with pytest.raises(stream.StreamError):
        await stream.open_stream(camera, "tok", stream=1)


async def test_open_stream_handshake_success() -> None:
    """VTM redirect + a result=0 VTDU response yields the live VTDU triple."""
    r_vtm, w_vtm = asyncio.StreamReader(), _writer()
    r_vtdu, w_vtdu = asyncio.StreamReader(), _writer()
    conns = AsyncMock(side_effect=[(r_vtm, w_vtm), (r_vtdu, w_vtdu)])
    exch = AsyncMock(
        side_effect=[
            {7: [b"ysproto://5.6.7.8:9000/live?stream=1"], 5: [b"VKEY"]},  # VTM
            {1: [0], 4: [b"SSN99"]},  # VTDU
        ]
    )
    with (
        patch.object(stream, "_open_connection", conns),
        patch.object(stream, "_streaminfo_exchange", exch),
    ):
        reader, writer, ssn = await stream.open_stream(_camera(), "tok", stream=1)

    assert (reader, writer, ssn) == (r_vtdu, w_vtdu, "SSN99")
    w_vtm.close.assert_called_once()  # VTM writer closed after its exchange
    # VTDU connection was opened at the redirect's host:port.
    assert conns.await_args_list[1].args == ("5.6.7.8", 9000)


async def test_open_stream_no_redirect_raises() -> None:
    """A VTM response without a redirect (field 7 or embedded URL) fails."""
    conns = AsyncMock(side_effect=[(asyncio.StreamReader(), _writer())])
    camera = _camera()
    with (
        patch.object(stream, "_open_connection", conns),
        patch.object(
            stream, "_streaminfo_exchange", AsyncMock(return_value={5: [b"VKEY"]})
        ),
        pytest.raises(stream.StreamError),
    ):
        await stream.open_stream(camera, "tok", stream=1)


async def test_open_stream_concurrency_limit_raises() -> None:
    """A concurrency-limit VTDU result closes the writer and raises."""
    w_vtdu = _writer()
    conns = AsyncMock(
        side_effect=[
            (asyncio.StreamReader(), _writer()),
            (asyncio.StreamReader(), w_vtdu),
        ]
    )
    exch = AsyncMock(
        side_effect=[
            {7: [b"ysproto://5.6.7.8:9000/live?stream=1"], 5: [b"K"]},
            {1: [5416]},  # a _CONCURRENCY_LIMIT_CODES value
        ]
    )
    camera = _camera()
    with (
        patch.object(stream, "_open_connection", conns),
        patch.object(stream, "_streaminfo_exchange", exch),
        pytest.raises(stream.StreamError),
    ):
        await stream.open_stream(camera, "tok", stream=1)

    w_vtdu.close.assert_called_once()


# --------------------------------------------------------------------------- #
# _capture_session
# --------------------------------------------------------------------------- #
async def test_capture_session_rtp_returns_annexb() -> None:
    reader = asyncio.StreamReader()
    frame, expected = _rtp_frame(6000)
    reader.feed_data(frame)
    reader.feed_eof()
    loop = asyncio.get_running_loop()

    transport, media = await stream._capture_session(
        reader, _writer(), "SSN", verification_code="", deadline=loop.time() + 5
    )

    assert transport == "rtp"
    assert media == expected


async def test_capture_session_mpeg_ps_decrypts_with_code() -> None:
    """Encrypted MPEG-PS is passed through decrypt_ps_video when a code is set."""
    reader = asyncio.StreamReader()
    reader.feed_data(_ps_frame())
    reader.feed_eof()
    loop = asyncio.get_running_loop()

    with patch.object(stream, "decrypt_ps_video", return_value=b"DECRYPTED") as decrypt:
        transport, media = await stream._capture_session(
            reader,
            _writer(),
            None,
            verification_code="123456",
            deadline=loop.time() + 5,
        )

    assert transport == "mpeg-ps"
    assert media == b"DECRYPTED"
    decrypt.assert_called_once()


# --------------------------------------------------------------------------- #
# _decode_jpeg
# --------------------------------------------------------------------------- #
async def test_decode_jpeg_returns_frame_when_large_enough() -> None:
    proc = Mock()
    jpeg = b"\xff" * stream._MIN_JPEG_BYTES
    proc.communicate = AsyncMock(return_value=(jpeg, b""))
    with patch.object(
        stream.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    ) as spawn:
        out = await stream._decode_jpeg("ffmpeg", "rtp", b"media")

    assert out == jpeg
    assert "hevc" in spawn.call_args.args  # rtp maps to "-f hevc"
    proc.communicate.assert_awaited_once_with(b"media")


async def test_decode_jpeg_rejects_tiny_output() -> None:
    """Output below the min-JPEG threshold is treated as a decode artifact (None)."""
    proc = Mock()
    proc.communicate = AsyncMock(return_value=(b"tiny", b""))
    with patch.object(
        stream.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    ):
        assert await stream._decode_jpeg("ffmpeg", None, b"media") is None


# --------------------------------------------------------------------------- #
# grab_jpeg
# --------------------------------------------------------------------------- #
async def test_grab_jpeg_success_first_session() -> None:
    reader, writer = asyncio.StreamReader(), _writer()
    with (
        patch.object(
            stream, "open_stream", AsyncMock(return_value=(reader, writer, "SSN"))
        ),
        patch.object(
            stream, "_capture_session", AsyncMock(return_value=("rtp", b"media"))
        ),
        patch.object(stream, "_decode_jpeg", AsyncMock(return_value=b"JPEG")),
    ):
        out = await stream.grab_jpeg(
            _camera(),
            AsyncMock(return_value="tok"),
            "ffmpeg",
            stream=1,
            verification_code="",
        )

    assert out == b"JPEG"
    writer.close.assert_called_once()


async def test_grab_jpeg_retries_after_handshake_error() -> None:
    """A first-session handshake error backs off and retries the next session."""
    reader, writer = asyncio.StreamReader(), _writer()
    open_mock = AsyncMock(
        side_effect=[stream.StreamError("boom"), (reader, writer, "S")]
    )
    with (
        patch.object(stream, "open_stream", open_mock),
        patch.object(stream, "_capture_session", AsyncMock(return_value=("rtp", b"m"))),
        patch.object(stream, "_decode_jpeg", AsyncMock(return_value=b"JPEG")),
        patch.object(stream.asyncio, "sleep", AsyncMock()),
    ):
        out = await stream.grab_jpeg(
            _camera(),
            AsyncMock(return_value="tok"),
            "ffmpeg",
            stream=1,
            verification_code="",
        )

    assert out == b"JPEG"
    assert open_mock.await_count == 2


async def test_grab_jpeg_none_when_no_media() -> None:
    """Sessions that capture no media are exhausted and None is returned."""
    reader, writer = asyncio.StreamReader(), _writer()
    with (
        patch.object(
            stream, "open_stream", AsyncMock(return_value=(reader, writer, "SSN"))
        ),
        patch.object(stream, "_capture_session", AsyncMock(return_value=("rtp", b""))),
        patch.object(stream, "_decode_jpeg", AsyncMock()) as decode,
        patch.object(stream.asyncio, "sleep", AsyncMock()),
    ):
        out = await stream.grab_jpeg(
            _camera(),
            AsyncMock(return_value="tok"),
            "ffmpeg",
            stream=1,
            verification_code="",
            max_sessions=2,
        )

    assert out is None
    decode.assert_not_called()  # never reached decode with empty media


# --------------------------------------------------------------------------- #
# iter_annexb
# --------------------------------------------------------------------------- #
async def test_iter_annexb_yields_depacketized_chunk() -> None:
    reader = asyncio.StreamReader()
    frame, expected = _rtp_frame(6000)
    reader.feed_data(frame)  # left open (no eof) so the generator parks at the yield
    writer = _writer()

    with patch.object(
        stream, "open_stream", AsyncMock(return_value=(reader, writer, "SSN"))
    ):
        gen = stream.iter_annexb(_camera(), AsyncMock(return_value="tok"), stream=1)
        rtp_ts, chunk = await gen.__anext__()
        await gen.aclose()

    assert rtp_ts == 6000
    assert chunk == expected
    writer.close.assert_called_once()  # aclose ran the finally


async def test_iter_annexb_backoff_on_handshake_error() -> None:
    """A handshake error logs, backs off, and loops (we break out via the sleep)."""

    class _StopError(Exception):
        """Sentinel raised from the patched sleep to break the reconnect loop."""

    with (
        patch.object(
            stream, "open_stream", AsyncMock(side_effect=stream.StreamError("x"))
        ),
        patch.object(stream.asyncio, "sleep", AsyncMock(side_effect=_StopError)),
    ):
        gen = stream.iter_annexb(_camera(), AsyncMock(return_value="tok"), stream=1)
        with pytest.raises(_StopError):
            await gen.__anext__()


# --------------------------------------------------------------------------- #
# iter_ps_decrypted
# --------------------------------------------------------------------------- #
async def test_iter_ps_decrypted_passthrough_without_code() -> None:
    """With no verification code the PS bytes pass through unchanged."""
    reader = asyncio.StreamReader()
    ps_body = b"\x00\x00\x01\xbapsdata"
    reader.feed_data(_ps_frame(ps_body))
    writer = _writer()

    with patch.object(
        stream, "open_stream", AsyncMock(return_value=(reader, writer, "SSN"))
    ):
        gen = stream.iter_ps_decrypted(
            _camera(), AsyncMock(return_value="tok"), stream=1, verification_code=""
        )
        chunk = await gen.__anext__()
        await gen.aclose()

    assert chunk == ps_body


async def test_iter_ps_decrypted_decrypts_with_code() -> None:
    """With a verification code each PS body is fed through the streaming decryptor."""
    reader = asyncio.StreamReader()
    reader.feed_data(_ps_frame(b"\x00\x00\x01\xbaX"))
    writer = _writer()
    fake_decryptor = Mock()
    fake_decryptor.feed = Mock(return_value=b"DEC")

    with (
        patch.object(
            stream, "open_stream", AsyncMock(return_value=(reader, writer, "SSN"))
        ),
        patch.object(stream, "StreamingPsDecryptor", return_value=fake_decryptor),
    ):
        gen = stream.iter_ps_decrypted(
            _camera(),
            AsyncMock(return_value="tok"),
            stream=1,
            verification_code="123456",
        )
        chunk = await gen.__anext__()
        await gen.aclose()

    assert chunk == b"DEC"
    fake_decryptor.feed.assert_called_once()


# --------------------------------------------------------------------------- #
# stream_annexb (CLI producer wrapper)
# --------------------------------------------------------------------------- #
async def test_stream_annexb_writes_iter_annexb_chunks() -> None:
    """stream_annexb (the CLI producer wrapper) writes+flushes each yielded chunk."""

    async def fake_iter(*_args: object, **_kwargs: object):  # noqa: ANN202
        yield 0, b"aa"  # (rtp_timestamp, annexb_chunk)
        yield 6000, b"bb"

    written: list[bytes] = []
    out = Mock()
    out.write = written.append
    out.flush = Mock()

    with patch.object(stream, "iter_annexb", fake_iter):
        await stream.stream_annexb(Mock(), Mock(), out, stream=1)

    assert b"".join(written) == b"aabb"
    assert out.flush.call_count == 2  # flushed after each chunk (live, low-latency)
