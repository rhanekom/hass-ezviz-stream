#!/usr/bin/env python3
"""List the cameras linked to an EZVIZ account.

Shows the details relevant to streaming (VTM node, channel, category, online
status, streamability).

Shares the control-plane core with ``ezviz_stream_probe.py`` (``ezviz_cloud.py``).
Credentials come from the untracked repo-root ``.env`` (see ``.env.example``) or
the environment; 2FA must be OFF (spec §7.1). Serials are masked in output - pass
``--full-serials`` to print them in full (they are sensitive; avoid sharing).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import ezviz_cloud as ez
except ImportError:  # pragma: no cover
    sys.exit(
        "Run this from the repo, e.g. `uv run python scripts/ezviz_list_cameras.py`."
    )


def main() -> int:
    here = Path(__file__).resolve().parent
    ez.load_env(here.parent / ".env")

    ap = argparse.ArgumentParser(description="List EZVIZ cameras linked to the account")
    ap.add_argument("--email", default=os.environ.get("EZVIZ_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("EZVIZ_PASSWORD"))
    ap.add_argument("--region", default=os.environ.get("EZVIZ_REGION", "Europe"))
    ap.add_argument(
        "--full-serials", action="store_true", help="print serials unmasked (sensitive)"
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

    try:
        session_id, host = ez.login(
            args.email, args.password, args.region, debug=args.debug
        )
        devices = ez.discover_devices(host, session_id, debug=args.debug)
    except ez.ApiError as exc:
        ez.log(f"CONTROL-PLANE ERROR: {exc}")
        return 1

    if not devices:
        ez.log("no cameras found on this account")
        return 0

    def fmt_serial(s: str) -> str:
        return s if args.full_serials else ez.mask_serial(s)

    online_map = {0: "offline", 1: "online"}
    print(f"\n{len(devices)} camera(s) linked to the account:\n")
    for i, d in enumerate(devices, 1):
        status = online_map.get(d["status"], str(d["status"]))
        vtm = f"{d['vtm_ip']}:{d['vtm_port']}" if d["streamable"] else "-"
        print(f"{i:>2}. {fmt_serial(d['serial'])}  {d['name'] or '(unnamed)'}")
        print(
            f"      status={status}  category={d['category'] or '?'}  "
            f"channel={d['channel']}  streamable={'yes' if d['streamable'] else 'no'}"
        )
        print(f"      VTM={vtm}  biz={'yes' if d['biz'] else 'none'}")
    print()
    if not args.full_serials:
        ez.log("serials masked; pass --full-serials to reveal (sensitive).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
