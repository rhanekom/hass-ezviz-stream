"""
Async VTM/VTDU streaming client: handshake, media loop, and frame grab.

Drives the ``ysproto`` handshake over asyncio sockets (non-blocking, so it runs in
HA's event loop), reads channel-0x01 media, de-packetises RTP→HEVC or decrypts
encrypted MPEG-PS, and can grab a single decoded JPEG across the ~27 s VTDU drop.
The proven logic is ported from ``scripts/ezviz_cloud.py`` /
``ezviz_stream_probe.py``; the pure protocol/codec bits live in :mod:`ysproto` and
:mod:`decrypt`.

Live verification: the socket path can't be unit-tested in CI (it needs the real
cloud), so it is exercised against a real account - as the diagnostic scripts were.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

from .decrypt import decrypt_ps_video
from .ysproto import (
    CH_STREAM,
    MSG_STREAMINFO_RSP,
    HevcDepacketizer,
    build_keepalive,
    build_stream_url,
    build_streaminfo_request,
    decode_protobuf,
    detect_transport,
    field_str,
    read_frame,
    scan_ysproto,
    set_stream_param,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from typing import IO

    from .api import EzvizCamera

_LOGGER = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 10
_HANDSHAKE_TIMEOUT = 10
_KEEPALIVE_INTERVAL = 5.0
_READ_SLICE = 5.0
_RETRY_BACKOFF = 2.0  # brief pause between sessions (wakes a sleeping cam; eases CAS)
_RECV = 65536
_FFMPEG_FMT = {"rtp": "hevc", "mpeg-ps": "mpeg", "mpeg-ts": "mpegts"}
_MIN_JPEG_BYTES = 5000  # smaller than a real frame => a decode artifact


class StreamError(Exception):
    """A streaming handshake or media error."""


class _FrameReader:
    """Read ysproto frames from an asyncio stream, buffering across reads."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader
        self._buf = bytearray()
        self.closed = False

    async def next_frame(self, deadline: float) -> tuple[int, int, bytes] | None:
        """Return the next (channel, msgcode, body) or None on timeout/close."""
        loop = asyncio.get_running_loop()
        while True:
            frame, consumed = read_frame(bytes(self._buf))
            if consumed:
                del self._buf[:consumed]
            if frame is not None:
                return frame
            timeout = deadline - loop.time()
            if timeout <= 0:
                return None
            try:
                chunk = await asyncio.wait_for(self._reader.read(_RECV), timeout)
            except TimeoutError:
                return None
            if not chunk:
                self.closed = True
                return None
            self._buf += chunk


async def _streaminfo_exchange(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    stream_url: str,
    vtm_stream_key: str | None,
) -> dict[int, list]:
    """Send a StreamInfoReq and return the decoded StreamInfoRsp fields."""
    writer.write(build_streaminfo_request(stream_url, vtm_stream_key))
    await writer.drain()
    frames = _FrameReader(reader)
    deadline = asyncio.get_running_loop().time() + _HANDSHAKE_TIMEOUT
    while True:
        frame = await frames.next_frame(deadline)
        if frame is None:
            msg = "no StreamInfoRsp before timeout"
            raise StreamError(msg)
        _ch, msgcode, body = frame
        if msgcode == MSG_STREAMINFO_RSP:
            return decode_protobuf(body)


async def _open_connection(
    host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    try:
        return await asyncio.wait_for(
            asyncio.open_connection(host, port), _CONNECT_TIMEOUT
        )
    except (OSError, TimeoutError) as err:
        msg = f"cannot connect to {host}:{port}: {err}"
        raise StreamError(msg) from err


async def open_stream(
    camera: EzvizCamera, token: str, *, stream: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str | None]:
    """Do the VTM/VTDU handshake; return the live VTDU (reader, writer, streamssn)."""
    if not camera.vtm_ip or not camera.vtm_port:
        msg = f"camera {camera.serial} has no VTM routing"
        raise StreamError(msg)

    # 1) VTM: ask where to stream.
    vtm_reader, vtm_writer = await _open_connection(camera.vtm_ip, camera.vtm_port)
    vtm_url = build_stream_url(
        camera.vtm_ip,
        camera.vtm_port,
        camera.serial,
        camera.channel,
        token,
        stream=stream,
        biz=camera.biz,
        timestamp_ms=int(time.time() * 1000),
    )
    try:
        fields = await _streaminfo_exchange(vtm_reader, vtm_writer, vtm_url, None)
    finally:
        vtm_writer.close()

    redirect = field_str(fields, 7)
    if not redirect:
        raw = b"".join(
            v for vals in fields.values() for v in vals if isinstance(v, bytes)
        )
        redirect = scan_ysproto(raw)
    if not redirect:
        msg = "VTM gave no VTDU redirect"
        raise StreamError(msg)
    vtm_key = field_str(fields, 5)
    host_part = redirect.split("//", 1)[1].split("/", 1)[0]
    vtdu_ip, _, vtdu_port = host_part.partition(":")
    redirect = set_stream_param(redirect, stream)  # keep the requested track

    # 2) VTDU: reuse the redirect URL, now carrying the vtmstreamkey.
    vtdu_reader, vtdu_writer = await _open_connection(vtdu_ip, int(vtdu_port))
    rsp = await _streaminfo_exchange(vtdu_reader, vtdu_writer, redirect, vtm_key)
    result = (rsp.get(1) or [0])[0]
    if result not in (0, None):
        vtdu_writer.close()
        msg = f"VTDU StreamInfoRsp result={result}"
        raise StreamError(msg)
    return vtdu_reader, vtdu_writer, field_str(rsp, 4)


async def _capture_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    stream_ssn: str | None,
    *,
    verification_code: str,
    deadline: float,
) -> tuple[str | None, bytes]:
    """Read one VTDU session's media, returning (transport, decodable bytes)."""
    loop = asyncio.get_running_loop()
    frames = _FrameReader(reader)
    depacketizer = HevcDepacketizer()
    keepalive = stream_ssn.encode() if stream_ssn else None
    last_ka = loop.time()
    transport: str | None = None
    out = bytearray()

    while loop.time() < deadline:
        if keepalive and loop.time() - last_ka >= _KEEPALIVE_INTERVAL:
            writer.write(build_keepalive(stream_ssn))
            await writer.drain()
            last_ka = loop.time()
        frame = await frames.next_frame(min(deadline, loop.time() + _READ_SLICE))
        if frame is None:
            if frames.closed:
                break
            continue
        channel, _msg, body = frame
        if channel != CH_STREAM or not body:
            continue
        if transport is None:
            transport = detect_transport(body)
        if transport == "rtp":
            out += depacketizer.push(body)
        else:
            out += body

    if transport in ("mpeg-ps", "mpeg-ts") and verification_code:
        out = bytearray(decrypt_ps_video(bytes(out), verification_code))
    return transport, bytes(out)


async def _decode_jpeg(
    ffmpeg_bin: str, transport: str | None, media: bytes
) -> bytes | None:
    """Decode a single JPEG from captured media via FFmpeg (stdin to stdout)."""
    fmt = _FFMPEG_FMT.get(transport or "")
    args = [ffmpeg_bin, "-hide_banner", "-v", "error", "-y"]
    if fmt:
        args += ["-f", fmt]
    args += ["-i", "pipe:0", "-frames:v", "1", "-f", "image2", "pipe:1"]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    jpeg, _ = await proc.communicate(media)
    return jpeg if len(jpeg) >= _MIN_JPEG_BYTES else None


async def grab_jpeg(  # noqa: PLR0913 - a session needs camera, token, ffmpeg + tuning
    camera: EzvizCamera,
    token_factory: Callable[[], Awaitable[str]],
    ffmpeg_bin: str,
    *,
    stream: int,
    verification_code: str,
    duration: float = 60.0,
    max_sessions: int = 6,
) -> bytes | None:
    """
    Grab a single decoded JPEG, reconnecting across the ~27 s VTDU drop.

    ``token_factory`` returns a fresh VTDU token per session (each reconnect needs a
    new one). Returns the JPEG bytes, or None if none decoded within the budget.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration
    session = 0
    while loop.time() < deadline and session < max_sessions:
        if session:  # brief backoff before a retry (a battery cam may still be waking)
            await asyncio.sleep(min(_RETRY_BACKOFF, max(0.0, deadline - loop.time())))
        session += 1
        try:
            token = await token_factory()
            reader, writer, stream_ssn = await open_stream(camera, token, stream=stream)
        except StreamError as err:
            _LOGGER.debug("session %s handshake failed: %s", session, err)
            continue
        try:
            transport, media = await _capture_session(
                reader,
                writer,
                stream_ssn,
                verification_code=verification_code,
                deadline=deadline,
            )
        finally:
            writer.close()
        if not media:
            continue
        jpeg = await _decode_jpeg(ffmpeg_bin, transport, media)
        if jpeg:
            _LOGGER.debug("decoded a frame after %s session(s)", session)
            return jpeg
    return None


async def iter_annexb(
    camera: EzvizCamera,
    token_factory: Callable[[], Awaitable[str]],
    *,
    stream: int,
) -> AsyncIterator[bytes]:
    """
    Yield Annex-B HEVC chunks continuously, reconnecting across the ~27 s drop.

    For RTP/HEVC cameras (battery cams) only. Runs until the consumer stops iterating
    (the driving task is cancelled when no client is watching - battery-friendly).
    MPEG-PS (encrypted IPC) needs continuous decryption + a remux and is handled
    separately (C.2b). ``token_factory`` yields a fresh VTDU token per reconnect.
    """
    while True:
        try:
            token = await token_factory()
            reader, writer, stream_ssn = await open_stream(camera, token, stream=stream)
        except StreamError as err:
            _LOGGER.debug("stream handshake failed: %s", err)
            await asyncio.sleep(_RETRY_BACKOFF)
            continue

        loop = asyncio.get_running_loop()
        frames = _FrameReader(reader)
        depacketizer = HevcDepacketizer()
        last_ka = loop.time()
        try:
            while True:
                if stream_ssn and loop.time() - last_ka >= _KEEPALIVE_INTERVAL:
                    writer.write(build_keepalive(stream_ssn))
                    await writer.drain()
                    last_ka = loop.time()
                frame = await frames.next_frame(loop.time() + _READ_SLICE)
                if frame is None:
                    if frames.closed:
                        break  # the ~27 s VTDU drop; reconnect below
                    continue
                channel, _msg, body = frame
                if channel != CH_STREAM or not body:
                    continue
                if detect_transport(body) == "rtp":
                    chunk = depacketizer.push(body)
                    if chunk:
                        yield chunk
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        await asyncio.sleep(_RETRY_BACKOFF)


async def stream_annexb(
    camera: EzvizCamera,
    token_factory: Callable[[], Awaitable[str]],
    out: IO[bytes],
    *,
    stream: int,
) -> None:
    """
    Write the continuous Annex-B HEVC stream to ``out`` (blocking file-like).

    Thin wrapper over :func:`iter_annexb` for the standalone CLI producer
    (``producer.py``); the integration itself consumes ``iter_annexb`` in-process via
    :mod:`broadcast`. Runs until cancelled.
    """
    async for chunk in iter_annexb(camera, token_factory, stream=stream):
        out.write(chunk)
        out.flush()
