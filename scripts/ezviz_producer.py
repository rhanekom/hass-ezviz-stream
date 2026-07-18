#!/usr/bin/env python3
"""
Standalone diagnostic - stream a camera's Annex-B HEVC to stdout.

Verifies the live cloud path outside Home Assistant (e.g. pipe into ffplay). This
is a debugging tool only: the integration streams in-process via
``custom_components.ezviz_stream.broadcast`` and serves MPEG-TS over HTTP
(``stream_view``) - go2rtc rejects ``exec:`` sources via its API, so it is never
run by go2rtc. RTP/HEVC (battery cams) only; encrypted MPEG-PS (IPC) needs
continuous decryption + remux.

Credentials come from a JSON file (``username`` / ``password`` / ``region`` /
``serial`` / optional ``stream``):

    uv run python scripts/ezviz_producer.py --creds-file <path> | ffplay -
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import aiohttp

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from custom_components.ezviz_stream.api import EzvizCloudApi  # noqa: E402
from custom_components.ezviz_stream.stream import stream_annexb  # noqa: E402


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
    parser = argparse.ArgumentParser(description="EZVIZ Stream Annex-B producer")
    parser.add_argument("--creds-file", required=True)
    args = parser.parse_args()
    creds = json.loads(Path(args.creds_file).read_text())
    try:
        return asyncio.run(_run(creds))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
