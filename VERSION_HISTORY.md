# Version history

Features that landed in each release. Design lives in `doc/specification.md`,
protocol findings in `doc/reference.md`, and forward-looking work in `doc/TODO.md`.

## 0.3.0 (unreleased)

- **Recordings & playback.** Browse and play a camera's **cloud** and **SD-card**
  recordings in Home Assistant's media library (`media_source`, per-camera Cloud/SD
  folders), served as H.264 fragmented MP4, with **video and audio for all cameras**
  (plaintext or AES-ECB-decrypted). **Opt-in** per account (`enable_recordings`, off by
  default) with a per-camera override. Robust to mixed/rotated encryption keys via a
  per-clip decode-probe (serves each clip plaintext / decrypted / best-effort). Uses a
  cloud-replay TLS transport and the SD `ysproto` `/playback` transport.
- **Offline-camera hardening.** An offline or unreachable camera no longer re-runs a
  full cloud-handshake storm each time Home Assistant re-pulls the view URL: the
  broadcaster records a cooldown after a session that produced no media and refuses to
  start a new one until it passes. FFmpeg is now reaped deterministically on the event
  loop (fixes a `BaseSubprocessTransport.__del__` warning on Python 3.14).
- **Distinct camera name.** The cloud camera carries a "Cloud" sub-name so it does not
  collide with the official `ezviz` entity on the same device card.
- **Internals & tooling.** Decryption refactored into `decrypt_stream`; the standalone
  CLI producer moved to `scripts/`; strict `mypy` plus extra pre-commit hooks
  (`uv-lock`, `check-jsonschema`, `zizmor`); test coverage for the streaming iterators
  and keyframe capture.

## 0.2.0 (2026-07-15)

- **Live-session thumbnails.** Thumbnails are grabbed from the shared live session
  instead of opening a rival cloud grab.
- **Per-camera H.264 transcode.** Opt-in per camera (`force_h264`) for browsers that
  cannot play native HEVC.
- **Keepalive fix.** Corrected the KeepAlive body encoding, ending the ~5.5 s VTDU
  session churn (heavy live-view buffering and the day/night flip).
- **Offline / reconnect hardening.** Bounded the reconnect loop so an offline camera
  gives up fast instead of looping forever; stop the Home Assistant stream on entity
  removal; fast-fail the stream for a known-offline mains camera; a static
  "refreshed when viewed" thumbnail mode; complete-keyframe capture (no half-written
  images) with atomic snapshot writes.

## 0.1.0 (2026-07-14)

- **Setup / config flow.** Two-step setup (validated account, then per-camera add with
  its own Image-Encryption verification code); per-camera reconfigure, account reauth,
  a frame-grab validation on save, an Advanced options section, and a battery-drain
  warning. Camera entities link to the official `ezviz` device when present.
- **Cloud protocol core (no runtime `pyezvizapi`).** Region-aware login, device
  discovery, VTDU token, and the VTM/VTDU `ysproto` handshake; the RTP/RFC-7798 HEVC
  depacketizer; MPEG-PS transport; an oracle-validated AES-ECB Image-Encryption
  decryptor; reconnect across the ~27 s VTDU drop.
- **Live view.** On-demand local MPEG-TS view fanned out from a single per-camera cloud
  session to go2rtc/WebRTC, the HLS `stream` component, and snapshots; streams only
  while watched (battery-friendly); RTP-clock playback pacing. Both transports
  confirmed live.
- **Snapshots.** On-demand cached JPEG grab, with the last good frame retained across
  restarts; battery cameras default to the last cloud motion image (no camera wake).
- **Tooling / CI.** `hassfest` + HACS validation green; a duplicate-code pre-commit
  hook; account credentials kept in memory only (no secrets on disk).
