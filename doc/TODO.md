# TODO - hass-ezviz-stream

Forward-looking action list. The authoritative *design* is `specification.md`;
protocol findings are in `reference.md`. Keep this lean - **prune landed work rather
than accumulating it** (details live in the specs + git history).

## Repo state (2026-07-13)

The **cloud streaming pipeline is proven end-to-end for both camera transports** in
the `scripts/` diagnostic harness. There is **no HA integration code yet** in
`custom_components/ezviz_stream/` beyond translations - building it is the focus now.

- `scripts/ezviz_cloud.py` - control+media core: region login, discovery, VTDU
  token, VTM/VTDU `ysproto` handshake, RTP/RFC-7798 HEVC depacketizer, transport
  auto-detect, KeepAlive (`0x132`), reconnect. (sync, `requests`.)
- `scripts/ezviz_decrypt.py` - our own AES-ECB Image-Encryption decryptor
  (`pycryptodome`); byte-for-byte oracle-validated (`tests/test_ezviz_decrypt.py`).
- `scripts/ezviz_stream_probe.py` · `ezviz_list_cameras.py` · `parse_ysproto_pcap.py`:
  capture/decode probe (`--stream {1,2}`, `--verify-code`), account lister, pcap
  opcode parser.

## Locked decisions (details in `specification.md`)

- **2FA off for v1** (§7.1) - same stance as official `ezviz`; surface a clear error
  on the MFA challenge `6002`.
- **Support battery + IPC cams**; decode path auto-detects transport RTP/PS/TS (§4).
- **Own two-step config flow, own entities** - not injected into official-`ezviz`
  entries (§7.2): account (user/pass/region) → camera select + verification code.
- **No runtime `pyezvizapi`** - HA core pins `==1.0.0.7`, one shared env → clash. We
  hand-roll auth+handshake and own our decryption; `pyezvizapi` is a dev-only test
  oracle (§8).
- **Apache-2.0** licensed (matches `pyezvizapi`, from which the decrypt algo derives).
- **Serve via go2rtc `exec:`**; default HEVC→H.264 transcode, native HEVC optional
  (§6, §6.1).
- **On-demand only** - stream while watched, stop on idle (battery-friendly; B.11).

Proven in `scripts/` (milestones 1–3, real cams), pending port: auth+handshake+wake
retry; transport-detect + RTP→HEVC + PS/H.264 decode incl. substream + AES decrypt;
reconnect loop + KeepAlive.

## Now: build the integration

### A. Foundation + config flow (§7.2) - DONE (2026-07-13)

- [x] Package scaffold: `manifest.json` (domain `ezviz_stream`, `config_flow`,
      `iot_class: cloud_polling`, `integration_type: hub`), `const.py`,
      `__init__.py` (`async_setup_entry`/`async_unload_entry`, entry `runtime_data`;
      `PLATFORMS` empty until the camera platform lands).
- [x] `api.py` - async cloud client on HA's `aiohttp` session: `async_login`,
      `async_get_cameras`; typed errors (`InvalidAuth`, `MfaRequired` for `6002`,
      `CannotConnect`, `InvalidRegion`). Ported auth/discovery from `ezviz_cloud.py`.
- [x] `config_flow.py` - **account entry** flow (email/pass/region, validated; MFA
      `6002` → clear error; `unique_id` = account email) + **camera subentry** flow
      (`CameraSubentryFlowHandler` via `async_get_supported_subentry_types`): pick a
      not-yet-added camera + supply **its own** verification code; `unique_id` =
      serial (no dupes). Per-camera codes - never a shared code.
- [x] `strings.json` + `translations/en.json` (account step + `config_subentries.
      camera` section). `hacs.json` HA floor bumped to `2025.4.0` (subentries).
- [x] `tests/test_config_flow.py` - account flow (happy path, invalid-auth/MFA/
      cannot-connect, already-configured) + subentry flow (add camera, no-cameras-
      left). `tests/conftest.py` enables custom integrations.
- Note: `requirements` stays `[]`; `pycryptodome`/`ffmpeg` get added with Milestone
      B. **Subentry reconfigure** (edit a camera's code) is deferred to Milestone D
      (polish); the `2025.4` floor already covers it.

### B. Camera entity + streaming

- [x] **B.1 - camera platform + entities (structural).** `camera.py`:
      `async_setup_entry` creates one `EzvizStreamCamera` per camera subentry
      (`async_add_entities(..., config_subentry_id=...)`); entity `unique_id` =
      serial, device-registry linked to the official EZVIZ device via a shared
      `("ezviz", serial)` identifier (§6.3). `PLATFORMS = [CAMERA]`. Streaming methods
      are placeholders (`async_camera_image` → None) until B.2/B.3. `PyTurboJPEG`
      added as a **dev** dep (needed to import HA's `camera` component in tests).
      Tested (`tests/test_camera.py`): entity + linked device created per subentry.
- [x] **B.2 - pure media core + decryptor moved in.** `decrypt.py` moved into the
      integration (git mv); `ysproto.py` added - RTP/RFC-7798 `HevcDepacketizer`,
      `detect_transport`, frame framing (`build_frame`/`read_frame`), minimal
      protobuf, StreamInfoReq/KeepAlive builders, stream-URL helpers - all pure/
      I/O-free and unit-tested (`tests/test_ysproto.py`). `pycryptodome` moved from
      dev → runtime (`manifest.json` + `[project].dependencies`); the diagnostic
      probe now decrypts via the `pyezvizapi` oracle (dev-only). Codec/protocol
      modules got a scoped `per-file-ignores` carve-out (CLAUDE.md documented).
- [x] **B.3a - producer control-plane (api.py).** `EzvizCamera` now carries VTM
      routing (`vtm_ip`, `vtm_port`, `biz`); added `async_get_vtdu_token()` (auth-addr
      resolve + JWT-sign + `vtdutoken2`). Tested (`tests/test_api.py`: login success/
      errors/region-redirect, camera VTM fields, VTDU token).
- [~] **B.3b - async streaming client + snapshot (code-complete; needs live
      verification).** `stream.py`: async VTM/VTDU handshake over `asyncio` sockets
      (non-blocking, so **in-process** - a subprocess is only needed for go2rtc live,
      C), media loop (RTP depacketize / PS decrypt), reconnect across the ~27 s drop +
      KeepAlive, and `grab_jpeg()` (reconnect until FFmpeg decodes one frame). Wired
      `camera.async_camera_image` → `grab_jpeg` via HA's ffmpeg manager; `ffmpeg`
      added to manifest `dependencies` (`ha-ffmpeg` + `PyTurboJPEG` are dev deps for
      tests). `_FrameReader` unit-tested (`tests/test_stream.py`); the socket path
      itself needs **live-cloud verification** (CI can't reach the cloud).
    - Known tuning: the snapshot drives a brief live session, so it is slow
      (seconds) and the 30 s budget is marginal for battery cams that need ~2
      sessions for a keyframe (main stream). Continuous/efficient live view is
      go2rtc (C); stream selection (main/sub) becomes an option in D.

### C. Serving (on-demand HTTP MPEG-TS -> go2rtc)

**Architecture decided (2026-07-14): serve MPEG-TS over a local HTTP view, not
`exec:`.** HA-managed go2rtc blocks `exec:` two ways - its API rejects it (`source
from insecure producer`; only `rtsp`/`http`-style sources are accepted) and its
config restricts `exec:` to the `ffmpeg` binary only. HA's go2rtc integration always
adds `stream_source()` via that API, so `exec:` can never work through it. Instead
`stream_source()` returns a token-guarded `http://127.0.0.1:<port>/api/ezviz_stream/
<serial>` URL; go2rtc pulls MPEG-TS from it (accepted via API, validated live) and
the `stream` component ffmpeg-opens the same URL for HLS - one path fixes both.

- [x] **C.1 - go2rtc provisioned** in the devcontainer (binary v1.9.14 + Dockerfile).
- [x] **C.2 - in-process streaming core (RTP/HEVC), live-verified.**
      `stream.py`: `iter_annexb()` async-generator yields `(rtp_timestamp, annexb)`
      across the ~27 s drop (reconnect + KeepAlive); `stream_annexb()` is now a thin
      file-like wrapper for the standalone diagnostic `producer.py` (no longer run by
      go2rtc). The integration consumes `iter_annexb` in-process (no subprocess, no
      creds file). `tests/test_stream.py` covers the wrapper + frame reader.
    - [ ] **C.2b - PS/encrypted (IPC) continuous path.** `mpegts_source` handles
          RTP/HEVC only; encrypted MPEG-PS (IPC) needs incremental decryption before the
          FFmpeg remux (`verification_code` is already threaded through for it).
- [x] **C.3 - entity + on-demand HTTP broadcaster; live-verified (WebRTC up, HLS
      `Protocol not found` gone, snapshots working).**
    - `broadcast.py`: `mpegts_source()` remuxes a camera's HEVC to MPEG-TS via
      `ffmpeg -use_wallclock_as_timestamps 1 -f hevc -i pipe:0 -c copy -f mpegts`;
      `CameraBroadcast` fans one on-demand upstream session out to all subscribers
      (starts on first, stops on last - battery-friendly), so go2rtc + HLS + snapshots
      share **one** cloud session (no VTDU 5405/5452 storm).
    - **Playback timing (C.3a, done):** `_Pacer` releases frames into FFmpeg on the
      camera's own RTP 90 kHz clock (rebasing to now on a reconnect/wrap), so the
      wall-clock-stamped MPEG-TS follows the real capture cadence - smooth and
      correct-rate, immune to the VTDU's bursty delivery. (Superseded two earlier
      guesses: default 25 fps was too fast -> periodic rebuffer; assumed CFR/wallclock
      alone drifted/jittered -> skip-and-catch-up.)
    - `stream_view.py`: `EzvizStreamMediaView` serves that MPEG-TS at
      `/api/ezviz_stream/<serial>`, guarded by a per-camera random token (constant-time
      compare); registered once in `__init__` (manifest `dependencies` gains `http`).
    - `camera.py`: `stream_source()` returns the token URL; the per-camera creds file
      and `exec:` are gone (account creds now stay in memory only - a security win).
    - Unit-tested: fan-out + on-demand lifecycle + drop-oldest backpressure + `_Pacer`
      scheduling/rebase (`test_broadcast.py`), view token/404 + streaming
      (`test_stream_view.py`), stream_source URL + registry lifecycle
      (`test_camera.py`).
    - Known: `mpegts_source` resolves the camera (VTM routing) once per upstream start;
      concurrent multi-camera *live* view opens one cloud session each (snapshot
      concurrency stays capped by `stream_semaphore`) - revisit a live cap in D if it
      trips VTDU limits.

### D. Polish

- [ ] Options flow (codec, main/sub stream, **serving mode**); reauth flow;
      diagnostics. (Frame-rate handling is solved by RTP-timestamp pacing - no fps
      option needed.)
- [ ] **Battery-cam thumbnail refresh.** Detect battery cameras and give them a
      slower snapshot refresh cadence than mains/IPC cams (each grab is a full cloud
      session, and a sleeping battery cam is slow to wake / more likely to return a
      blank first frame on a busy multi-camera view). Expose an opt-in checkbox in the
      camera subentry config flow. Ties into the `_SNAPSHOT_CACHE_TTL` / snapshot path
      in `camera.py`.
- [ ] **D.x - MJPEG serving mode (opt-in fallback).** An alternative to the default
      WebRTC-via-go2rtc path that needs no go2rtc/`stream` component and sidesteps
      HEVC-in-browser entirely (ffmpeg decodes to JPEG server-side). Override
      `Camera.handle_async_mjpeg_stream` (served at `/api/camera_proxy_stream/
      <entity>`) to push frames from a continuous decode - a `mjpeg_source` sibling to
      `broadcast.mpegts_source` (`ffmpeg -f hevc -i pipe:0 -c:v mjpeg -r <fps> -f
      image2pipe`, split on JPEG markers), driven through the existing
      `CameraBroadcast` so **one decode fans out to N viewers** (better than
      ezviz_hp7's one-ffmpeg-per-viewer). Selected via the options flow. Trade-offs:
      universal browser support + robust fallback, but heavy bandwidth (no interframe),
      live-view only (no audio/recording), fps capped (~5-10). Reuses the cloud
      session + on-demand lifecycle already built; scope is the source variant + the
      mjpeg override + option + tests. See the streaming-architecture discussion (git
      log / conversation 2026-07-14).
- [ ] `hassfest` + HACS validation green (CI `validate.yml`).
- [ ] README / docs: install + configuration.

## Later / nice-to-have

- [ ] MFA / verification-code login step (a differentiator - `Bobsilvio/ezviz_hp7`
      shows the SMS-code approach works). 2FA fast-follow.
- [ ] Multi-camera niceties; snapshot via the cloud path; pre-fill the camera picker
      from existing `ezviz` devices (§6.3).

## Container rebuild notes

- `uv sync` restores the venv from `uv.lock` after a rebuild.
- `.git` ownership gotcha: setup commits can leave `.git/objects/*` root-owned,
  blocking `git add` - fix with `sudo chown -R vscode:vscode .git`.
- `.env` (creds), `scripts/in/` + `scripts/out/` (captures), and `*.jpg` are
  gitignored - keep them out of commits.
