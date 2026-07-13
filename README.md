# EZVIZ Stream

A Home Assistant custom integration (`ezviz_stream`) that streams EZVIZ cameras
live via the EZVIZ **cloud** - including **battery** cameras that expose no local
RTSP, and cameras with **Image Encryption** enabled.

> **⚠️ Work in progress - not yet functional.** The design is settled and the core
> decode logic is proven, but the integration does not yet stream. See
> [`doc/TODO.md`](./doc/TODO.md) for what is done and what is next.

## Why this exists

The official Home Assistant `ezviz` integration shows live view over the camera's
**local RTSP** stream. That leaves gaps: battery cameras (e.g. HP2/HP7 doorbells)
run no persistent RTSP server, and any camera unreachable on Home Assistant's LAN
can't be viewed. This integration reaches the cameras the way the EZVIZ app does,
over the **cloud**, and decrypts the video for cameras with Image Encryption on.
See [`doc/specification.md` §1](./doc/specification.md) for the full rationale.

## How it works

The proven pipeline (design in [`doc/specification.md`](./doc/specification.md)):

```text
EZVIZ cloud login → device list → VTDU tokens        # control plane (auth)
  → VTM/VTDU binary handshake (ysproto://)           # obtain a media socket
  → channel-0x01 media, transport auto-detected:
      - RTP / RFC-7798  → Annex-B HEVC                  (battery cams)
      - MPEG-PS → H.264, AES-decrypted if encrypted     (IPC cams)
  → FFmpeg (default: → H.264 transcode) → go2rtc → HA camera
```

Key decisions:

- **Auth + handshake are hand-rolled** - the integration takes **no runtime
  dependency on `pyezvizapi`** (Home Assistant core pins an incompatible version of
  it for the official integration; a shared environment can't satisfy both). See
  [spec §8](./doc/specification.md).
- **De-packetizer + decryption are the core contribution** - the RTP→HEVC logic
  ([spec §4.1](./doc/specification.md)) and a hand-rolled AES-ECB Image-Encryption
  decryptor (validated byte-for-byte against `pyezvizapi`). `pyezvizapi` is kept
  only as a **dev-only decryption test oracle**.
- **Codec** - defaults to on-demand →H.264 transcode (works in all browsers);
  native HEVC is available as a config option (Safari/iOS).
- **Serving** - go2rtc `exec:` source, for on-demand start/stop and fan-out.
- **Battery-friendly** - streams *only* while a client is watching, never 24/7,
  with cam-wake retry and a reconnect loop for the periodic VTDU drop.

## Requirements

- Home Assistant **2026.3.0** or newer (the release that moved to Python 3.14,
  which this integration targets).
- An EZVIZ cloud account with **two-step verification disabled** (same constraint
  as the official `ezviz` integration for v1 - see
  [spec §7.1](./doc/specification.md)).
- For any camera with **Image Encryption** enabled, its **verification code** (the
  6-character code on the camera label), entered per camera during setup.
- `ffmpeg` available to Home Assistant (bundled with the standard HA install).

## Installation

> Not usable yet - these steps are the intended flow once the integration
> functions. Installing it today will not produce a working camera.

Via [HACS](https://hacs.xyz) as a custom repository:

1. HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add this repository's URL with category **Integration**.
3. Install **EZVIZ Stream** and restart Home Assistant.
4. Add the integration from **Settings → Devices & Services**. Setup is two steps:
   first your EZVIZ **account** (email, password, region); then **select the
   cameras** to add and supply the **verification code** for any encrypted ones.

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

Tests use `pytest-homeassistant-custom-component`, which mocks Home Assistant, so
no live HA instance or real EZVIZ cloud calls are needed. **All integration code
lands with tests**, enforced by a `pytest` pre-commit hook. See
[`CLAUDE.md`](./CLAUDE.md) for the full working conventions.

## Documentation

- [`doc/specification.md`](./doc/specification.md) - authoritative design and the
  proven decode pipeline.
- [`doc/TODO.md`](./doc/TODO.md) - official todo list / build milestones.
- [`doc/reference.md`](./doc/reference.md) - EZVIZ API and streaming reference notes.

## Credits

Built on the work of others who reverse-engineered the EZVIZ cloud protocol:

- [`RenierM26/pyEzvizApi`](https://github.com/RenierM26/pyEzvizApi) - cloud protocol
  reference; the decryption algorithm derives from it (Apache-2.0), and it serves as
  a dev-only decryption test oracle (no runtime dependency).
- [`RenierM26/ha-ezviz`](https://github.com/RenierM26/ha-ezviz) - the official
  HACS EZVIZ integration (local-RTSP only).
- [`ESJavadex/ezviz-ha-addon`](https://github.com/ESJavadex/ezviz-ha-addon) -
  reverse-engineered cloud connection + VTM/VTDU handshake.
- [`LethalEthan/LE-EZVIZ-VS`](https://github.com/LethalEthan/LE-EZVIZ-VS) -
  protocol / encryption / codec notes.

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## License

[Apache-2.0](./LICENSE) (see also [`NOTICE`](./NOTICE)).
