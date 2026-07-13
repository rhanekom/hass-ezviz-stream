# SPDX-License-Identifier: Apache-2.0
"""EZVIZ Image-Encryption video decryption (AES-ECB over MPEG-PS video NALs).

Our own implementation of the decryption EZVIZ applies to its cloud/SDK MPEG-PS
video when *Image Encryption* is ON. The MPEG-PS container (pack/system/PSM/PES
framing) and the Annex-B NAL start codes stay in the clear; only the first
``NAL_ENCRYPTED_PREFIX_LEN`` bytes of each **video** NAL *body* — after
``nalu_header_size`` clear codec-header bytes — are AES-ECB encrypted, with the
camera's verification code zero-padded/truncated to 16 bytes as the key (no IV).

A single NAL body can span several video PES packets, so the AES 16-byte blocks
must accumulate across PES boundaries (the container bytes between payloads are not
part of the cipher stream). ``nalu_header_size`` is ``2`` for HEVC, ``1`` for H.264
with a clear NAL header, and ``0`` for H.264 whose NAL header is itself encrypted
(the case observed on our IPC cams); :func:`detect_nalu_header_size` picks it.

We roll our own so the integration takes **no runtime dependency on pyezvizapi**
(HA core pins ``pyezvizapi==1.0.0.7``, which would clash — see doc/TODO.md). The
algorithm is derived from ``RenierM26/pyEzvizApi`` (Apache-2.0),
``pyezvizapi.stream.decrypt_hikvision_ps_video``, and is validated byte-for-byte
against it in the test suite. Runtime dependency: ``pycryptodome`` only.
"""

from __future__ import annotations

from typing import Any

from Crypto.Cipher import AES

MPEG_START_CODE_PREFIX = b"\x00\x00\x01"
ANNEX_B_LONG_START_CODE = b"\x00\x00\x00\x01"
NAL_ENCRYPTED_PREFIX_LEN = 4096
AES_BLOCK = 16

_PACK_HEADER = 0xBA
_SYSTEM_HEADER = 0xBB
_PROGRAM_STREAM_MAP = 0xBC
_PRIVATE_STREAM_1 = 0xBD
_PADDING = 0xBE
_PRIVATE_STREAM_2 = 0xBF


def _aes(key: bytes) -> Any:  # AES.new returns an opaque ECB cipher object
    return AES.new(key, AES.MODE_ECB)


def _aes_key(key: str | bytes) -> bytes:
    key_bytes = key.encode() if isinstance(key, str) else key
    return key_bytes.ljust(16, b"\0")[:16]


# --------------------------------------------------------------------------- #
# MPEG-PS stream-id classifiers + PES/packet parsing
# --------------------------------------------------------------------------- #
def _is_video_pes_stream_id(stream_id: int) -> bool:
    return 0xE0 <= stream_id <= 0xEF


def _is_metadata_stream_id(stream_id: int) -> bool:
    return stream_id in {_PACK_HEADER, _SYSTEM_HEADER, _PROGRAM_STREAM_MAP, _PADDING}


def _is_packet_start_id(stream_id: int) -> bool:
    """Return True for MPEG-PS packet start codes, excluding Annex-B NAL types."""
    return (
        stream_id
        in {
            _PACK_HEADER,
            _SYSTEM_HEADER,
            _PROGRAM_STREAM_MAP,
            _PRIVATE_STREAM_1,
            _PADDING,
            _PRIVATE_STREAM_2,
        }
        or 0xC0 <= stream_id <= 0xEF
    )


def _is_mpeg2_pack_header(data: bytes, start: int) -> bool:
    return (
        (data[start + 4] & 0xC4) == 0x44
        and (data[start + 6] & 0x04) == 0x04
        and (data[start + 8] & 0x04) == 0x04
        and (data[start + 12] & 0x01) == 0x01
        and (data[start + 13] & 0xF8) == 0xF8
    )


def _pes_payload_start(data: bytes, packet_start: int) -> int | None:
    """Payload offset for a complete-enough PES header at ``packet_start``."""
    if packet_start + 9 > len(data):
        return None
    stream_id = data[packet_start + 3]
    flags = data[packet_start + 6]
    if (flags & 0xC0) == 0x80:
        return packet_start + 9 + data[packet_start + 8]
    if 0xC0 <= stream_id <= 0xEF or stream_id == _PRIVATE_STREAM_1:
        return None
    return packet_start + 6


def _mpeg_ps_packet_end(data: bytes, start: int) -> int | None:
    """End offset of a complete MPEG-PS packet at ``start`` (else None)."""
    if start + 4 > len(data) or data[start : start + 3] != MPEG_START_CODE_PREFIX:
        return None
    stream_id = data[start + 3]
    if (
        stream_id == _PACK_HEADER
        and start + 14 <= len(data)
        and _is_mpeg2_pack_header(data, start)
    ):
        stuffing = data[start + 13] & 0x07
        candidate = start + 14 + stuffing
        return candidate if candidate <= len(data) else None
    if _is_packet_start_id(stream_id) and start + 6 <= len(data):
        packet_length = int.from_bytes(data[start + 4 : start + 6], "big")
        candidate = start + 6 + packet_length
        if packet_length and candidate <= len(data):
            if _is_video_pes_stream_id(stream_id) or stream_id == _PRIVATE_STREAM_1:
                payload_start = _pes_payload_start(data, start)
            elif 0xC0 <= stream_id <= 0xDF:
                payload_start = _pes_payload_start(data, start) or start + 6
            else:
                payload_start = start + 6
            if payload_start is not None and payload_start <= candidate:
                return candidate
    return None


def _is_zero_length_video_pes_start(data: bytes, start: int) -> bool:
    return (
        start + 9 <= len(data)
        and data[start : start + 3] == MPEG_START_CODE_PREFIX
        and _is_video_pes_stream_id(data[start + 3])
        and int.from_bytes(data[start + 4 : start + 6], "big") == 0
        and _pes_payload_start(data, start) is not None
    )


def _next_unbounded_video_pes_boundary(data: bytes, start: int) -> int | None:
    """Next packet boundary after a zero-length (unbounded) video PES payload."""
    i = start
    while i < len(data) - 3:
        if _mpeg_ps_packet_end(data, i) is not None or _is_zero_length_video_pes_start(
            data, i
        ):
            return i
        i += 1
    return None


def _video_pes_payload_ranges(data: bytes) -> list[tuple[int, int]]:
    """Payload (start, end) ranges of every video PES packet, in order."""
    ranges: list[tuple[int, int]] = []
    i = 0
    while i < len(data) - 9:
        if data[i : i + 3] != MPEG_START_CODE_PREFIX:
            i += 1
            continue
        stream_id = data[i + 3]
        if not _is_video_pes_stream_id(stream_id):
            i += 4
            continue
        pes_length = int.from_bytes(data[i + 4 : i + 6], "big")
        payload_start = _pes_payload_start(data, i)
        if payload_start is None:
            break
        packet_end = (
            i + 6 + pes_length
            if pes_length
            else _next_unbounded_video_pes_boundary(data, payload_start) or len(data)
        )
        if packet_end > len(data):
            break
        if payload_start < packet_end:
            ranges.append((payload_start, packet_end))
        i = max(i + 4, packet_end)
    return ranges


def _has_non_video_pes_packet(data: bytes, start: int, end: int) -> bool:
    """Return True when a non-video PES packet appears in ``data[start:end]``."""
    i = start
    while i < end - 3:
        if data[i : i + 3] != MPEG_START_CODE_PREFIX:
            i += 1
            continue
        stream_id = data[i + 3]
        if _is_metadata_stream_id(stream_id):
            i += 4
            continue
        if _is_packet_start_id(stream_id) and not _is_video_pes_stream_id(stream_id):
            return True
        i += 4
    return False


# --------------------------------------------------------------------------- #
# Annex-B NAL scanning + header plausibility (used to reject ciphertext that
# accidentally contains 00 00 01, which would shift AES block alignment)
# --------------------------------------------------------------------------- #
def _find_nal_start_codes(data: bytes, start: int, end: int) -> list[tuple[int, int]]:
    positions: list[tuple[int, int]] = []
    i = start
    while i < end - 3:
        if data[i : i + 4] == ANNEX_B_LONG_START_CODE:
            positions.append((i, 4))
            i += 4
        elif data[i : i + 3] == MPEG_START_CODE_PREFIX:
            positions.append((i, 3))
            i += 3
        else:
            i += 1
    return positions


def _h264_nal_type(data: bytes, pos: int, length: int) -> int | None:
    header_pos = pos + length
    if header_pos >= len(data):
        return None
    return data[header_pos] & 0x1F


def _hevc_nal_type(data: bytes, pos: int, length: int) -> int | None:
    header_pos = pos + length
    if header_pos + 1 >= len(data):
        return None
    return (data[header_pos] >> 1) & 0x3F


def _find_h264_nal_start_codes(
    data: bytes, start: int, end: int
) -> list[tuple[int, int]]:
    return [
        (pos, length)
        for pos, length in _find_nal_start_codes(data, start, end)
        if (t := _h264_nal_type(data, pos, length)) is not None and 1 <= t <= 23
    ]


def _find_hevc_nal_start_codes(
    data: bytes, start: int, end: int
) -> list[tuple[int, int]]:
    return [
        (pos, length)
        for pos, length in _find_nal_start_codes(data, start, end)
        if (t := _hevc_nal_type(data, pos, length)) is not None and t <= 40
    ]


def _is_plausible_hevc_header(data: bytes, pos: int, length: int) -> bool:
    header_pos = pos + length
    if header_pos + 1 >= len(data):
        return False
    if (data[header_pos] & 0x80) != 0:
        return False
    nal_type = (data[header_pos] >> 1) & 0x3F
    layer_id = ((data[header_pos] & 0x01) << 5) | (data[header_pos + 1] >> 3)
    return nal_type <= 40 and layer_id == 0 and (data[header_pos + 1] & 0x07) != 0


def _is_plausible_h264_header(data: bytes, pos: int, length: int) -> bool:
    header_pos = pos + length
    if header_pos >= len(data):
        return False
    nal_header = data[header_pos]
    return (nal_header & 0x80) == 0 and 1 <= (nal_header & 0x1F) <= 23


def _is_plausible_hevc_header_bytes(header: bytes) -> bool:
    if len(header) < 2:
        return False
    forbidden_zero = header[0] & 0x80 == 0
    layer_id = ((header[0] & 0x01) << 5) | (header[1] >> 3)
    temporal_id_plus1 = header[1] & 0x07
    nal_type = (header[0] >> 1) & 0x3F
    return forbidden_zero and layer_id == 0 and temporal_id_plus1 > 0 and nal_type <= 40


def _h264_header_score(nal_type: int) -> int:
    if nal_type in {1, 5, 7, 8}:
        return 4
    if nal_type in {2, 3, 4}:
        return 2
    return 1


def _hevc_header_score(nal_type: int) -> int:
    if nal_type in {32, 33, 34}:
        return 4
    if nal_type < 32:
        return 2
    return 1


def detect_nalu_header_size(
    data: bytes, key: str | bytes, *, default: int | None = 2
) -> int | None:
    """Detect clear codec-header bytes to preserve before AES: 2=HEVC, 1=H.264
    clear header, 0=H.264 encrypted header. ``default`` if no video NAL evidence."""
    aes_key = _aes_key(key)
    scores = {"hevc": 0, "hevc-encrypted-header": 0, "h264-clear-header": 0, "h264": 0}
    for payload_start, payload_end in _video_pes_payload_ranges(data):
        for pos, length in _find_nal_start_codes(data, payload_start, payload_end):
            header_pos = pos + length
            if _is_plausible_hevc_header(data, pos, length):
                scores["hevc"] += _hevc_header_score((data[header_pos] >> 1) & 0x3F)
            if _is_plausible_h264_header(data, pos, length):
                scores["h264-clear-header"] += _h264_header_score(
                    data[header_pos] & 0x1F
                )
            if header_pos + AES_BLOCK <= payload_end:
                decrypted = _aes(aes_key).decrypt(
                    bytes(data[header_pos : header_pos + AES_BLOCK])
                )
                first = decrypted[0]
                if _is_plausible_hevc_header_bytes(decrypted[:2]):
                    scores["hevc-encrypted-header"] += _hevc_header_score(
                        (decrypted[0] >> 1) & 0x3F
                    )
                if (first & 0x80) == 0 and 1 <= (first & 0x1F) <= 23:
                    scores["h264"] += _h264_header_score(first & 0x1F)
    if not any(scores.values()):
        return default
    if (
        scores["hevc-encrypted-header"] > scores["hevc"]
        and scores["hevc-encrypted-header"] >= scores["h264"]
        and scores["hevc-encrypted-header"] >= scores["h264-clear-header"]
    ):
        return 0
    if scores["h264"] > scores["hevc"] and scores["h264"] > scores["h264-clear-header"]:
        return 0
    if scores["h264-clear-header"] > scores["hevc"]:
        return 1
    return 2


# --------------------------------------------------------------------------- #
# Decryption
# --------------------------------------------------------------------------- #
def decrypt_ps_video(
    data: bytes, key: str | bytes, *, nalu_header_size: int | None = None
) -> bytes:
    """Decrypt EZVIZ Image-Encryption MPEG-PS video and return the clear stream.

    ``nalu_header_size`` None => auto-detect (:func:`detect_nalu_header_size`).
    """
    if nalu_header_size is None:
        nalu_header_size = detect_nalu_header_size(data, key)
    if nalu_header_size is None:
        nalu_header_size = 2
    if nalu_header_size < 0:
        raise ValueError("nalu_header_size must be non-negative")

    aes_key = _aes_key(key)

    def find_nal_start_codes(buf: bytes, start: int, end: int) -> list[tuple[int, int]]:
        if nalu_header_size == 1:
            return _find_h264_nal_start_codes(buf, start, end)
        if nalu_header_size == 2:
            return _find_hevc_nal_start_codes(buf, start, end)
        # header itself encrypted (0): a real start code precedes an AES block that
        # decrypts to a plausible NAL header.
        out: list[tuple[int, int]] = []
        for pos, length in _find_nal_start_codes(buf, start, end):
            header = pos + length
            if header + AES_BLOCK > end:
                continue
            dec = _aes(aes_key).decrypt(bytes(buf[header : header + AES_BLOCK]))
            t = dec[0] & 0x1F
            if _is_plausible_hevc_header_bytes(dec[:2]) or (
                (dec[0] & 0x80) == 0 and 1 <= t <= 23
            ):
                out.append((pos, length))
        return out

    def decrypt_video_payload_run(payload: bytes) -> bytes:
        payload_output = bytearray(payload)
        pending_positions: list[int] = []
        pending_block = bytearray()
        active_nal = False
        active_nal_decrypted = active_nal_body_start = 0

        def reset() -> None:
            nonlocal active_nal, active_nal_body_start, active_nal_decrypted
            pending_positions.clear()
            pending_block.clear()
            active_nal = False
            active_nal_decrypted = active_nal_body_start = 0

        def decrypt_segment(start: int, end: int) -> None:
            nonlocal active_nal_decrypted
            if end <= start:
                return
            remaining = NAL_ENCRYPTED_PREFIX_LEN - active_nal_decrypted
            if remaining <= 0:
                return
            decrypt_end = min(end, start + remaining)
            for pos in range(start, decrypt_end):
                pending_positions.append(pos)
                pending_block.append(payload_output[pos])
                active_nal_decrypted += 1
                if len(pending_block) != AES_BLOCK:
                    continue
                dec = _aes(aes_key).decrypt(bytes(pending_block))
                for block_pos, dec_byte in zip(pending_positions, dec, strict=True):
                    payload_output[block_pos] = dec_byte
                pending_positions.clear()
                pending_block.clear()

        def starts_plausible_encrypted_nal(start: int, end: int) -> bool:
            if end - start < AES_BLOCK:
                return False
            dec = _aes(aes_key).decrypt(
                bytes(payload_output[start : start + AES_BLOCK])
            )
            t = dec[0] & 0x1F
            return _is_plausible_hevc_header_bytes(dec[:2]) or (1 <= t <= 23)

        def is_post_prefix_tail_lookalike(pos: int, length: int) -> bool:
            if nalu_header_size == 1:
                t = _h264_nal_type(payload, pos, length)
                return t is None or not 1 <= t <= 5
            t = _hevc_nal_type(payload, pos, length)
            return t is None or t >= 32

        nal_starts = find_nal_start_codes(payload, 0, len(payload))
        segment_start = 0
        for idx, (pos, length) in enumerate(nal_starts):
            decrypt_end = (
                nal_starts[idx + 1][0] if idx + 1 < len(nal_starts) else len(payload)
            )
            if active_nal:
                candidate = active_nal_decrypted + max(0, pos - segment_start)
                if candidate < NAL_ENCRYPTED_PREFIX_LEN:
                    if nalu_header_size == 0 and (
                        candidate == 0
                        or not starts_plausible_encrypted_nal(pos + length, decrypt_end)
                    ):
                        continue
                    if nalu_header_size != 0 and candidate == 0:
                        continue
            if (
                active_nal
                and active_nal_decrypted >= NAL_ENCRYPTED_PREFIX_LEN
                and pos > active_nal_body_start + NAL_ENCRYPTED_PREFIX_LEN
                and (
                    (
                        nalu_header_size == 0
                        and not starts_plausible_encrypted_nal(
                            pos + length, decrypt_end
                        )
                    )
                    or (
                        nalu_header_size != 0
                        and is_post_prefix_tail_lookalike(pos, length)
                    )
                )
            ):
                continue
            if active_nal and segment_start < pos:
                decrypt_segment(segment_start, pos)
            reset()
            active_nal = True
            decrypt_start = pos + length + nalu_header_size
            active_nal_body_start = decrypt_start
            decrypt_segment(decrypt_start, decrypt_end)
            segment_start = decrypt_end
        if active_nal and segment_start < len(payload):
            decrypt_segment(segment_start, len(payload))
        return bytes(payload_output)

    output = bytearray(data)
    run_offsets: list[int] = []
    run_bytes = bytearray()

    def flush_run() -> None:
        if not run_bytes:
            return
        decrypted = decrypt_video_payload_run(bytes(run_bytes))
        for out_pos, dec_byte in zip(run_offsets, decrypted, strict=True):
            output[out_pos] = dec_byte
        run_offsets.clear()
        run_bytes.clear()

    previous_video_end: int | None = None
    for payload_start, payload_end in _video_pes_payload_ranges(data):
        if previous_video_end is not None and _has_non_video_pes_packet(
            data, previous_video_end, payload_start
        ):
            flush_run()
        for out_pos in range(payload_start, payload_end):
            run_offsets.append(out_pos)
            run_bytes.append(data[out_pos])
        previous_video_end = payload_end
    flush_run()
    return bytes(output)
