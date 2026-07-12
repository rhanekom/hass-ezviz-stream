#!/usr/bin/env python3
"""EZVIZ cloud live-stream diagnostic tool.

Exercises the end-to-end cloud path (login -> handshake -> channel-0x01 media ->
transport-detect -> de-packetize -> FFmpeg) against *real* cameras and writes a
capture plus a decoded jpg per camera. Kept in the repo as our manual test
harness for the streaming path; the proven logic ports into
``custom_components/ezviz_stream/``. The reusable core lives in ``ezviz_cloud.py``.

By default it captures **every streamable camera** on the account; pass
``--serial`` to target one. Credentials come from the untracked repo-root ``.env``
(see ``.env.example``) or the environment. 2FA must be OFF (spec §7.1).

Output (capture ``.bin`` + ``.jpg`` per camera) goes to ``scripts/out/``, which is
**wiped at the start of every run** and gitignored.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

try:
    import ezviz_cloud as ez
except ImportError:  # pragma: no cover
    sys.exit(
        "Run this from the repo, e.g. `uv run python scripts/ezviz_stream_probe.py`."
    )


def _send_keepalive(sock: socket.socket, ssn_body: bytes) -> None:
    with contextlib.suppress(OSError):
        sock.sendall(
            struct.pack(
                ">BBHHH", ez.MAGIC, ez.CH_MSG, len(ssn_body), 0, ez.MSG_KEEPALIVE_REQ
            )
            + ssn_body
        )


def stream_segment(dev: dict, token: str, fh, deadline: float) -> dict:
    """Read one VTDU session (until the ~27s drop or the deadline), appending media
    to fh and sending periodic KeepAlive. RTP is de-packetized to Annex-B HEVC;
    PS/TS bodies are written raw for FFmpeg to demux."""
    vtdu, reader, stream_ssn = ez.open_stream(dev, token)
    ka_body = stream_ssn.encode() if stream_ssn else None
    ka_interval = 5.0
    last_ka = time.time()
    state = {"fu": None}  # fresh RTP FU-reassembly state per session
    transport: str | None = None
    out_bytes = packets = nals = 0
    pt_counts: dict[int, int] = {}
    try:
        while time.time() < deadline:
            if ka_body and time.time() - last_ka >= ka_interval:
                _send_keepalive(vtdu, ka_body)
                last_ka = time.time()
            # Wake at least every ka_interval so keep-alive frames keep flowing.
            frame = reader.next_frame(min(deadline, time.time() + ka_interval))
            if frame is None:
                if reader.closed:
                    break
                continue  # slice timeout — loop to send the next keep-alive
            ch, _msg, body = frame
            if ch != ez.CH_STREAM or not body:
                continue
            packets += 1
            if transport is None:
                transport = ez.detect_transport(body)
                ez.log(f"transport={transport}  first8={body[:8].hex(' ')}")
            if transport == "rtp":
                if (body[0] >> 6) == 2 and len(body) >= 2:
                    pt = body[1] & 0x7F
                    pt_counts[pt] = pt_counts.get(pt, 0) + 1
                out = ez.depacketize(body, state)
                if out:
                    fh.write(out)
                    out_bytes += len(out)
                    nals += out.count(ez.SC)
            else:
                fh.write(body)
                out_bytes += len(body)
    finally:
        vtdu.close()
    return {
        "transport": transport,
        "packets": packets,
        "out_bytes": out_bytes,
        "nals": nals,
        "pt_counts": pt_counts,
        "closed": reader.closed,
    }


MIN_JPG_BYTES = 5000  # a real frame is ~100-400KB; smaller = a decode artifact


def _ffmpeg_fmt(transport: str | None) -> str | None:
    """FFmpeg -f for a transport. RTP is a raw HEVC elementary stream (force it);
    mpeg-ps/ts are self-describing containers (force the demuxer so ffmpeg resyncs
    even when a session starts mid-PES). None = let ffmpeg auto-detect."""
    return {"rtp": "hevc", "mpeg-ps": "mpeg", "mpeg-ts": "mpegts"}.get(transport or "")


def extract_jpg(src: Path, jpg: Path, transport: str | None) -> int:
    """Decode the first real frame of src to jpg; return the jpg size (0 = failed)."""
    if not shutil.which("ffmpeg"):
        ez.log("ffmpeg not found; skipping frame extraction")
        return 0
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y"]
    fmt = _ffmpeg_fmt(transport)
    if fmt:
        cmd += ["-f", fmt]
    cmd += ["-i", str(src), "-frames:v", "1", str(jpg)]
    subprocess.run(cmd, capture_output=True, timeout=30, check=False)
    size = jpg.stat().st_size if jpg.exists() else 0
    if size < MIN_JPG_BYTES:
        jpg.unlink(missing_ok=True)
        return 0
    return size


def capture_camera(
    dev: dict,
    label: str,
    auth_addr: str,
    session_id: str,
    out_dir: Path,
    args: argparse.Namespace,
) -> dict:
    """Capture one camera across the ~27s VTDU drops, decoding each session
    *independently* (reconnected sessions restart mid-stream, so they can't be
    spliced into one file). Reconnect until a real frame decodes or the budget is
    spent. Battery/RTP cams usually decode on the first session; IPC cams with a
    long keyframe interval may need several.
    """
    tag = f"{label} {ez.mask_serial(dev['serial'])}"
    result = {"label": label, "transport": None, "packets": 0, "jpg": None, "bytes": 0}
    deadline = time.time() + args.duration
    transport: str | None = None
    seg = 0
    kept_bin = out_dir / f"{label}.bin"  # largest session, kept for inspection
    seg_path = out_dir / f"{label}.seg.bin"
    while time.time() < deadline and seg < args.max_segments:
        seg += 1
        try:
            token = ez.get_vtdu_token(auth_addr, session_id, debug=args.debug)
            with seg_path.open("wb") as fh:
                st = stream_segment(dev, token, fh, deadline)
        except (ez.ApiError, OSError) as exc:
            ez.log(f"[{tag}] session {seg} failed: {exc}")
            time.sleep(2)
            continue
        # Lock the transport to the first clearly-detected container (a reconnect
        # may start mid-PES and mis-detect as "unknown").
        if st["transport"] in ("rtp", "mpeg-ps", "mpeg-ts"):
            transport = transport or st["transport"]
        eff = transport or st["transport"]
        ez.log(
            f"[{tag}] seg{seg}: transport={st['transport']} packets={st['packets']} "
            f"nals={st['nals']} written={st['out_bytes']}B closed={st['closed']}"
        )
        if st["packets"] == 0:
            ez.log(f"[{tag}] no packets (camera waking?) — retrying")
            time.sleep(2)
            continue
        if st["out_bytes"] > result["bytes"]:  # remember the biggest session
            seg_path.replace(kept_bin)
            result["bytes"] = st["out_bytes"]
            src = kept_bin
        else:
            src = seg_path
        size = extract_jpg(src, out_dir / f"{label}.jpg", eff)
        if size:
            if src is not kept_bin:
                src.replace(kept_bin)
            ez.log(f"[{tag}] decoded {label}.jpg ({size}B) after {seg} session(s) ✔")
            result.update(transport=eff, packets=st["packets"], jpg=f"{label}.jpg")
            return result
        if st["closed"] and time.time() < deadline:
            ez.log(f"[{tag}] VTDU drop, no keyframe yet; reconnecting…")
    seg_path.unlink(missing_ok=True)
    ez.log(f"[{tag}] no decodable frame after {seg} session(s) (transport={transport})")
    result.update(transport=transport)
    return result


# Opcodes we already understand — excluded from the I-frame opcode sweep.
KNOWN_OPCODES = {0x132, 0x133, 0x135, ez.MSG_STREAMINFO_REQ, ez.MSG_STREAMINFO_RSP}


H264_SPS = b"\x00\x00\x01\x67"  # SPS NAL (nal_ref_idc=3, type=7)
H264_IDR = b"\x00\x00\x01\x65"  # IDR slice NAL (nal_ref_idc=3, type=5)


def _probe_one_opcode(
    dev: dict, token: str, op: int | None, out_dir: Path, args: argparse.Namespace
) -> dict:
    """Open a fresh session, send candidate opcode `op` (None = control) ~1.5s in,
    then keep reading. Measure H.264 SPS/IDR markers arriving *after* the send vs
    before — a forced keyframe shows up as IDRs appearing only after the opcode.
    """
    vtdu, reader, ssn = ez.open_stream(dev, token)
    body = b"" if args.probe_body == "empty" else (ssn.encode() if ssn else b"")
    seg = out_dir / "probe.seg.bin"
    state = {"fu": None}
    transport: str | None = None
    t0 = time.time()
    last_ka = t0
    deadline = t0 + args.probe_window
    sent = op is None  # control run "sends" nothing
    written = 0
    send_offset = 0
    try:
        with seg.open("wb") as fh:
            while time.time() < deadline:
                if ssn and time.time() - last_ka >= 5:
                    _send_keepalive(vtdu, ssn.encode())
                    last_ka = time.time()
                if not sent and time.time() - t0 >= 1.5:  # let the stream settle first
                    with contextlib.suppress(OSError):
                        vtdu.sendall(
                            struct.pack(">BBHHH", ez.MAGIC, ez.CH_MSG, len(body), 0, op)
                            + body
                        )
                    sent = True
                    send_offset = written
                frame = reader.next_frame(min(deadline, time.time() + 2))
                if frame is None:
                    if reader.closed:
                        break
                    continue
                ch, _msg, pkt = frame
                if ch != ez.CH_STREAM or not pkt:
                    continue
                if transport is None:
                    transport = ez.detect_transport(pkt)
                out = ez.depacketize(pkt, state) if transport == "rtp" else pkt
                if out:
                    fh.write(out)
                    written += len(out)
    finally:
        vtdu.close()
    data = seg.read_bytes()
    tail = data[send_offset:]
    return {
        "transport": transport,
        "idr_before": data[:send_offset].count(H264_IDR),
        "idr_after": tail.count(H264_IDR),
        "sps_after": tail.count(H264_SPS),
        "jpg_size": extract_jpg(seg, out_dir / "probe.jpg", transport),
    }


def probe_iframe(
    dev: dict, auth_addr: str, session_id: str, out_dir: Path, args: argparse.Namespace
) -> None:
    """Sweep candidate opcodes to find one that forces a keyframe on an IPC/PS cam.
    A control(none) baseline runs first so IDRs-after-send are attributable, not luck.
    """
    tag = ez.mask_serial(dev["serial"])
    candidates: list[int | None] = [None] + [
        op for op in range(0x130, 0x146) if op not in KNOWN_OPCODES
    ]
    ez.log(
        f"[probe {tag}] sweeping {len(candidates) - 1} opcodes "
        f"(body={args.probe_body}); watching for IDRs after the send"
    )
    control_idr = 0
    hits: list[str] = []
    for op in candidates:
        name = "control(none)" if op is None else f"0x{op:03x}"
        try:
            token = ez.get_vtdu_token(auth_addr, session_id, debug=args.debug)
            r = _probe_one_opcode(dev, token, op, out_dir, args)
        except (ez.ApiError, OSError) as exc:
            ez.log(f"[probe {tag}] {name}: session failed ({exc})")
            time.sleep(1)
            continue
        ez.log(
            f"[probe {tag}] {name} ({r['transport']}): "
            f"IDR before/after={r['idr_before']}/{r['idr_after']} "
            f"SPS_after={r['sps_after']} jpg={r['jpg_size']}B"
        )
        if op is None:
            control_idr = r["idr_after"]
        # A hit: a keyframe appears after the send that the control didn't show.
        elif r["idr_after"] > control_idr or r["jpg_size"]:
            hits.append(name)
            if r["jpg_size"]:
                (out_dir / "probe.jpg").replace(out_dir / f"hit_{name}.jpg")
        time.sleep(1)
    (out_dir / "probe.seg.bin").unlink(missing_ok=True)
    if hits:
        ez.log(f"[probe {tag}] candidate I-frame opcode(s): {hits}  (see {out_dir})")
    else:
        ez.log(
            f"[probe {tag}] no opcode produced an IDR "
            f"(control IDR-after={control_idr}); try --probe-body empty / wider range"
        )


def _sample_transport(dev: dict, token: str, timeout: float = 6.0) -> str | None:
    """Open a brief session just to detect the camera's transport, then close."""
    vtdu, reader, _ssn = ez.open_stream(dev, token)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            frame = reader.next_frame(min(deadline, time.time() + 2))
            if frame is None:
                if reader.closed:
                    break
                continue
            ch, _msg, body = frame
            if ch == ez.CH_STREAM and body:
                return ez.detect_transport(body)
    finally:
        vtdu.close()
    return None


def _select_ps_camera(
    targets: list[dict], auth_addr: str, session_id: str, args: argparse.Namespace
) -> dict | None:
    """Pick the camera to probe: an explicit --serial, else the first MPEG-PS cam."""
    if args.serial:
        return targets[0]  # main() already filtered to the requested serial
    for dev in targets:
        try:
            token = ez.get_vtdu_token(auth_addr, session_id, debug=args.debug)
            transport = _sample_transport(dev, token)
        except (ez.ApiError, OSError) as exc:
            ez.log(f"sample {ez.mask_serial(dev['serial'])} failed: {exc}")
            continue
        ez.log(f"sample {ez.mask_serial(dev['serial'])}: transport={transport}")
        if transport in ("mpeg-ps", "mpeg-ts"):
            return dev
    return None


def _reset_out_dir(out_dir: Path) -> None:
    """Wipe the output directory so each run starts clean."""
    if out_dir.exists():
        for child in out_dir.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink()
            else:
                shutil.rmtree(child)
    out_dir.mkdir(parents=True, exist_ok=True)


def main() -> int:
    here = Path(__file__).resolve().parent
    ez.load_env(here.parent / ".env")

    ap = argparse.ArgumentParser(description="EZVIZ cloud live-stream diagnostic tool")
    ap.add_argument("--email", default=os.environ.get("EZVIZ_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("EZVIZ_PASSWORD"))
    ap.add_argument("--region", default=os.environ.get("EZVIZ_REGION", "Europe"))
    ap.add_argument(
        "--serial", help="capture only this camera (default: all streamable)"
    )
    ap.add_argument(
        "--duration",
        type=float,
        default=90.0,
        help="wall-clock budget per cam (spans reconnects for a keyframe)",
    )
    ap.add_argument(
        "--max-segments",
        type=int,
        default=6,
        help="max VTDU (re)connections per cam within the budget",
    )
    ap.add_argument(
        "--probe-iframe",
        action="store_true",
        help="sweep opcodes to find an I-frame request (targets an MPEG-PS cam)",
    )
    ap.add_argument(
        "--probe-window", type=float, default=10.0, help="seconds to observe per opcode"
    )
    ap.add_argument(
        "--probe-body",
        choices=("ssn", "empty"),
        default="ssn",
        help="body to send with each candidate opcode",
    )
    ap.add_argument(
        "--debug", action="store_true", help="dump (redacted) API responses"
    )
    args = ap.parse_args()

    missing = [n for n in ("email", "password") if not getattr(args, n)]
    if missing:
        ez.log(f"missing required creds: {', '.join(missing)}")
        ez.log(
            "Fill EZVIZ_EMAIL / EZVIZ_PASSWORD in the repo-root .env, or pass flags."
        )
        return 2

    out_dir = here / "out"
    _reset_out_dir(out_dir)

    try:
        session_id, host = ez.login(
            args.email, args.password, args.region, debug=args.debug
        )
        auth_addr = ez.get_auth_addr(host, session_id, debug=args.debug)
        devices = ez.discover_devices(host, session_id, debug=args.debug)
    except ez.ApiError as exc:
        ez.log(f"CONTROL-PLANE ERROR: {exc}")
        return 1

    streamable = [d for d in devices if d["streamable"]]
    if args.serial:
        targets = [d for d in streamable if d["serial"] == args.serial]
        if not targets:
            seen = ", ".join(ez.mask_serial(d["serial"]) for d in streamable) or "none"
            ez.log(f"serial not found among streamable cameras (have: {seen})")
            return 1
    else:
        targets = streamable
    if not targets:
        ez.log("no streamable cameras to capture")
        return 1

    if args.probe_iframe:
        target = _select_ps_camera(targets, auth_addr, session_id, args)
        if target is None:
            ez.log("no MPEG-PS camera found to probe")
            return 1
        probe_iframe(target, auth_addr, session_id, out_dir, args)
        return 0

    results = [
        capture_camera(dev, f"cam{i:02d}", auth_addr, session_id, out_dir, args)
        for i, dev in enumerate(targets, 1)
    ]

    ok = [r for r in results if r["jpg"]]
    ez.log(f"done: {len(ok)}/{len(results)} camera(s) produced a frame -> {out_dir}")
    for r in results:
        state = f"jpg={r['jpg']}" if r["jpg"] else "no frame"
        ez.log(
            f"  {r['label']}: transport={r['transport']} packets={r['packets']} {state}"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
