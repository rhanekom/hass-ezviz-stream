#!/usr/bin/env python3
"""Extract EZVIZ ysproto control messages from a packet capture.

Purpose: find the **I-frame / keyframe request** opcode the official EZVIZ client
(Studio / app) sends, which isn't publicly documented (see doc/TODO.md). Capture a
pcap while the official client live-views an IPC (MPEG-PS/H.264) camera, then run
this on it — it reassembles each TCP stream, parses the 8-byte ysproto framing
(magic 0x24, channel, len, seq, msgcode; see reference.md B.1), and prints every
client→server control message with its opcode and protobuf body decoded. Any
opcode that is NOT one we already know (StreamInfoReq/Rsp, KeepAlive) is a
candidate I-frame request.

Reads the capture directly in Python via scapy — no external tools, so it runs the
same on Windows or in the devcontainer. Capture the pcap with Wireshark on Windows
(it produces the .pcapng), then either run this on Windows or drop the file into
the repo and run it in the devcontainer:

    uv run python scripts/parse_ysproto_pcap.py <capture.pcapng>

⚠ The StreamInfoReq body contains your stream token (`ssn=`); review the output
before sharing it. This tool prints bodies as hex + decoded protobuf fields so you
can redact tokens/serials — the *opcode* itself is what we need, and it is not
sensitive.
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import defaultdict
from pathlib import Path

try:
    import ezviz_cloud as ez
except ImportError:  # pragma: no cover
    sys.exit("Run from the repo, e.g. `uv run python scripts/parse_ysproto_pcap.py …`.")

try:
    from scapy.all import IP, TCP, PcapReader
except ImportError:  # pragma: no cover
    sys.exit("scapy not installed — run `uv sync` (it's a dev dependency).")

MAGIC = 0x24
OPCODE_NAMES = {
    0x132: "KeepAliveReq",
    0x133: "KeepAliveRsp",
    0x135: "KeepAlive",
    0x13B: "StreamInfoReq",
    0x13C: "StreamInfoRsp",
}
KNOWN = set(OPCODE_NAMES)


def read_pcap(pcap: Path) -> list[tuple[str, str, bytes]]:
    """Return [(src_endpoint, dst_endpoint, payload)] for TCP packets carrying data,
    in capture order (good enough to reassemble a clean short capture)."""
    rows: list[tuple[str, str, bytes]] = []
    with PcapReader(str(pcap)) as pr:
        for pkt in pr:
            if not (pkt.haslayer(IP) and pkt.haslayer(TCP)):
                continue
            payload = bytes(pkt[TCP].payload)
            if not payload:
                continue
            ip, tcp = pkt[IP], pkt[TCP]
            src = f"{ip.src}:{tcp.sport}"
            dst = f"{ip.dst}:{tcp.dport}"
            rows.append((src, dst, payload))
    return rows


def parse_frames(buf: bytes) -> list[tuple[int, int, bytes]]:
    """Parse a reassembled byte stream into (channel, msgcode, body) ysproto frames."""
    frames, i = [], 0
    while i + 8 <= len(buf):
        if buf[i] != MAGIC:
            i += 1  # resync on the magic byte
            continue
        _, ch, length, _seq, msg = struct.unpack(">BBHHH", buf[i : i + 8])
        if i + 8 + length > len(buf):
            break
        frames.append((ch, msg, buf[i + 8 : i + 8 + length]))
        i += 8 + length
    return frames


def show_body(body: bytes) -> None:
    ell = "…" if len(body) > 64 else ""
    print(f"      body ({len(body)}B) hex: {body[:64].hex(' ')}{ell}")
    fields = ez.decode_protobuf(body)
    for fn in sorted(fields):
        for val in fields[fn]:
            if isinstance(val, (bytes, bytearray)):
                try:
                    printable = val.decode()
                    shown = (
                        f'"{printable}"' if printable.isprintable() else val.hex(" ")
                    )
                except UnicodeDecodeError:
                    shown = val.hex(" ")
                print(f"        field {fn} (len {len(val)}): {shown[:120]}")
            else:
                print(f"        field {fn} (varint): {val}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract ysproto control messages from a pcap"
    )
    ap.add_argument("pcap", type=Path, help="capture file (.pcap/.pcapng)")
    ap.add_argument(
        "--all-opcodes",
        action="store_true",
        help="show known opcodes too, not just unknown",
    )
    args = ap.parse_args()
    if not args.pcap.exists():
        sys.exit(f"no such file: {args.pcap}")

    # Reassemble each src→dst direction in capture order.
    directions: dict[tuple[str, str], bytearray] = defaultdict(bytearray)
    for src, dst, payload in read_pcap(args.pcap):
        directions[src, dst] += payload

    # A direction is "client→server" if it carries a StreamInfoReq (0x13b).
    candidates: dict[int, int] = defaultdict(int)
    found_any = False
    for (src, dst), buf in directions.items():
        frames = parse_frames(bytes(buf))
        if not frames:
            continue
        opcodes = {msg for _ch, msg, _b in frames}
        is_client = ez.MSG_STREAMINFO_REQ in opcodes
        if not is_client:
            continue
        found_any = True
        print(f"\n=== {src} → {dst} (client → server) — {len(frames)} frames ===")
        for ch, msg, body in frames:
            name = OPCODE_NAMES.get(msg, "❓ UNKNOWN")
            if msg not in KNOWN:
                candidates[msg] += 1
            if msg in KNOWN and not args.all_opcodes:
                continue
            print(f"  ch=0x{ch:02x}  opcode=0x{msg:03x}  {name}")
            if body:
                show_body(body)

    if not found_any:
        print("No ysproto client→server stream found (no StreamInfoReq / 0x13b seen).")
        print("Check you captured the plaintext TCP to the VTM (:8554) / VTDU (:600x).")
        return 1
    print("\n--- candidate I-frame opcodes (client→server, not previously known) ---")
    if candidates:
        for op, n in sorted(candidates.items()):
            print(f"  0x{op:03x}  x{n}")
        print("Share these opcode(s) + body field structure (redact any token/serial).")
    else:
        print("  none — the client sent only known opcodes on this capture.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
