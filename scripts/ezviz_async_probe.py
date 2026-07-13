#!/usr/bin/env python3
"""Live-verify the integration's async streaming client against real cameras.

This is the async counterpart to ``ezviz_stream_probe.py`` (which drives the sync
core). It exercises the *actual integration* code -
``custom_components/ezviz_stream/{api,stream,decrypt,ysproto}.py`` - end to end:
login -> discovery -> VTM/VTDU handshake -> media -> depacketize/decrypt -> FFmpeg,
saving a decoded JPEG to ``scripts/out/``. CI cannot reach the EZVIZ cloud, so this
is how the socket path is verified (as the sync probe verified the core).

Credentials come from the repo-root ``.env`` (``EZVIZ_EMAIL`` / ``EZVIZ_PASSWORD`` /
``EZVIZ_REGION`` / ``EZVIZ_VERIFY_CODE``). 2FA must be OFF (spec 7.1).

    uv run python scripts/ezviz_async_probe.py                    # first streamable cam
    uv run python scripts/ezviz_async_probe.py --serial <SN> --stream 2
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import aiohttp  # noqa: E402
import ezviz_cloud as ez  # noqa: E402  (scripts core: .env loader + serial masking)

from custom_components.ezviz_stream.api import EzvizCloudApi  # noqa: E402
from custom_components.ezviz_stream.stream import grab_jpeg  # noqa: E402


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
        if args.serial:
            cameras = [c for c in cameras if c.serial == args.serial]
        if not cameras:
            ez.log("no matching streamable camera")
            return 1
        camera = cameras[0]
        ez.log(f"streaming {ez.mask_serial(camera.serial)} (stream={args.stream})")

        jpeg = await grab_jpeg(
            camera,
            api.async_get_vtdu_token,
            "ffmpeg",
            stream=args.stream,
            verification_code=code,
            duration=args.duration,
        )

    if not jpeg:
        ez.log("no frame decoded within the budget")
        return 1
    out_dir = _REPO / "scripts" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"async_{ez.mask_serial(camera.serial)}.jpg"
    path.write_bytes(jpeg)
    ez.log(f"decoded {len(jpeg)} B -> {path}")
    return 0


def main() -> int:
    here = Path(__file__).resolve().parent
    ez.load_env(here.parent / ".env")
    ap = argparse.ArgumentParser(description="Live-verify the async streaming client")
    ap.add_argument("--serial", help="camera serial (default: first streamable)")
    ap.add_argument("--stream", type=int, choices=(1, 2), default=1)
    ap.add_argument("--duration", type=float, default=60.0)
    return asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
