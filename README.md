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
reaches cameras the way the EZVIZ app does - over the cloud - so you can watch them in
your dashboard, and it decrypts the video for cameras that have Image Encryption on.

> **This is a cloud integration.** Live view and snapshots go through EZVIZ's servers,
> not your local network, and EZVIZ limits how much you can stream at once. Please read
> [Limitations and what to expect](#limitations-and-what-to-expect) before installing.

## Features

| | |
|---|---|
| ☁️ **Cloud live view** | WebRTC in the dashboard, for cameras with no usable local RTSP. |
| 🔋 **Battery cameras** | Streams battery cams, with battery-aware defaults (a no-wake motion-image thumbnail + sub stream) so they aren't woken just to fill a tile. |
| 🔒 **Image Encryption** | Decrypts encrypted video on the fly, using the per-camera verification code. |
| 🖼️ **Snapshots** | Pick the thumbnail source per camera (live snapshot, last cloud motion image, or a static frame); cached and kept across restarts. |
| 🔌 **On-demand only** | A camera streams **only while someone is watching**, then stops - kind to batteries and to the EZVIZ cloud. |
| ⚙️ **Set up from the UI** | Add, reconfigure, and re-authenticate cameras and the account from Settings; each save checks the settings by grabbing a real frame. |
| 🏠 **Device-linked** | Camera entities attach to the same device as the official `ezviz` integration when it is installed. |

## Requirements

- **Home Assistant 2026.3.0 or newer** (the release that adopted Python 3.14; the
  integration uses Python 3.14 features and won't load on older builds).
- An EZVIZ cloud account with **two-step verification disabled** (same constraint as
  the official `ezviz` integration for now).
- For any camera with **Image Encryption** on, its **verification code** - the
  6-character code printed on the camera's label.
- The **go2rtc** and **ffmpeg** integrations, both bundled with a standard Home
  Assistant install (go2rtc turns the cloud video into a stream your browser can play).

## Installation

### HACS (recommended)

1. In HACS, open the ⋮ menu and choose **Custom repositories**.
2. Add `https://github.com/rhanekom/hass-ezviz-stream` with category **Integration**.
3. Install **EZVIZ Stream** and restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for
   **EZVIZ Stream**.

### Manual

Copy the `custom_components/ezviz_stream` folder into your Home Assistant
`config/custom_components/` directory and restart.

## Configuration

Setup has two levels: one **account**, then a **camera** for each device you want to
stream.

### 1. Add your account

Enter your EZVIZ **email**, **password**, and **region**. The integration signs in to
check the details before saving.

### 2. Add a camera

On the account, use **Add camera**:

1. **Pick the camera** from those found on your account (listed as *Name (Serial)*).
2. **Set its options** (the form shows the camera's serial, whether it's a battery
   camera, and whether Image Encryption is on):
    - **Verification code** - the 6-character code on the camera's label. It's
      **required** when the camera has Image Encryption on (and checked instantly, so a
      wrong code is caught before any streaming), and **hidden** when the camera
      reports no encryption.
    - **Advanced** (collapsed by default):
        - **Thumbnail source** - how the camera tile is filled:
            - *Live snapshot* - a fresh frame refreshed on a schedule (wakes battery
              cameras).
            - *Latest motion image* - the most recent cloud motion snapshot, with no
              camera wake.
            - *Static image* - the last frame from a live view, refreshed for free
              whenever you open the stream (never wakes the camera on its own).
            - *Static, then newer motion images* - a static baseline that is replaced
              only by motion newer than the last save (**the battery default**; save
              the camera again to dismiss an unwanted image).
        - **Thumbnail refresh interval** - how often a tile that's being viewed
          refreshes (longer wakes battery cameras less often).
        - **Video stream** - **Main (HD)** or **Sub (lower resolution)**. Battery
          cameras default to **Sub** (gentler on the battery and on a weak connection).
        - **Transcode to H.264** - off by default. Home Assistant's go2rtc already
          converts the camera's native HEVC to H.264 for browsers on demand, so leave
          this off unless live view won't play in your browser. Turning it on
          re-encodes the video continuously while the camera is watched, which is
          CPU-heavy (roughly a full core per 1080p camera) - enable it only on the
          cameras you need it for.

When you save, the integration **grabs a real frame** to confirm the code and stream
work. If it can't - a wrong code, or a battery camera that's asleep or briefly
unreachable - it lets you **try again** or **save anyway**, so nothing that fails to
stream is accepted silently.

### Changing a camera later

Use the camera's **Reconfigure** action to change its verification code, thumbnail
source, refresh interval, or stream. (Saving a camera set to *Static, then newer
motion images* also re-anchors it, dismissing any stale motion image.) If your account
password changes or expires, Home Assistant prompts you to **re-authenticate** without
re-adding anything.

### Account options

From the account's **Configure** action you can set **Max concurrent snapshot
fetches** - how many camera thumbnails may refresh from the cloud at once. The default
is **1** (serialised, safest against EZVIZ rate-limiting); raise it only if a
multi-camera dashboard fills too slowly.

## How it works

- The integration signs in to your EZVIZ account and connects to each camera through
  the cloud, the same way the EZVIZ app does.
- It streams a camera **only while you're watching it**, and stops when you close the
  view - so cameras (battery ones especially) aren't streaming around the clock.
- Home Assistant's built-in go2rtc turns the cloud video into a stream your browser can
  play. For encrypted cameras, the integration decrypts the video first using your
  verification code.
- Each camera uses a single cloud connection, shared by everyone viewing it and by the
  thumbnail, so a busy dashboard doesn't open duplicate sessions.

## Supported cameras

- **Battery cameras** - verified end to end.
- **Mains / indoor & outdoor (IPC) cameras**, including **Image Encryption** - verified
  end to end.

## Limitations and what to expect

Because this works entirely through the EZVIZ cloud, it behaves differently from a
local-network camera:

- **Everything goes through EZVIZ's servers.** Live view and snapshots travel from the
  camera up to the cloud and back down to Home Assistant, not over your LAN. It needs a
  working internet connection and EZVIZ's servers to be up; if either is down, cameras
  won't stream.
- **EZVIZ throttles cloud streaming.** Your account has an (unpublished) limit on how
  many streams it can open at once. The integration is deliberately gentle - one shared
  connection per camera, only while watched, and snapshot fetches are rate-limited - but
  a large dashboard, or several people watching at once, can still hit the limit.
  Symptoms: live view that won't start, stalled or blank tiles, and
  `concurrency/resource limit` warnings in the log. Ease it by watching fewer cameras at
  once, keeping battery cams on the motion-image thumbnail (or a longer refresh
  interval), and using the Sub stream.
- **Live view has cloud latency.** A round-trip to the cloud is slower than local RTSP.
  WebRTC keeps latency low, but on a jittery or slow connection it will skip forward to
  stay live rather than lag smoothly (see [Tips](#tips-and-troubleshooting)).
- **Battery cameras drain while streaming.** Live viewing runs off the battery, so watch
  sparingly and prefer the Sub stream.
- **Unofficial protocol.** This talks to the EZVIZ cloud using a reverse-engineered
  protocol, the same one the app uses. EZVIZ could change or restrict it at any time,
  which may break streaming until the integration is updated.
- **Two-step verification (2FA)** is not supported yet - disable it in the EZVIZ app.
- **Native HEVC and an MJPEG fallback** are planned but not yet available.

## Tips and troubleshooting

- **Live view keeps "catching up" or skipping.** This is almost always the network:
  WebRTC keeps latency low and skips rather than lagging when a connection is jittery or
  bandwidth-limited. Switch that camera to the **Sub** stream (Reconfigure → Advanced),
  and prefer 5 GHz Wi-Fi or a wired Home Assistant host. To tell a local problem from a
  remote one, compare live view on the same Wi-Fi as Home Assistant versus away from
  home.
- **A camera is draining fast.** Live viewing runs off the battery. It streams only
  while watched, but frequent or long viewing still adds up - use the Sub stream and
  keep sessions short.
- **A thumbnail is briefly blank.** Thumbnails are cached and the last good image is kept
  across restarts. A blank usually means a brand-new camera whose first frame hasn't been
  captured yet, or a battery camera slow to wake; it fills in on the next refresh. Using
  the **motion-image** thumbnail avoids waking the camera for a tile at all.
- **`concurrency/resource limit` warnings in the log.** EZVIZ is refusing simultaneous
  streams - view or snapshot fewer cameras at once.
- **Two-step verification.** If sign-in fails, make sure 2FA is turned off in the EZVIZ
  app.

## Credits

Built on the work of others who reverse-engineered the EZVIZ cloud protocol:

- [`RenierM26/pyEzvizApi`](https://github.com/RenierM26/pyEzvizApi) - cloud protocol
  reference (the decryption approach derives from it, under Apache-2.0).
- [`RenierM26/ha-ezviz`](https://github.com/RenierM26/ha-ezviz) - the official HACS
  EZVIZ integration (local-RTSP only).
- [`ESJavadex/ezviz-ha-addon`](https://github.com/ESJavadex/ezviz-ha-addon) -
  reverse-engineered cloud connection and streaming handshake.
- [`LethalEthan/LE-EZVIZ-VS`](https://github.com/LethalEthan/LE-EZVIZ-VS) - protocol,
  encryption, and codec notes.

## Contributing

Contributions are welcome - see [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## License

[Apache-2.0](./LICENSE) (see also [`NOTICE`](./NOTICE)).
