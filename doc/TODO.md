# TODO — hass-ezviz-stream

The project's official todo list. Keep it current: check items off as they land,
add new actions as they surface. The authoritative *design* lives in
`specification.md`; this file tracks *what to do next*.

## Repo state (as of 2026-07-13)

Diagnostic tooling exists and works (in `scripts/`, shared core in
`ezviz_cloud.py`); **no integration code in `custom_components/` yet** beyond the
skeleton. The proven protocol logic here is what gets ported into the integration.

- `scripts/ezviz_cloud.py` — shared control-plane + media-plane core (login,
  discovery, VTM/VTDU `ysproto` handshake, RTP/RFC-7798 HEVC de-packetizer,
  transport auto-detect, KeepAlive). Depends on `requests` (present via the venv).
- `scripts/ezviz_stream_probe.py` — capture all/one camera; reconnect across the
  ~27 s drop; decode a jpg per cam; `--probe-iframe` opcode sweep.
- `scripts/ezviz_list_cameras.py` — list account cameras (masked serials).
- `scripts/parse_ysproto_pcap.py` — decode ysproto control messages from a pcap to
  find the I-frame opcode (scapy-based). **`scapy` is a dev dep** (in `uv.lock`).

## ⚠ Container rebuild notes

- **Commit uncommitted work first.** `scripts/parse_ysproto_pcap.py`, the `scapy`
  dev-dep (`pyproject.toml` + `uv.lock`), and this TODO update may be uncommitted —
  commit before rebuilding so they aren't at risk.
- After rebuild, `uv sync` restores the venv (incl. `scapy`) from `uv.lock`.
- **`.git` ownership gotcha:** container-setup commits can leave some
  `.git/objects/*` dirs owned by `root`, which blocks `git add` as `vscode`
  ("insufficient permission for adding an object"). Fix:
  `sudo chown -R vscode:vscode .git`.
- `.env` (real creds) and `scripts/out/` (captures) are gitignored — verify they
  stay out of any commit.

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
          are valid PS (H.264), but the **main stream's keyframe interval is very
          long** (order of minutes): across a 17-opcode sweep (~170 s of sampling)
          only **one** IDR NAL was seen. Fresh sessions join mid-GOP with no IDR,
          the ~27 s VTDU drop caps a session below the GOP length, and independent
          sessions can't be spliced — so single-frame extraction fails on both IPC
          cams.
        - [x] **Empirical I-frame opcode sweep — negative.** `ezviz_stream_probe.py
              --probe-iframe` sweeps opcodes `0x130`–`0x145` (excl. the 5 known),
              sending each ~1.5 s into a fresh session and counting H.264 SPS/IDR
              markers after the send vs a control. **No opcode (ssn body) forced an
              IDR.** *(2026-07-12)*
        - [~] **Chosen route: capture the official EZVIZ client.** Capture a pcap
              while EZVIZ Studio live-views an IPC cam, then run
              `scripts/parse_ysproto_pcap.py <cap.pcapng>` (scapy-based, cross-
              platform) — it decodes the ysproto control messages and flags any
              unknown client→server opcode (the I-frame request). Awaiting capture.
        - [ ] Other routes if that stalls: (b) use the **substream (`stream=2`)** —
              short GOP → keyframe within one session (lower res); or (c) accept
              **RTP-only for v1** and defer PS. Not yet tried: `--probe-body empty`
              / wider opcode range (low expected yield).
        - Spec §4: mark the PS branch **capture-proven but not frame-proven**.
- [ ] **Authentication & 2FA fast-follow (not v1):**
    - [ ] Verify whether `RenierM26/pyEzvizApi` exposes an MFA / verification-code
          step in its login flow.
    - [ ] If it does, add an MFA-code step to our config flow (the
          `Bobsilvio/ezviz_hp7` fork proves the SMS-code approach works against the
          same cloud API). Would be a real differentiator.
- [ ] **Config flow must surface a clear "disable two-step verification" error**
      when login returns the MFA challenge (code `6002`).

## Build milestones (from spec §9)

Milestones 1–3 are **proven in the `scripts/` diagnostic tools** (against real
cameras); they still need **porting into `custom_components/ezviz_stream/`** as the
actual integration.

1. [x] **Auth + handshake** → VTDU socket streaming channel-0x01 packets, with
   wake-retry. Proven in `ezviz_cloud.py` (we implement the handshake ourselves;
   `pyEzvizApi` still to be adopted for the integration's auth layer).
2. [x] **Detect transport + de-packetize** (§4) → RTP branch (RFC-7798 → HEVC)
   fully proven with real frames; PS/TS branch dumps raw for FFmpeg (PS
   capture-proven, **not frame-proven** — see the open IPC item above).
3. [x] **Reconnect loop + KeepAlive** → proven (`ezviz_stream_probe.py`);
   KeepAlive is `0x132` (not `0x135`) and is required to keep media flowing.
4. [ ] **Serve** → wire into go2rtc `exec:` source (decided path, §6); add the
   HEVC→H.264 transcode option (default on; native HEVC as a config option, §6.1).
5. [ ] **HA entity + config flow** → creds / serial(s) / region / codec option;
   on-demand start/stop; device-link the camera to the official integration's
   device via matching `device_info` identifier (§6.3).
6. [ ] **Port the proven `scripts/` logic into `custom_components/ezviz_stream/`**
   (the integration proper) — currently only diagnostic tools exist.

## Nice-to-haves / later (spec §9.6)

- [ ] Multi-camera support.
- [ ] Encrypted-channel support (`0x0b`) for cams with Image Encryption ON
      (see `LethalEthan/LE-EZVIZ-VS` notes).
- [ ] Snapshot via the same cloud path.
- [ ] Config-flow convenience: enumerate existing `ezviz` devices to pre-fill the
      serial picker (§6.3).
