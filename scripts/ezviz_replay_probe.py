#!/usr/bin/env python3
"""Live probe: list a camera's cloud recordings and play one back (decrypted).

Exercises the real integration code end to end against a live account:
``api.EzvizCloudApi`` (login -> list cloud clips -> playback ticket) and
``cloud_replay.iter_cloud_replay_ps`` (the TLS replay socket + on-the-fly decrypt).

Credentials come from the repo-root ``.env`` (EZVIZ_EMAIL / EZVIZ_PASSWORD /
EZVIZ_REGION / EZVIZ_VERIFY_CODE); 2FA must be OFF. Secrets and device serials are
never printed in full. With ``--out FILE`` the decrypted MPEG-PS is written and, if
ffprobe is available, validated as decodable.

    uv run python scripts/ezviz_replay_probe.py [--serial SN] [--out FILE]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # ezviz_cloud (env loader + masking helpers)
sys.path.insert(0, str(HERE.parent))  # custom_components package

import ezviz_cloud as ez  # noqa: E402

from custom_components.ezviz_stream.api import EzvizCloudApi  # noqa: E402
from custom_components.ezviz_stream.cloud_replay import (  # noqa: E402
    iter_cloud_replay_ps,
)

_PS_PACK_HEADER = bytes.fromhex("000001ba")  # MPEG-PS pack start code


async def _run(args: argparse.Namespace) -> int:
    email = os.environ.get("EZVIZ_EMAIL")
    password = os.environ.get("EZVIZ_PASSWORD")
    region = os.environ.get("EZVIZ_REGION", "Europe")
    code = os.environ.get("EZVIZ_VERIFY_CODE", "")
    if not email or not password:
        ez.log("missing EZVIZ_EMAIL / EZVIZ_PASSWORD (set them in .env)")
        return 2

    async with aiohttp.ClientSession() as session:
        api = EzvizCloudApi(session)
        await api.async_login(email, password, region)
        cameras = await api.async_get_cameras()
        ez.log(f"logged in; {len(cameras)} streamable camera(s)")
        if not cameras:
            return 1

        cam = next(
            (c for c in cameras if c.serial == args.serial),
            None if args.serial else cameras[0],
        )
        if cam is None:
            ez.log(f"serial {ez.mask_serial(args.serial)} not found")
            return 1
        ez.log(
            f"camera {ez.mask_serial(cam.serial)} ch={cam.channel} "
            f"battery={cam.is_battery} encrypted={cam.is_encrypted}"
        )

        videos = await api.async_get_cloud_videos(
            cam.serial, cam.channel, limit=args.limit
        )
        ez.log(f"{len(videos)} cloud clip(s) returned")
        for v in videos[:5]:
            ez.log(
                f"  clip {ez.redact(v.seq_id, keep=4)} begin={v.begin_cas} "
                f"dur={v.video_long}ms size={v.file_size} crypt={v.crypt} "
                f"stream_url={'yes' if v.stream_url else 'no'}"
            )
        rec = next(
            (v for v in videos if v.stream_url and v.begin_cas and v.end_cas), None
        )
        if rec is None:
            ez.log(
                "no clip with a stream_url + time range (direct-download path needed)"
            )
            return 1

        ticket = await api.async_get_camera_ticket(cam.serial, cam.channel)
        ez.log(
            f"ticket {ez.redact(ticket)}; playing clip {ez.redact(rec.seq_id, keep=4)}"
        )

        assert rec.begin_cas is not None  # noqa: S101 - guarded by the next() filter
        assert rec.end_cas is not None  # noqa: S101
        total = 0
        chunks = 0
        head = b""
        buf = bytearray()
        async for chunk in iter_cloud_replay_ps(
            stream_url=rec.stream_url,
            ticket=ticket,
            serial=cam.serial,
            channel=cam.channel,
            seq_id=rec.seq_id,
            begin_cas=rec.begin_cas,
            end_cas=rec.end_cas,
            storage_version=rec.storage_version,
            verification_code=code if rec.crypt else "",
            file_size=rec.file_size,
        ):
            total += len(chunk)
            chunks += 1
            if not head:
                head = chunk[:16]
            if args.out:
                buf += chunk
            if total >= args.max_bytes:
                break

        ez.log(f"decrypted {total} bytes in {chunks} chunk(s); head={head.hex()}")
        if head[:4] == _PS_PACK_HEADER:
            ez.log("head is a valid MPEG-PS pack start code -> decrypt looks correct")
        else:
            ez.log("head is NOT a PS pack start code -> wrong key or protocol mismatch")

        if args.out and buf:
            out = Path(args.out)
            out.write_bytes(bytes(buf))  # noqa: ASYNC240 - probe writes its capture
            ez.log(f"wrote {len(buf)} bytes to {out}")
            _ffprobe(out)
        return 0


def _ffprobe(path: Path) -> None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        ez.log("ffprobe not found; skipping decode validation")
        return
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=format_name,duration",
            "-of",
            "default=nw=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = result.stdout.strip() or result.stderr.strip()
    ez.log(f"ffprobe rc={result.returncode}: {detail}")


def main() -> int:
    ez.load_env(HERE.parent / ".env")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--serial", default=None, help="camera serial (default: first)")
    ap.add_argument("--limit", type=int, default=20, help="cloud clips to list")
    ap.add_argument(
        "--max-bytes", type=int, default=2_000_000, help="stop after N decrypted bytes"
    )
    ap.add_argument(
        "--out", default=None, help="write decrypted MPEG-PS here + ffprobe it"
    )
    return asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
