# EZVIZ Stream

Live-stream EZVIZ cameras in Home Assistant through the EZVIZ **cloud** - including
**battery** cameras that expose no local RTSP, and cameras with **Image Encryption**
enabled.

[![Validate](https://github.com/rhanekom/hass-ezviz-stream/actions/workflows/validate.yml/badge.svg)](https://github.com/rhanekom/hass-ezviz-stream/actions/workflows/validate.yml)
[![Lint](https://github.com/rhanekom/hass-ezviz-stream/actions/workflows/lint.yml/badge.svg)](https://github.com/rhanekom/hass-ezviz-stream/actions/workflows/lint.yml)
[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)

The official Home Assistant `ezviz` integration only shows live view over a camera's
**local RTSP** stream. That leaves gaps: battery cameras run no persistent RTSP
server, and any camera off Home Assistant's LAN can't be viewed. This integration
reaches cameras the way the EZVIZ app does - over the cloud - and decodes the video,
decrypting it for cameras that have Image Encryption switched on.

## Features

| | |
|---|---|
| ☁️ **Cloud live view** | WebRTC in the dashboard via go2rtc, for cameras with no usable local RTSP. |
| 🔋 **Battery cameras** | Streams RTP/HEVC from battery cams, with battery-aware defaults (sub stream + slower thumbnails). |
| 🔒 **Image Encryption** | Decrypts AES-encrypted MPEG-PS video on the fly, using the per-camera verification code. |
| 🖼️ **Snapshots** | On-demand JPEG thumbnails, cached and retained across restarts. |
| 🔌 **On-demand only** | A camera streams **only while someone is watching**, then stops - kind to batteries and to the EZVIZ cloud. |
| 🧩 **One session, fanned out** | A single cloud session per camera is shared by every viewer and the snapshot, so a dashboard never opens duplicate sessions. |
| ⚙️ **Full config flow** | Add / reconfigure cameras and re-authenticate the account from the UI; each save validates by grabbing a real frame. |
| 🏠 **Device-linked** | Camera entities attach to the same device as the official `ezviz` integration when it is installed. |

## Requirements

- **Home Assistant 2025.4.0 or newer** (this integration uses config subentries).
- A Home Assistant build running **Python 3.14** - see [Compatibility](#compatibility).
- An EZVIZ cloud account with **two-step verification disabled** (same constraint as
  the official `ezviz` integration for now).
- For any camera with **Image Encryption** on, its **verification code** - the
  6-character code on the camera's label - entered per camera during setup.
- **`ffmpeg`** and the **go2rtc** integration, both bundled with a standard Home
  Assistant install (go2rtc handles the browser-friendly transcode for live view).

## Installation

### HACS (recommended)

1. In HACS, open the ⋮ menu and choose **Custom repositories**.
2. Add `https://github.com/rhanekom/hass-ezviz-stream` with category **Integration**.
3. Install **EZVIZ Stream** and restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for
   **EZVIZ Stream**.

### Manual

Copy `custom_components/ezviz_stream` into your Home Assistant
`config/custom_components/` directory and restart.

## Configuration

Setup has two levels: one **account**, then a **camera** for each device you want to
stream.

### 1. Add the account

Enter your EZVIZ **email**, **password**, and **region**. The integration signs in to
validate the credentials before creating the account.

### 2. Add a camera

On the account, use **Add camera** (the subentry action):

1. **Pick the camera** from those discovered on your account.
2. **Set its options:**
    - **Verification code** - required only if the camera has Image Encryption on;
      leave blank otherwise.
    - **Advanced** (collapsed):
        - **Slow thumbnail refresh** - poll the still image less often. Defaults on
          for battery cameras.
        - **Video stream** - **Main (HD)** or **Sub (lower-res)**. Defaults to Sub for
          battery cameras (lighter on the battery and on a weak link).

On save, the integration **grabs a real frame** to confirm the code and stream work.
If it can't (a wrong code, or a sleeping / temporarily-unreachable camera), you get a
choice to **try again** or **save anyway** - it never silently accepts settings that
don't stream.

### Changing a camera later

Use the camera's **Reconfigure** action to change its verification code, thumbnail
cadence, or stream. If the account password changes or expires, Home Assistant
prompts you to **re-authenticate** without re-adding anything.

## How it works

```text
EZVIZ cloud:  login  ->  device list  ->  VTDU token
   ->  VTM/VTDU "ysproto" handshake  ->  channel-0x01 media
        - battery cams:  RTP / RFC-7798      ->  Annex-B HEVC
        - IPC cams:      MPEG-PS  (+ AES-ECB decrypt if Image Encryption is on)
   ->  FFmpeg remux  ->  MPEG-TS  ->  local token-guarded HTTP view
   ->  go2rtc pulls it  ->  WebRTC (HEVC transcoded to H.264) in the dashboard
```

- **Hand-rolled auth + handshake.** The integration takes **no runtime dependency on
  `pyezvizapi`** - Home Assistant core pins an incompatible version of it for the
  official integration, and one environment can't satisfy both.
- **Own decryptor.** The RTP-to-HEVC depacketizer and an AES-ECB Image-Encryption
  decryptor (one-shot plus an incremental streaming variant) are the core
  contribution, validated byte-for-byte against `pyezvizapi` (kept only as a dev-only
  test oracle).
- **Serving.** `stream_source()` returns a token-guarded local `http://` MPEG-TS URL
  that go2rtc pulls (it rejects `exec:` sources via its API). One camera is one cloud
  session, fanned out to every viewer, started on first watch and stopped on the last.
- **Smooth playback.** Frames are paced to the camera's own RTP clock, so live view
  tracks the real capture cadence.

## Supported cameras

- **Battery cameras** (RTP/HEVC) - verified end to end.
- **Mains / IPC cameras** (MPEG-PS), including **Image Encryption** - verified end to
  end.

Video is decoded from the cloud stream; the transcode to a browser-friendly codec is
handled by go2rtc.

## Tips and troubleshooting

- **Live view keeps "catching up" / skipping.** This is almost always the network:
  WebRTC keeps latency low and skips rather than lagging when a link is jittery or
  bandwidth-limited. Switch that camera to the **Sub** stream (Reconfigure → Advanced)
  and prefer 5 GHz Wi-Fi or a wired Home Assistant host. To tell local from remote
  apart, compare live view on the same Wi-Fi as HA versus away from home.
- **Battery draining fast.** Live viewing runs off the battery. It streams only while
  watched, but frequent or long viewing still drains it - use the Sub stream and keep
  sessions short.
- **A thumbnail is briefly blank.** Snapshots are cached and the last good frame is
  kept across restarts; a blank usually means a cold start on a battery camera that is
  slow to wake, or Home Assistant rotating the image token. It recovers on the next
  refresh.
- **Two-step verification.** Not supported yet - disable it in the EZVIZ app.
- **Repeated `concurrency/resource limit` warnings in the log.** EZVIZ is refusing
  simultaneous streams; view or snapshot fewer cameras at once.

## Compatibility

This integration is developed against **Python 3.14** and uses 3.14 language features,
so it must run on a Home Assistant build using that Python version. Config subentries
additionally require Home Assistant **2025.4.0+**. Installing on an older Python will
fail to load the integration.

## Limitations and roadmap

- **Two-step verification (2FA)** login is not yet supported.
- **Codec / serving-mode options** (native HEVC passthrough, an MJPEG fallback) are
  planned but not yet exposed.
- Brand assets are pending a `home-assistant/brands` submission before the integration
  can go into the HACS default store.

See [`doc/TODO.md`](./doc/TODO.md) for the current state and what is next.

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
uv run pytest tests/     # Run the test suite
```

Tests use `pytest-homeassistant-custom-component`, which mocks Home Assistant, so no
live HA instance or real EZVIZ cloud calls are needed. All integration code lands with
tests (enforced by a pre-commit hook). See [`CLAUDE.md`](./CLAUDE.md) for the working
conventions.

## Documentation

- [`doc/specification.md`](./doc/specification.md) - authoritative design and the
  proven decode pipeline.
- [`doc/reference.md`](./doc/reference.md) - EZVIZ cloud/streaming reference notes.
- [`doc/TODO.md`](./doc/TODO.md) - shipped features and remaining work.

## Credits

Built on the work of others who reverse-engineered the EZVIZ cloud protocol:

- [`RenierM26/pyEzvizApi`](https://github.com/RenierM26/pyEzvizApi) - cloud protocol
  reference; the decryption algorithm derives from it (Apache-2.0), and it is used as
  a dev-only decryption test oracle (no runtime dependency).
- [`RenierM26/ha-ezviz`](https://github.com/RenierM26/ha-ezviz) - the official HACS
  EZVIZ integration (local-RTSP only).
- [`ESJavadex/ezviz-ha-addon`](https://github.com/ESJavadex/ezviz-ha-addon) -
  reverse-engineered cloud connection and VTM/VTDU handshake.
- [`LethalEthan/LE-EZVIZ-VS`](https://github.com/LethalEthan/LE-EZVIZ-VS) - protocol,
  encryption, and codec notes.

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## License

[Apache-2.0](./LICENSE) (see also [`NOTICE`](./NOTICE)).
