"""Tests for the pure ysproto media protocol helpers."""

from __future__ import annotations

from custom_components.ezviz_stream import ysproto


def _rtp(payload: bytes) -> bytes:
    """Wrap an RTP payload in a minimal 12-byte header (V2, PT 96, no CSRC/ext)."""
    return bytes([0x80, 0x60]) + bytes(10) + payload


# --- transport detection ---------------------------------------------------- #
def test_detect_transport() -> None:
    assert ysproto.detect_transport(b"\x00\x00\x01\xba\x00\x00") == "mpeg-ps"
    assert ysproto.detect_transport(b"\x47\x00\x00") == "mpeg-ts"
    assert ysproto.detect_transport(b"\x80\x60\x00") == "rtp"
    assert ysproto.detect_transport(b"\xff\x00\x00") == "unknown"
    assert ysproto.detect_transport(b"") == "unknown"


# --- frame framing ---------------------------------------------------------- #
def test_build_read_frame_roundtrip() -> None:
    frame = ysproto.build_frame(ysproto.CH_STREAM, ysproto.MSG_STREAMINFO_RSP, b"body")
    parsed, consumed = ysproto.read_frame(frame)
    assert parsed == (ysproto.CH_STREAM, ysproto.MSG_STREAMINFO_RSP, b"body")
    assert consumed == len(frame)


def test_read_frame_skips_leading_garbage() -> None:
    frame = ysproto.build_frame(ysproto.CH_MSG, ysproto.MSG_KEEPALIVE_REQ, b"x")
    parsed, consumed = ysproto.read_frame(b"\x99\x98" + frame)
    assert parsed == (ysproto.CH_MSG, ysproto.MSG_KEEPALIVE_REQ, b"x")
    assert consumed == 2 + len(frame)


def test_read_frame_incomplete_keeps_partial() -> None:
    frame = ysproto.build_frame(ysproto.CH_STREAM, 0x100, b"abcd")
    parsed, consumed = ysproto.read_frame(frame[:-2])  # truncated body
    assert parsed is None
    assert consumed == 0  # keep everything from the magic


def test_build_keepalive_wraps_ssn_as_protobuf() -> None:
    """The keepalive body must be the ssn as protobuf field 1, not the raw string.

    Packet capture of the official client showed a 0x0a/0x33-prefixed body; sending
    the raw string instead makes the VTDU FIN the connection (~5.5 s session churn).
    """
    frame = ysproto.build_keepalive("ABC", seq=1)
    # magic 24, channel 00 (control), len 0x0005, seq 0x0001, msgcode 0x0132, body.
    assert frame == bytes.fromhex("2400000500010132") + b"\x0a\x03ABC"

    parsed, _ = ysproto.read_frame(frame)
    assert parsed is not None
    channel, msgcode, body = parsed
    assert channel == ysproto.CH_MSG
    assert msgcode == ysproto.MSG_KEEPALIVE_REQ
    assert body == b"\x0a\x03ABC"  # protobuf field 1 = the ssn
    assert body != b"ABC"  # regression: the raw string body is fatal


def test_build_keepalive_defaults_seq_zero() -> None:
    """Seq defaults to 0 and is honoured when passed (official client increments it)."""
    assert ysproto.build_keepalive("X")[4:6] == b"\x00\x00"
    assert ysproto.build_keepalive("X", seq=7)[4:6] == b"\x00\x07"


# --- protobuf --------------------------------------------------------------- #
def test_streaminforeq_protobuf_roundtrip() -> None:
    body = ysproto.encode_streaminforeq("ysproto://host/live?dev=X", "vtmkey")
    fields = ysproto.decode_protobuf(body)
    assert ysproto.field_str(fields, 1) == "ysproto://host/live?dev=X"
    assert ysproto.field_str(fields, 2) == "vtmkey"
    assert ysproto.field_str(fields, 3) == ysproto.VERSION_TAG
    assert fields[4][0] == 0  # proxytype


# --- stream URL ------------------------------------------------------------- #
def test_build_stream_url_and_set_stream_param() -> None:
    url = ysproto.build_stream_url("1.2.3.4", 6001, "SN1", 1, "tok", stream=2)
    assert "stream=2" in url
    assert "dev=SN1" in url
    assert "ssn=tok" in url
    assert "timestamp=" not in url  # omitted when not provided
    assert ysproto.set_stream_param(url, 1).count("stream=1") == 1


def test_build_stream_url_timestamp() -> None:
    url = ysproto.build_stream_url(
        "1.2.3.4", 6001, "SN1", 1, "tok", stream=1, timestamp_ms=1700000000000
    )
    assert "&timestamp=1700000000000" in url


# --- HEVC de-packetiser ----------------------------------------------------- #
def test_depacketize_single_nal() -> None:
    depacketizer = ysproto.HevcDepacketizer()
    # HEVC NAL header byte0=0x02 -> nal_type 1 (< 48, a single NAL unit).
    payload = bytes([0x02, 0x01, 0xAA, 0xBB])
    assert depacketizer.push(_rtp(payload)) == ysproto.ANNEX_B_START_CODE + payload


def test_depacketize_fragmentation_unit() -> None:
    depacketizer = ysproto.HevcDepacketizer()
    # FU: outer NAL type 49; FU header start=0x80|type, end=0x40|type; type 1.
    start_pkt = _rtp(bytes([0x62, 0x01, 0x81]) + b"\xaa\xbb")
    end_pkt = _rtp(bytes([0x62, 0x01, 0x41]) + b"\xcc")
    assert depacketizer.push(start_pkt) == b""  # still assembling
    # Reassembled NAL = rebuilt 2-byte header (0x02, 0x01) + all fragments.
    assert (
        depacketizer.push(end_pkt)
        == ysproto.ANNEX_B_START_CODE + b"\x02\x01\xaa\xbb\xcc"
    )


def test_depacketize_aggregation_packet() -> None:
    depacketizer = ysproto.HevcDepacketizer()
    # AP: outer NAL type 48; two length-prefixed NALs.
    nal_a, nal_b = b"\x02\x01\xaa", b"\x04\x01\xbb\xcc"
    payload = (
        bytes([0x60, 0x01])
        + len(nal_a).to_bytes(2, "big")
        + nal_a
        + len(nal_b).to_bytes(2, "big")
        + nal_b
    )
    out = depacketizer.push(_rtp(payload))
    assert (
        out == ysproto.ANNEX_B_START_CODE + nal_a + ysproto.ANNEX_B_START_CODE + nal_b
    )


def test_depacketize_ignores_non_rtp() -> None:
    depacketizer = ysproto.HevcDepacketizer()
    assert depacketizer.push(b"\x00\x00\x01\xba too short-ish") == b""
