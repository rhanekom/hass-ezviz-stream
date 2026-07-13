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
- [x] **Prove the end-to-end cloud stream against real cameras** (§9 milestones
      1–3) — **done for BOTH transports** (RTP/HEVC battery cams and MPEG-PS/H.264
      IPC cams, the latter with substream + decryption; see the RESOLVED item below).
      Via the kept diagnostic tools `scripts/ezviz_stream_probe.py` +
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
    - [x] **RESOLVED: IPC / MPEG-PS frames now decode end-to-end.** *(2026-07-13.)*
          Root cause was **EZVIZ Image Encryption**, not GOP length. Fix has two
          parts, both proven live against IPC cam `F123…77`:
        1. **Substream (`--stream 2`)** — the main stream's keyframe interval is
           minutes; the substream delivers **SPS+PPS+IDR every ~5 s session** (per
           the probe's NAL census), so a single VTDU session carries a full keyframe.
        2. **Decrypt the video** — the MPEG-PS container and PPS are in the clear,
           but the **VCL slice NALs are AES-ECB encrypted** (NAL header included →
           `nalu_header_size=0`, auto-detected), key = the **verification code**
           zero-padded to 16 B. Decrypting with `pyezvizapi.stream.
           decrypt_hikvision_ps_video` before FFmpeg yields clean **H.264 768×432**.
           `ezviz_stream_probe.py --stream 2 --verify-code <code>` (or
           `EZVIZ_VERIFY_CODE` in `.env`) decodes a real frame in **one session**.
        - The battery (RTP/HEVC) cams decoded all along because their streams are in
          the clear; these IPC cams have encryption ON. The earlier "long GOP / need
          an I-frame opcode" reading was a symptom, not the cause.
        - **Decision surfaced:** `pyezvizapi` (already the planned auth dep) ships a
          *complete* cloud-stream stack (`VtmStreamClient`, `cloud_stream.py`,
          decryption). Revisit spec §4/§9 build-vs-buy: we may use its cloud_stream
          end-to-end rather than only porting our hand-rolled handshake/depacketizer.
        - Tooling landed 2026-07-13: `--stream {1,2}` selector, `--verify-code`
          (+`EZVIZ_VERIFY_CODE`) decrypt-before-decode (falls back to raw), a
          per-session **SPS/PPS/IDR census** log, and a transport-lock fix
          (unambiguous PS/TS magic now beats the weak RTP heuristic — a mid-PES
          `0x83` byte had mislocked transport to `rtp`).
        - [x] **Empirical I-frame opcode sweep — negative.** `ezviz_stream_probe.py
              --probe-iframe` sweeps opcodes `0x130`–`0x145` (excl. the 5 known),
              sending each ~1.5 s into a fresh session and counting H.264 SPS/IDR
              markers after the send vs a control. **No opcode (ssn body) forced an
              IDR.** *(2026-07-12)*
        - [~] **Chosen route: capture the official EZVIZ client.** Capture a pcap
              while EZVIZ Studio live-views an IPC cam, then run
              `scripts/parse_ysproto_pcap.py <cap.pcapng>` (scapy-based, cross-
              platform) — it decodes the ysproto control messages and flags any
              unknown client→server opcode (the I-frame request).
            - [x] **First capture (`scripts/in/EzViz_Capture.pcapng`, 2026-07-13):
                  the IPC cams streamed over LAN P2P, so the cloud path carried only
                  the battery cams.** The phone (`192.168.68.83`) reached the powered
                  IPC cams **directly over the LAN** (camera `192.168.68.55`, ctrl
                  port 9010 / media 9020) via EZVIZ's private P2P protocol (magic
                  `9e ba ac e9`, XML-negotiated, stream opcode `0x3105`/`0x3106`) —
                  a **different protocol from cloud `ysproto`**. Only the two
                  BatteryCamera cams (BH86…60, BH86…07) went via the cloud VTM/VTDU
                  (RTP/HEVC), so the ysproto parser saw only them. The one unknown
                  **cloud** opcode, **`0x130`**, is **stream-stop/teardown** (start/
                  stop range `0x12E`–`0x131`, ref §B.3; sent last after the
                  keepalives; `streamssn` body; already swept → no IDR), not a
                  force-IDR; the client sent no force-IDR on these cams. Handshake +
                  keep-alive `0x132`/`streamssn` confirmed to match ours.
                - Bonus: the LAN IPC media (`0x3106`) contains a real
                  **SPS+PPS+IDR** cluster — so the IPC cam **does** emit keyframes on
                  a fresh stream start. The cloud IPC failure is therefore about how
                  the **VTDU relays** the stream (likely a persistent/shared device
                  GOP joined mid-stream + the ~27 s drop), not the cam withholding
                  IDRs.
            - [ ] **Re-capture an IPC cam ON THE CLOUD PATH.** The app uses LAN P2P
                  whenever the phone shares the camera's network, so **turn the
                  phone's Wi-Fi off (cellular only)** — or capture from a network that
                  can't reach the cam — to force it through VTM/VTDU. Then live-view
                  the online IPC cam (**G145…96**; `F123…77` was `status=2`/asleep)
                  and re-run the parser.
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
2. [x] **Detect transport + de-packetize** (§4) → RTP branch (RFC-7798 → HEVC) and
   **PS branch both frame-proven**. PS/TS dumps raw for FFmpeg; encrypted IPC PS is
   AES-decrypted (verification code) before decode. Substream (`stream=2`) gives PS
   cams a keyframe per session.
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
- [x] Video decryption for cams with Image Encryption ON — **PROVEN** via
      `pyezvizapi.stream.decrypt_hikvision_ps_video` (AES-ECB, key = verification
      code padded to 16 B). Required (not optional) for our IPC test cams. **Still
      to do in the integration:** config flow must collect the verification code per
      encrypted cam, and we should auto-detect encryption rather than always trying
      to decrypt (decrypting a clear stream corrupts it).
- [ ] Snapshot via the same cloud path.
- [ ] Config-flow convenience: enumerate existing `ezviz` devices to pre-fill the
      serial picker (§6.3).
