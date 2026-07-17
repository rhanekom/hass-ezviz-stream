#!/usr/bin/env python3
"""Live probe: play back an SD-card segment over the LAN ysproto /playback path.

Validates the SD-playback transport end to end against a real account:
`stream.iter_playback_ps` opening a time-ranged `/playback` ysproto session and
depacketising channel-0x01 to MPEG-PS. Credentials come from the repo-root `.env`.

    uv run python scripts/ezviz_sd_probe.py [--name Backyard] [--back 120] [--dur 30]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import ezviz_cloud as ez  # noqa: E402

from custom_components.ezviz_stream import api as api_mod  # noqa: E402
from custom_components.ezviz_stream import stream as stream_mod  # noqa: E402
from custom_components.ezviz_stream.const import SUB_STREAM  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    email = os.environ.get("EZVIZ_EMAIL")
    password = os.environ.get("EZVIZ_PASSWORD")
    region = os.environ.get("EZVIZ_REGION", "Europe")
    code = os.environ.get("EZVIZ_VERIFY_CODE", "")
    if not email or not password:
        ez.log("missing EZVIZ_EMAIL / EZVIZ_PASSWORD (set them in .env)")
        return 2

    async with aiohttp.ClientSession() as session:
        api = api_mod.EzvizCloudApi(session)
        await api.async_login(email, password, region)
        cameras = await api.async_get_cameras()
        ez.log(f"{len(cameras)} camera(s): " + ", ".join(c.label for c in cameras))
        cam = next(
            (c for c in cameras if args.name.lower() in c.name.lower()),
            cameras[0] if cameras else None,
        )
        if cam is None:
            ez.log("no camera found")
            return 1
        ez.log(
            f"camera {ez.mask_serial(cam.serial)} '{cam.name}' ch={cam.channel} "
            f"encrypted={cam.is_encrypted} vtm={cam.vtm_ip}:{cam.vtm_port}"
        )

        if args.begin and args.end:
            ez.log(f"explicit range {args.begin} .. {args.end} (sub stream)")
            await _play(api, cam, args.begin, args.end, code, args)
            return 0

        now_ms = int(time.time() * 1000)
        start_ms = now_ms - int(args.hours * 3600_000)
        # Extend the end 1 h past "now": camera clocks can run ahead, timestamping
        # just-recorded footage in the near future (observed ~15 min ahead).
        records = await api.async_search_records(
            cam.serial,
            cam.channel,
            start_millis=start_ms,
            stop_millis=now_ms + 3600_000,
        )
        ez.log(f"{len(records)} SD segment(s) in the last {args.hours}h")
        if not records:
            return 1
        for r in records[:3]:
            ez.log(f"  segment {r.begin_cas} .. {r.end_cas} ({r.duration_ms}ms)")
        seg = records[-1]  # most recent
        begin = seg.begin_cas
        end = api_mod._cas_time(  # noqa: SLF001 - probe reuses the CAS formatter
            min(seg.end_millis, seg.begin_millis + args.dur * 1000)
        )
        ez.log(f"playing {begin} .. {end} (sub stream)")
        await _play(api, cam, begin, end, code, args)
        return 0


async def _play(
    api: object, cam: object, begin: str, end: str, code: str, args: argparse.Namespace
) -> None:
    total = chunks = 0
    head = b""
    buf = bytearray()
    async for chunk in stream_mod.iter_playback_ps(
        cam,
        api.async_get_vtdu_token,
        stream=SUB_STREAM,
        verification_code=code if cam.is_encrypted else "",
        begin_cas=begin,
        end_cas=end,
    ):
        total += len(chunk)
        chunks += 1
        if not head:
            head = chunk[:16]
        buf += chunk
        if total >= args.max_bytes:
            break

    ez.log(f"got {total} bytes in {chunks} chunk(s); head={head.hex()}")
    if not total:
        ez.log("no media - transport failed (check window has SD footage)")
        return
    if head[:4] == bytes.fromhex("000001ba"):
        ez.log("MPEG-PS pack start present -> playback transport works")
    out = Path("scripts/out/sd_playback.ps")
    out.write_bytes(bytes(buf))  # noqa: ASYNC240 - probe writes its capture
    ez.log(f"wrote {len(buf)} bytes to {out}")
    if shutil.which("ffprobe"):
        r = subprocess.run(  # noqa: ASYNC221 - probe validation, blocking is fine
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,codec_type",
                "-of",
                "default=nw=1",
                str(out),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        ez.log(f"ffprobe: {r.stdout.strip() or r.stderr.strip()}")


def main() -> int:
    import logging  # noqa: PLC0415

    ez.load_env(HERE.parent / ".env")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", default="Backyard", help="camera name substring")
    ap.add_argument(
        "--hours", type=float, default=6.0, help="search window (hours back)"
    )
    ap.add_argument(
        "--dur", type=int, default=30, help="segment length to play (seconds)"
    )
    ap.add_argument("--max-bytes", type=int, default=1_500_000)
    ap.add_argument(
        "--begin", help="explicit begin CAS (skip search), e.g. 20260717T103548Z"
    )
    ap.add_argument("--end", help="explicit end CAS")
    ap.add_argument("--debug", action="store_true", help="show stream handshake logs")
    args = ap.parse_args()
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
