"""Tests for our EZVIZ Image-Encryption video decryptor (scripts/ezviz_decrypt.py).

Two guarantees, on synthetic MPEG-PS built here (no real camera data / secrets):

* **Round-trip:** decrypt(encrypt(clear)) == clear, for H.264 with encrypted NAL
  header (nalu_header_size=0, our IPC cams' case), H.264 with a clear header (1),
  and HEVC (2).
* **Oracle equivalence:** our output is byte-for-byte identical to
  ``pyezvizapi.stream.decrypt_hikvision_ps_video`` - the upstream implementation our
  algorithm derives from, kept as a **dev-only** dependency (never a runtime dep;
  HA core pins an incompatible ``pyezvizapi`` version - see doc/TODO.md).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from Crypto.Cipher import AES
from pyezvizapi.stream import (
    decrypt_hikvision_ps_video as oracle_decrypt,
)
from pyezvizapi.stream import (
    detect_hikvision_ps_video_nalu_header_size as oracle_detect,
)

from custom_components.ezviz_stream import decrypt as ez

CODE = "ezviz-test-code"  # synthetic AES key material for tests, not a real secret


def _aes_key(key: str) -> bytes:
    return key.encode().ljust(16, b"\0")[:16]


def _pack_header() -> bytes:
    """A minimal valid MPEG-2 pack header (14 bytes, zero stuffing)."""
    b = bytearray(b"\x00\x00\x01\xba" + bytes(10))
    b[4] = 0x44  # marker bits checked by _is_mpeg2_pack_header
    b[6] |= 0x04
    b[8] |= 0x04
    b[12] |= 0x01
    b[13] = 0xF8
    return bytes(b)


def _video_pes(payload: bytes) -> bytes:
    """A video PES packet (stream 0xE0), minimal 3-byte PES header, bounded length."""
    body = b"\x80\x00\x00" + payload  # flags=0x80 (marker), no PTS, header_len=0
    return b"\x00\x00\x01\xe0" + len(body).to_bytes(2, "big") + body


def _nal(header: bytes, body_len: int) -> bytes:
    """Annex-B NAL: long start code + codec header bytes + pseudo-random body."""
    body = bytes([(i * 7 + 3) & 0xFF for i in range(body_len)])
    return ez.ANNEX_B_LONG_START_CODE + header + body


def _encrypt_ps(clear: bytes, key: str, nalu_header_size: int) -> bytes:
    """Encrypt the fixture the same way EZVIZ does, so decrypt() must invert it.

    Locates NAL start codes in the *clear* stream, then AES-ECB encrypts the first
    ``NAL_ENCRYPTED_PREFIX_LEN`` body bytes (after ``nalu_header_size`` header bytes)
    in 16-byte blocks accumulated across the contiguous video PES run.
    """
    ranges = ez._video_pes_payload_ranges(clear)
    offsets: list[int] = []
    run = bytearray()
    for start, end in ranges:
        for pos in range(start, end):
            offsets.append(pos)
            run.append(clear[pos])
    starts = ez._find_nal_start_codes(bytes(run), 0, len(run))
    for idx, (pos, length) in enumerate(starts):
        seg_end = starts[idx + 1][0] if idx + 1 < len(starts) else len(run)
        first = pos + length + nalu_header_size
        last = min(seg_end, first + ez.NAL_ENCRYPTED_PREFIX_LEN)
        positions: list[int] = []
        block = bytearray()
        for p in range(first, last):
            positions.append(p)
            block.append(run[p])
            if len(block) == 16:
                enc = AES.new(_aes_key(key), AES.MODE_ECB).encrypt(bytes(block))
                for bp, eb in zip(positions, enc, strict=True):
                    run[bp] = eb
                positions.clear()
                block.clear()
    out = bytearray(clear)
    for op, rb in zip(offsets, run, strict=True):
        out[op] = rb
    return bytes(out)


# H.264 IDR NAL header (type 5, nri 3) = 0x65; HEVC VPS (type 32) = 0x40 0x01.
_CASES = {
    0: b"\x65",  # H.264, whole NAL header encrypted
    1: b"\x65",  # H.264, clear header preserved
    2: b"\x40\x01",  # HEVC, 2-byte clear header preserved
}


def _fixture(nalu_header_size: int) -> tuple[bytes, bytes]:
    """Return (clear, encrypted); one NAL body split across two video PES packets."""
    nal = _nal(_CASES[nalu_header_size], body_len=300)
    clear = _pack_header() + _video_pes(nal[:160]) + _video_pes(nal[160:])
    return clear, _encrypt_ps(clear, CODE, nalu_header_size)


def _audio_pes() -> bytes:
    """A minimal non-video (audio, 0xC0) PES packet - it ends a video-PES run."""
    return b"\x00\x00\x01\xc0\x00\x04" + bytes(4)


def _multi_run_fixture(nalu_header_size: int) -> tuple[bytes, bytes]:
    """Three video-PES runs (one NAL each, split across 2 PES) separated by audio."""
    parts = [_pack_header()]
    for _frame in range(3):
        nal = _nal(_CASES[nalu_header_size], body_len=300)
        parts += [_video_pes(nal[:160]), _video_pes(nal[160:]), _audio_pes()]
    clear = b"".join(parts)
    return clear, _encrypt_ps(clear, CODE, nalu_header_size)


@pytest.mark.parametrize("nhs", [0, 1, 2])
def test_round_trip(nhs: int) -> None:
    clear, enc = _fixture(nhs)
    assert enc != clear  # encryption actually changed the body
    assert ez.decrypt_ps_video(enc, CODE, nalu_header_size=nhs) == clear


@pytest.mark.parametrize("nhs", [0, 1, 2])
def test_matches_oracle(nhs: int) -> None:
    _clear, enc = _fixture(nhs)
    assert ez.decrypt_ps_video(enc, CODE, nalu_header_size=nhs) == oracle_decrypt(
        enc, CODE, nalu_header_size=nhs
    )


@pytest.mark.parametrize("nhs", [0, 1, 2])
def test_detect_matches_oracle(nhs: int) -> None:
    _clear, enc = _fixture(nhs)
    assert ez.detect_nalu_header_size(enc, CODE) == oracle_detect(enc, CODE)


def test_auto_detect_used_when_header_size_none() -> None:
    # H.264 encrypted-header fixture should auto-detect to 0 and round-trip.
    clear, enc = _fixture(0)
    assert ez.detect_nalu_header_size(enc, CODE) == 0
    assert ez.decrypt_ps_video(enc, CODE) == clear


def test_non_ps_input_is_returned_unchanged() -> None:
    # No video PES packets → nothing to decrypt.
    junk = b"not an mpeg-ps stream at all" * 4
    assert ez.decrypt_ps_video(junk, CODE, nalu_header_size=0) == junk


@pytest.mark.parametrize("nhs", [0, 1, 2])
@pytest.mark.parametrize("chunk", [1, 7, 64, 100000])
def test_streaming_matches_one_shot(nhs: int, chunk: int) -> None:
    """StreamingPsDecryptor over arbitrary chunk splits == one-shot decrypt_ps_video."""
    _clear, enc = _multi_run_fixture(nhs)
    expected = ez.decrypt_ps_video(enc, CODE, nalu_header_size=nhs)

    dec = ez.StreamingPsDecryptor(CODE, nalu_header_size=nhs)
    out = bytearray()
    for i in range(0, len(enc), chunk):
        out += dec.feed(enc[i : i + chunk])
    out += dec.flush()

    assert bytes(out) == expected


def test_streaming_autodetects_header_size_on_flush() -> None:
    """With no explicit header size, flush detects it and decrypts the buffer."""
    clear, enc = _multi_run_fixture(0)
    dec = ez.StreamingPsDecryptor(CODE)  # header size auto-detected
    out = dec.feed(enc) + dec.flush()
    assert out == ez.decrypt_ps_video(enc, CODE) == clear


# --- richer fixtures: >4096-byte prefix, multi-NAL runs, unbounded PES ------- #
def _split_video_pes(payload: bytes, chunk: int = 1500) -> bytes:
    """Carry ``payload`` across a run of bounded video PES packets."""
    return b"".join(
        _video_pes(payload[i : i + chunk]) for i in range(0, len(payload), chunk)
    )


def _large_nal_fixture(nalu_header_size: int) -> tuple[bytes, bytes]:
    """A NAL whose body exceeds NAL_ENCRYPTED_PREFIX_LEN, then a second NAL.

    Exercises the 4096-byte prefix cap (only the prefix is encrypted, the tail is
    left clear) and the post-prefix start-code handling for a following NAL.
    """
    big = _nal(_CASES[nalu_header_size], body_len=ez.NAL_ENCRYPTED_PREFIX_LEN + 512)
    small = _nal(_CASES[nalu_header_size], body_len=200)
    clear = _pack_header() + _split_video_pes(big + small)
    return clear, _encrypt_ps(clear, CODE, nalu_header_size)


def _zero_length_video_pes(payload: bytes) -> bytes:
    """An unbounded (length=0) video PES packet, ended by the next start code."""
    body = b"\x80\x00\x00" + payload  # flags=0x80, no PTS, header_len=0
    return b"\x00\x00\x01\xe0" + (0).to_bytes(2, "big") + body


def _unbounded_fixture(nalu_header_size: int) -> tuple[bytes, bytes]:
    """An unbounded video PES terminated by a following pack header."""
    nal = _nal(_CASES[nalu_header_size], body_len=120)
    clear = _pack_header() + _zero_length_video_pes(nal) + _pack_header()
    return clear, _encrypt_ps(clear, CODE, nalu_header_size)


@pytest.mark.parametrize("nhs", [0, 1, 2])
def test_large_nal_prefix_cap_matches_oracle(nhs: int) -> None:
    """A >4096-byte NAL exercises the prefix cap; decrypt matches the oracle exactly.

    Round-trip is not asserted here: a large pseudo-random body can encrypt to
    ciphertext that contains a false start code, making the transform legitimately
    non-invertible - so the guarantee that matters is byte-for-byte agreement with the
    upstream oracle, which both implementations honour.
    """
    _clear, enc = _large_nal_fixture(nhs)
    ours = ez.decrypt_ps_video(enc, CODE, nalu_header_size=nhs)
    assert ours == oracle_decrypt(enc, CODE, nalu_header_size=nhs)


@pytest.mark.parametrize("nhs", [0, 1, 2])
def test_unbounded_video_pes_round_trip_and_oracle(nhs: int) -> None:
    """A zero-length (unbounded) video PES decrypts correctly and matches the oracle."""
    clear, enc = _unbounded_fixture(nhs)
    ours = ez.decrypt_ps_video(enc, CODE, nalu_header_size=nhs)
    assert ours == clear
    assert ours == oracle_decrypt(enc, CODE, nalu_header_size=nhs)


@pytest.mark.parametrize("nhs", [0, 1, 2])
def test_large_nal_streaming_matches_one_shot(nhs: int) -> None:
    """Streaming a >4096-byte NAL in tiny chunks equals the one-shot decrypt."""
    _clear, enc = _large_nal_fixture(nhs)
    expected = ez.decrypt_ps_video(enc, CODE, nalu_header_size=nhs)
    dec = ez.StreamingPsDecryptor(CODE, nalu_header_size=nhs)
    out = bytearray()
    for i in range(0, len(enc), 13):
        out += dec.feed(enc[i : i + 13])
    out += dec.flush()
    assert bytes(out) == expected


# --- guard rails ------------------------------------------------------------ #
def test_negative_nalu_header_size_raises() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        ez.decrypt_ps_video(_pack_header(), CODE, nalu_header_size=-1)


def test_detect_returns_default_without_video_evidence() -> None:
    """No video NAL evidence falls back to the requested default (2, or None)."""
    junk = b"no video pes packets in here " * 8
    assert ez.detect_nalu_header_size(junk, CODE) == 2
    assert ez.detect_nalu_header_size(junk, CODE, default=None) is None


def test_streaming_defers_until_enough_to_detect() -> None:
    """Auto-detect emits nothing until enough bytes accumulate to pick a header size."""
    dec = ez.StreamingPsDecryptor(CODE)  # no explicit header size
    assert dec.feed(_pack_header()) == b""  # far below the detection threshold


def test_streaming_safety_valve_on_open_run() -> None:
    """An open run that never boundaries past the cap emits nothing, without hanging."""
    with patch.object(ez, "_MAX_STREAM_BUFFER", 16):
        dec = ez.StreamingPsDecryptor(CODE, nalu_header_size=2)
        chunk = b"\x00\x00\x01\xe0\x00\x00\x80\x00\x00" + bytes(64)
        assert dec.feed(chunk) == b""  # nothing complete to emit despite the big buffer
