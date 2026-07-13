"""
go2rtc ``exec:`` producer - stream a camera's Annex-B HEVC to stdout.

Run by go2rtc as ``python -m custom_components.ezviz_stream.producer
--creds-file <path>``. Reads a JSON creds file (mode 600, written by the
integration - never creds on the command line) with username/password/region/
serial/stream, logs in, and writes the camera's Annex-B HEVC bitstream to stdout,
which go2rtc ingests directly (spec §6). RTP/HEVC (battery cams) only for now;
encrypted MPEG-PS (IPC) needs continuous decryption + remux (C.2b).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import aiohttp

from .api import EzvizCloudApi
from .stream import stream_annexb


async def _run(creds: dict[str, Any]) -> int:
    async with aiohttp.ClientSession() as session:
        api = EzvizCloudApi(session)
        await api.async_login(creds["username"], creds["password"], creds["region"])
        camera = next(
            (
                cam
                for cam in await api.async_get_cameras()
                if cam.serial == creds["serial"]
            ),
            None,
        )
        if camera is None:
            sys.stderr.write(f"camera {creds['serial']} not found on the account\n")
            return 1
        await stream_annexb(
            camera,
            api.async_get_vtdu_token,
            sys.stdout.buffer,
            stream=int(creds.get("stream", 1)),
        )
    return 0


def main() -> int:
    """Parse args and run the producer until stopped."""
    parser = argparse.ArgumentParser(description="EZVIZ Stream go2rtc producer")
    parser.add_argument("--creds-file", required=True)
    args = parser.parse_args()
    creds = json.loads(Path(args.creds_file).read_text())
    try:
        return asyncio.run(_run(creds))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
