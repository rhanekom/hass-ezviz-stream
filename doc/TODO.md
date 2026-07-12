# TODO — hass-ezviz-stream

The project's official todo list. Keep it current: check items off as they land,
add new actions as they surface. The authoritative *design* lives in
`specification.md`; this file tracks *what to do next*.

## Now / next actions

- [ ] **Authentication & 2FA** (see §7, and the 2FA discussion):
    - [ ] Verify whether `RenierM26/pyEzvizApi` exposes an MFA / verification-code
          step in its login flow.
    - [ ] v1: document "disable EZVIZ two-step verification" as a setup
          prerequisite (matches the official `ezviz` integration, which does
          **not** support 2FA).
    - [ ] Fast-follow: if `pyEzvizApi` supports it, add an MFA-code step to our
          config flow (the `Bobsilvio/ezviz_hp7` fork proves the SMS-code approach
          works against the same cloud API). Would be a real differentiator.
    - [ ] Add an "Authentication & 2FA" note to §7 of `specification.md`.

## Build milestones (from spec §9)

1. [ ] **Auth + handshake** → obtain a VTDU socket streaming channel-0x01
   packets (reuse `pyEzvizApi` + `ezviz_stream.py` reference). Add **wake-retry**
   for sleeping battery cams (first request often returns 0 packets).
2. [ ] **De-packetize** (§4.1) → write `.h265`; verify with FFmpeg
   (`ffprobe -f hevc`, single-frame `ffmpeg` decode). *Port the proven logic
   verbatim — this is the core contribution.*
3. [ ] **Producer** → continuous Annex-B HEVC to stdout + **reconnect loop** for
   the ~27 s VTDU drops + KeepAlive (`0x135`) for longer sessions.
4. [ ] **Serve** → wire into go2rtc `exec:` source (decided path, §6); add the
   HEVC→H.264 transcode option (default on; native HEVC as a config option, §6.1).
5. [ ] **HA entity + config flow** → creds / serial(s) / region / codec option;
   on-demand start/stop; device-link the camera to the official integration's
   device via matching `device_info` identifier (Powercalc-style, §6.3).

## Nice-to-haves / later (spec §9.6)

- [ ] Multi-camera support.
- [ ] Encrypted-channel support (`0x0b`) for cams with Image Encryption ON
      (see `LethalEthan/LE-EZVIZ-VS` notes).
- [ ] Snapshot via the same cloud path.
- [ ] Config-flow convenience: enumerate existing `ezviz` devices to pre-fill the
      serial picker (§6.3).
