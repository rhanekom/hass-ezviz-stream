# TODO — hass-ezviz-stream

The project's official todo list. Keep it current: check items off as they land,
add new actions as they surface. The authoritative *design* lives in
`specification.md`; this file tracks *what to do next*.

## Now / next actions

- [x] **Decision: 2FA must be disabled for v1** — same stance as the official
      `ezviz` integration. Documented in `specification.md` §7.1 (and §3). *(decided
      2026-07-12)*
- [x] **Decision: target both battery and normal cameras**, so the decode path
      auto-detects transport (RTP / MPEG-PS / TS) rather than assuming RTP.
      Documented in `specification.md` §4. *(decided 2026-07-12)*
- [~] **Prove the end-to-end cloud stream against real cameras** (§9 milestones
      1–3) via the kept diagnostic tools `scripts/ezviz_stream_probe.py` +
      `ezviz_list_cameras.py` (shared core in `ezviz_cloud.py`). Login → handshake
      → channel-0x01 → transport-detect → depacketize/dump → FFmpeg → jpg per cam.
      Test account: 4 cams (2 BatteryCamera, 2 IPC), all online. *(2026-07-12)*
    - [x] **RTP / HEVC cameras (both battery cams): FULLY WORKING.** Real frames
          decoded — cam01 **2304×1296**, cam02 **1280×720** jpgs. Needed:
          periodic **KeepAlive** (`0x132`) to keep media flowing (without it the
          stream stalled after param sets), and typically **2 sessions** (first
          wakes the cam / yields only VPS+SPS+PPS, second carries a keyframe).
    - [x] **Reconnect loop (milestone 3): implemented + proven.** The tool
          reconnects across the ~27 s VTDU drop; a per-session min-JPEG-size gate
          rejects decode artifacts (an earlier 206 B "frame").
    - [x] **Transport auto-detect proven for both:** battery cams → RTP/HEVC;
          IPC cams → **MPEG-PS carrying H.264** (ffprobe confirms the codec).
    - [ ] **OPEN: IPC / MPEG-PS cameras don't yield a decodable frame.** Captures
          are valid PS (H.264), but **no single ~27 s VTDU session contains a
          keyframe** — fresh sessions join the main stream mid-GOP with no IDR, and
          independent live sessions can't be spliced (a reconnect often starts
          mid-PES → mis-detected `unknown`). So single-frame extraction fails on
          both IPC cams. Options to try (decision needed): request the **substream
          (`stream=2`)** which usually has a short GOP → keyframe within one
          session; find a protocol way to **request an I-frame**; or accept
          RTP-only for v1 and defer PS. Note in spec §4 that the PS branch is
          capture-proven but not yet frame-proven.
- [ ] **Authentication & 2FA fast-follow (not v1):**
    - [ ] Verify whether `RenierM26/pyEzvizApi` exposes an MFA / verification-code
          step in its login flow.
    - [ ] If it does, add an MFA-code step to our config flow (the
          `Bobsilvio/ezviz_hp7` fork proves the SMS-code approach works against the
          same cloud API). Would be a real differentiator.
- [ ] **Config flow must surface a clear "disable two-step verification" error**
      when login returns the MFA challenge (code `6002`).

## Build milestones (from spec §9)

1. [ ] **Auth + handshake** → obtain a VTDU socket streaming channel-0x01
   packets (reuse `pyEzvizApi` + `ezviz_stream.py` reference). Add **wake-retry**
   for sleeping battery cams (first request often returns 0 packets).
2. [ ] **Detect transport + de-packetize** (§4) → branch on the channel-0x01
   container (RTP vs MPEG-PS vs TS). RTP path: RFC-7798 de-packetize (§4.1) →
   write `.h265`; verify with FFmpeg (`ffprobe -f hevc`, single-frame decode).
   *Port the proven RTP logic verbatim — this is the core contribution.* PS/TS
   path: hand the raw body to FFmpeg's demuxer.
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
