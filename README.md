# EZVIZ Stream

A Home Assistant custom integration (`ezviz_stream`) that provides a live video
stream from EZVIZ **battery** cameras via the EZVIZ **cloud** — for models that
have no local RTSP.

> **⚠️ Work in progress — not yet functional.** The design is settled and the core
> decode logic is proven, but the integration does not yet stream. See
> [`doc/TODO.md`](./doc/TODO.md) for what is done and what is next.

## Why this exists

EZVIZ's battery cameras (e.g. the HP2/HP7 doorbells and similar) don't expose a
local RTSP feed, so the official Home Assistant `ezviz` integration — which is
local-RTSP only — can't show their live view. This integration reaches the same
cameras the way the EZVIZ app does: over the cloud. See
[`doc/specification.md` §1](./doc/specification.md) for the full rationale.

## How it works

The proven pipeline (design in [`doc/specification.md`](./doc/specification.md)):

```text
EZVIZ cloud login → device list → VTDU tokens        # control plane (auth)
  → VTM/VTDU binary handshake (ysproto://)           # obtain a media socket
  → channel-0x01 RTP packets → RTP/RFC-7798 depacketize → Annex-B HEVC
  → FFmpeg (default: HEVC→H.264 transcode) → HA camera
```

Key decisions:

- **Auth** is delegated to [`RenierM26/pyEzvizApi`](https://github.com/RenierM26/pyEzvizApi)
  (login / device list / tokens); only the VTM/VTDU socket handshake is
  implemented here.
- **De-packetizer** — the RTP→HEVC logic ([spec §4.1](./doc/specification.md)) is
  the core contribution and is ported verbatim from proven code.
- **Codec** — defaults to on-demand HEVC→H.264 transcode (works in all browsers);
  native HEVC is available as a config option (Safari/iOS).
- **Serving** — go2rtc `exec:` source, for on-demand start/stop and fan-out.
- **Battery-friendly** — streams *only* while a client is watching, never 24/7,
  with cam-wake retry and a reconnect loop for the periodic VTDU drop.

## Requirements

- Home Assistant **2024.12.0** or newer.
- An EZVIZ cloud account with **two-step verification disabled** (same constraint
  as the official `ezviz` integration for v1 — see
  [spec §7.1](./doc/specification.md)).
- `ffmpeg` available to Home Assistant (bundled with the standard HA install).

## Installation

> Not usable yet — these steps are the intended flow once the integration
> functions. Installing it today will not produce a working camera.

Via [HACS](https://hacs.xyz) as a custom repository:

1. HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add this repository's URL with category **Integration**.
3. Install **EZVIZ Stream** and restart Home Assistant.
4. Add the integration from **Settings → Devices & Services** and enter your
   EZVIZ credentials.

## Development

The repo ships a VS Code devcontainer with everything preinstalled.

```bash
.devcontainer/scripts/setup    # Bootstrap the container (runs on create)
.devcontainer/scripts/develop  # Start a local Home Assistant on port 8123
.devcontainer/scripts/clean    # Reset the Home Assistant config directory
.devcontainer/scripts/lint     # ruff format + ruff check --fix
```

Dependencies are managed with [uv](https://docs.astral.sh/uv/):

```bash
uv sync                  # Install/refresh the .venv from pyproject.toml + uv.lock
uv add --dev <pkg>       # Add a dev dependency
uv run pytest tests/     # Run the test suite
```

Tests use `pytest-homeassistant-custom-component`, which mocks Home Assistant —
no live HA instance or real EZVIZ cloud calls are needed. **All integration code
lands with tests**, enforced by a `pytest` pre-commit hook. See
[`CLAUDE.md`](./CLAUDE.md) for the full working conventions.

## Documentation

- [`doc/specification.md`](./doc/specification.md) — authoritative design and the
  proven decode pipeline.
- [`doc/TODO.md`](./doc/TODO.md) — official todo list / build milestones.
- [`doc/reference.md`](./doc/reference.md) — EZVIZ API and streaming reference notes.

## Credits

Built on the work of others who reverse-engineered the EZVIZ cloud protocol:

- [`RenierM26/pyEzvizApi`](https://github.com/RenierM26/pyEzvizApi) — cloud auth / API.
- [`RenierM26/ha-ezviz`](https://github.com/RenierM26/ha-ezviz) — the official
  HACS EZVIZ integration (local-RTSP only).
- [`ESJavadex/ezviz-ha-addon`](https://github.com/ESJavadex/ezviz-ha-addon) —
  reverse-engineered cloud connection + VTM/VTDU handshake.
- [`LethalEthan/LE-EZVIZ-VS`](https://github.com/LethalEthan/LE-EZVIZ-VS) —
  protocol / encryption / codec notes.

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## License

[MIT](./LICENSE).
