# TODO — hass-ezviz-stream

Forward-looking action list. The authoritative *design* is `specification.md`;
protocol findings are in `reference.md`. Keep this lean — **prune landed work rather
than accumulating it** (details live in the specs + git history).

## Repo state (2026-07-13)

The **cloud streaming pipeline is proven end-to-end for both camera transports** in
the `scripts/` diagnostic harness. There is **no HA integration code yet** in
`custom_components/ezviz_stream/` beyond translations — building it is the focus now.

- `scripts/ezviz_cloud.py` — control+media core: region login, discovery, VTDU
  token, VTM/VTDU `ysproto` handshake, RTP/RFC-7798 HEVC depacketizer, transport
  auto-detect, KeepAlive (`0x132`), reconnect. (sync, `requests`.)
- `scripts/ezviz_decrypt.py` — our own AES-ECB Image-Encryption decryptor
  (`pycryptodome`); byte-for-byte oracle-validated (`tests/test_ezviz_decrypt.py`).
- `scripts/ezviz_stream_probe.py` · `ezviz_list_cameras.py` · `parse_ysproto_pcap.py`
  — capture/decode probe (`--stream {1,2}`, `--verify-code`), account lister, pcap
  opcode parser.

## Locked decisions (details in `specification.md`)

- **2FA off for v1** (§7.1) — same stance as official `ezviz`; surface a clear error
  on the MFA challenge `6002`.
- **Support battery + IPC cams**; decode path auto-detects transport RTP/PS/TS (§4).
- **Own two-step config flow, own entities** — not injected into official-`ezviz`
  entries (§7.2): account (user/pass/region) → camera select + verification code.
- **No runtime `pyezvizapi`** — HA core pins `==1.0.0.7`, one shared env → clash. We
  hand-roll auth+handshake and own our decryption; `pyezvizapi` is a dev-only test
  oracle (§8).
- **Apache-2.0** licensed (matches `pyezvizapi`, from which the decrypt algo derives).
- **Serve via go2rtc `exec:`**; default HEVC→H.264 transcode, native HEVC optional
  (§6, §6.1).
- **On-demand only** — stream while watched, stop on idle (battery-friendly; B.11).

Proven in `scripts/` (milestones 1–3, real cams), pending port: auth+handshake+wake
retry; transport-detect + RTP→HEVC + PS/H.264 decode incl. substream + AES decrypt;
reconnect loop + KeepAlive.

## Now: build the integration

### A. Foundation + config flow (§7.2) — DONE (2026-07-13)

- [x] Package scaffold: `manifest.json` (domain `ezviz_stream`, `config_flow`,
      `iot_class: cloud_polling`, `integration_type: hub`), `const.py`,
      `__init__.py` (`async_setup_entry`/`async_unload_entry`, entry `runtime_data`;
      `PLATFORMS` empty until the camera platform lands).
- [x] `api.py` — async cloud client on HA's `aiohttp` session: `async_login`,
      `async_get_cameras`; typed errors (`InvalidAuth`, `MfaRequired` for `6002`,
      `CannotConnect`, `InvalidRegion`). Ported auth/discovery from `ezviz_cloud.py`.
- [x] `config_flow.py` — **account entry** flow (email/pass/region, validated; MFA
      `6002` → clear error; `unique_id` = account email) + **camera subentry** flow
      (`CameraSubentryFlowHandler` via `async_get_supported_subentry_types`): pick a
      not-yet-added camera + supply **its own** verification code; `unique_id` =
      serial (no dupes). Per-camera codes — never a shared code.
- [x] `strings.json` + `translations/en.json` (account step + `config_subentries.
      camera` section). `hacs.json` HA floor bumped to `2025.4.0` (subentries).
- [x] `tests/test_config_flow.py` — account flow (happy path, invalid-auth/MFA/
      cannot-connect, already-configured) + subentry flow (add camera, no-cameras-
      left). `tests/conftest.py` enables custom integrations.
- Note: `requirements` stays `[]`; `pycryptodome`/`ffmpeg` get added with Milestone
      B. **Subentry reconfigure** (edit a camera's code) is deferred to Milestone D
      (polish); the `2025.4` floor already covers it.

### B. Camera entity + streaming

- [x] **B.1 — camera platform + entities (structural).** `camera.py`:
      `async_setup_entry` creates one `EzvizStreamCamera` per camera subentry
      (`async_add_entities(..., config_subentry_id=...)`); entity `unique_id` =
      serial, device-registry linked to the official EZVIZ device via a shared
      `("ezviz", serial)` identifier (§6.3). `PLATFORMS = [CAMERA]`. Streaming methods
      are placeholders (`async_camera_image` → None) until B.2/B.3. `PyTurboJPEG`
      added as a **dev** dep (needed to import HA's `camera` component in tests).
      Tested (`tests/test_camera.py`): entity + linked device created per subentry.
- [x] **B.2 — pure media core + decryptor moved in.** `decrypt.py` moved into the
      integration (git mv); `ysproto.py` added — RTP/RFC-7798 `HevcDepacketizer`,
      `detect_transport`, frame framing (`build_frame`/`read_frame`), minimal
      protobuf, StreamInfoReq/KeepAlive builders, stream-URL helpers — all pure/
      I/O-free and unit-tested (`tests/test_ysproto.py`). `pycryptodome` moved from
      dev → runtime (`manifest.json` + `[project].dependencies`); the diagnostic
      probe now decrypts via the `pyezvizapi` oracle (dev-only). Codec/protocol
      modules got a scoped `per-file-ignores` carve-out (CLAUDE.md documented).
- [x] **B.3a — producer control-plane (api.py).** `EzvizCamera` now carries VTM
      routing (`vtm_ip`, `vtm_port`, `biz`); added `async_get_vtdu_token()` (auth-addr
      resolve + JWT-sign + `vtdutoken2`). Tested (`tests/test_api.py`: login success/
      errors/region-redirect, camera VTM fields, VTDU token).
- [ ] **B.3b — socket driver + producer + snapshot.** **Decided: subprocess
      producer** (go2rtc-exec path, §6). Build the async VTM→VTDU handshake driver
      (using `ysproto` + `api`) + media loop (depacketize/decrypt) + reconnect across
      the ~27 s drop + KeepAlive, packaged as a runnable producer writing Annex-B/
      H.264 to stdout (creds via **env**, never argv). Wire `async_camera_image` to
      run it → FFmpeg → JPEG (working snapshot). Substream/codec from options.
      *(Socket path needs live-cloud verification, like the `scripts/` were.)*

### C. Serving

- [ ] go2rtc `exec:` source emitting Annex-B; HEVC→H.264 transcode default-on,
      native HEVC as an option (§6.1).

### D. Polish

- [ ] Options flow (codec, main/sub stream); reauth flow; diagnostics.
- [ ] `hassfest` + HACS validation green (CI `validate.yml`).
- [ ] README / docs: install + configuration.

## Later / nice-to-have

- [ ] MFA / verification-code login step (a differentiator — `Bobsilvio/ezviz_hp7`
      shows the SMS-code approach works). 2FA fast-follow.
- [ ] Multi-camera niceties; snapshot via the cloud path; pre-fill the camera picker
      from existing `ezviz` devices (§6.3).

## Container rebuild notes

- `uv sync` restores the venv from `uv.lock` after a rebuild.
- `.git` ownership gotcha: setup commits can leave `.git/objects/*` root-owned,
  blocking `git add` — fix with `sudo chown -R vscode:vscode .git`.
- `.env` (creds), `scripts/in/` + `scripts/out/` (captures), and `*.jpg` are
  gitignored — keep them out of commits.
