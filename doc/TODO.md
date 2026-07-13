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
- [x] `config_flow.py` — user step (email/pass/region, validated) → cameras step
      (multi-select + shared verification code); MFA `6002` → clear error;
      `unique_id` = account email.
- [x] `strings.json` + `translations/en.json`.
- [x] `tests/test_config_flow.py` — happy path, invalid-auth/MFA/cannot-connect,
      no-cameras + already-configured aborts (mocked api). `tests/conftest.py`
      enables custom integrations.
- Note: `requirements` stays `[]` and `pycryptodome`/`ffmpeg` get added with
      Milestone B (when the decryptor + streaming move into the integration).

### B. Camera entity + streaming

- [ ] Async streaming module: port the handshake + RTP depacketizer + PS decrypt
      from `scripts/` into `custom_components/` (async sockets / executor), with the
      reconnect loop + KeepAlive and on-demand start/stop. Move `ezviz_decrypt.py`
      in (add `pycryptodome` to `manifest.json` + `[project].dependencies`).
- [ ] `camera.py` — Camera entity; device-registry link to the official device
      (§6.3); substream/codec from options; on-demand.
- [ ] Tests (mock HA + sockets).

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
