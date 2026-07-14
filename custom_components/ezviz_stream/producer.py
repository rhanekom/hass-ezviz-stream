"""
Standalone diagnostic - stream a camera's Annex-B HEVC to stdout.

Run manually as ``python -m custom_components.ezviz_stream.producer
--creds-file <path>`` with a JSON creds file (username/password/region/serial/
stream) to verify the live cloud path outside HA (e.g. ``... | ffplay -``). This is
a debugging tool only: the integration streams in-process via :mod:`broadcast` and
serves MPEG-TS over HTTP (:mod:`stream_view`) - go2rtc rejects ``exec:`` sources via
its API, so it is never run by go2rtc. RTP/HEVC (battery cams) only; encrypted
MPEG-PS (IPC) needs continuous decryption + remux (C.2b).
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
