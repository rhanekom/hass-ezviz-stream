"""Tests for the EZVIZ cloud-replay transport (framing, message reader, streaming).

The real replay socket needs the live cloud (exercised by
``scripts/ezviz_replay_probe.py``); here the TLS socket is replaced by a fake that
replays canned server frames, so the framing, XML parsing, end-of-stream handling,
and the async bridge are all covered offline.
"""

from __future__ import annotations

import pytest

from custom_components.ezviz_stream import cloud_replay as cr
from custom_components.ezviz_stream.cloud_replay import (
    CloudReplayError,
    _build_open_xml,
    _frame,
    _md5_hex,
    _parse_stream_url,
    _run_cloud_replay,
    iter_cloud_replay_ps,
)


def _server_message(data: bytes, *, data_type: int = 0, result: int = 0) -> bytes:
    """Build one framed server message the reader can parse (32-byte header stripped).

    Layout: a 32-byte frame header (any non-<?xml bytes), the XML control header,
    a CRLF, the binary body, then the MD5 over ``xml + CRLF + body``.
    """
    xml = (
        b'<?xml version="1.0" encoding="utf-8"?>\n<Response>\n'
        b"\t<Type>%d</Type>\n\t<Length>%d</Length>\n\t<Result>%d</Result>\n</Response>"
        % (data_type, len(data), result)
    )
    framed = xml + b"\r\n" + data
    return (b"\x00" * 32) + framed + _md5_hex(framed)


class _FakeSocket:
    """A socket stand-in: hands out queued bytes, then reports a clean close."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, _bufsize: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        pass


def _factory(chunks: list[bytes]) -> object:
    return lambda _host, _port: _FakeSocket(chunks)


# --- framing / helpers ------------------------------------------------------ #
def test_frame_has_header_payload_and_md5() -> None:
    frame = _frame(b"hello", sequence=1, command=cr._OPEN_CMD)
    assert len(frame) == cr._FRAME_HEADER.size + len(b"hello") + 32
    fields = cr._FRAME_HEADER.unpack(frame[: cr._FRAME_HEADER.size])
    assert fields[0] == cr._MAGIC
    assert fields[4] == cr._OPEN_CMD
    assert frame[-32:] == _md5_hex(b"hello")


def test_parse_stream_url() -> None:
    assert _parse_stream_url("cas.example.com:6500") == ("cas.example.com", 6500)
    for bad in ("no-port", "host:", "host:abc", ""):
        with pytest.raises(CloudReplayError):
            _parse_stream_url(bad)


def test_build_open_xml_carries_clip_descriptor() -> None:
    xml = _build_open_xml(
        ticket="TICKET",
        serial="SN1",
        channel=1,
        seq_id="SEQ",
        begin_cas="20260101T000000Z",
        end_cas="20260101T000010Z",
        storage_version=2,
        video_type=2,
    )
    assert b"<Token>TICKET</Token>" in xml
    assert b'Id="SEQ"' in xml
    assert b'SubSerial="SN1_1"' in xml
    assert b'Begin="20260101T000000Z"' in xml


# --- blocking read loop ----------------------------------------------------- #
def test_run_collects_media_until_close() -> None:
    chunks = [_server_message(b"AAAA"), _server_message(b"BBBB")]
    got: list[bytes] = []
    _run_cloud_replay(
        stream_url="h:1",
        ticket="t",
        serial="SN1",
        channel=1,
        seq_id="S",
        begin_cas="b",
        end_cas="e",
        storage_version=2,
        video_type=2,
        on_media=got.append,
        should_stop=lambda: False,
        socket_factory=_factory(chunks),
    )
    assert b"".join(got) == b"AAAABBBB"


def test_run_stops_at_file_size() -> None:
    # Three media messages, but file_size is reached after the first two.
    chunks = [
        _server_message(b"AAAA"),
        _server_message(b"BBBB"),
        _server_message(b"CC"),
    ]
    got: list[bytes] = []
    _run_cloud_replay(
        stream_url="h:1",
        ticket="t",
        serial="SN1",
        channel=1,
        seq_id="S",
        begin_cas="b",
        end_cas="e",
        storage_version=2,
        video_type=2,
        on_media=got.append,
        should_stop=lambda: False,
        file_size=8,
        socket_factory=_factory(chunks),
    )
    assert b"".join(got) == b"AAAABBBB"  # stopped before the third message


def test_run_raises_on_error_result() -> None:
    chunks = [_server_message(b"", result=5)]
    with pytest.raises(CloudReplayError):
        _run_cloud_replay(
            stream_url="h:1",
            ticket="t",
            serial="SN1",
            channel=1,
            seq_id="S",
            begin_cas="b",
            end_cas="e",
            storage_version=2,
            video_type=2,
            on_media=lambda _c: None,
            should_stop=lambda: False,
            socket_factory=_factory(chunks),
        )


def test_run_raises_on_bad_md5() -> None:
    corrupt = _server_message(b"AAAA")[:-1] + b"x"  # tamper the trailing digest
    with pytest.raises(CloudReplayError):
        _run_cloud_replay(
            stream_url="h:1",
            ticket="t",
            serial="SN1",
            channel=1,
            seq_id="S",
            begin_cas="b",
            end_cas="e",
            storage_version=2,
            video_type=2,
            on_media=lambda _c: None,
            should_stop=lambda: False,
            socket_factory=_factory([corrupt]),
        )


# --- async bridge ----------------------------------------------------------- #
async def test_iter_yields_media_then_ends() -> None:
    chunks = [_server_message(b"AAAA"), _server_message(b"BBBB")]
    out = [
        chunk
        async for chunk in iter_cloud_replay_ps(
            stream_url="h:1",
            ticket="t",
            serial="SN1",
            channel=1,
            seq_id="S",
            begin_cas="b",
            end_cas="e",
            verification_code="",  # no decrypt: pass media through unchanged
            socket_factory=_factory(chunks),
        )
    ]
    assert b"".join(out) == b"AAAABBBB"


async def test_iter_propagates_server_error() -> None:
    chunks = [_server_message(b"", result=7)]
    with pytest.raises(CloudReplayError):
        async for _chunk in iter_cloud_replay_ps(
            stream_url="h:1",
            ticket="t",
            serial="SN1",
            channel=1,
            seq_id="S",
            begin_cas="b",
            end_cas="e",
            verification_code="",
            socket_factory=_factory(chunks),
        ):
            pass
