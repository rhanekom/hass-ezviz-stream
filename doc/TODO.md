# TODO - hass-ezviz-stream

Forward-looking action list. The authoritative *design* is `specification.md`;
protocol findings are in `reference.md`. Keep this lean - **prune landed work rather
than accumulating it** (details live in the specs + git history).

## Where we are (2026-07-14)

The integration is built and **live-verified end to end for both camera transports**
(battery RTP/HEVC and mains/IPC encrypted MPEG-PS). It installs via HACS, passes
`hassfest` + HACS CI, and has full unit-test coverage. What remains is polish
(options, MJPEG fallback, docs) and one open design decision (live buffering).

## Shipped (v0.1)

High-level features that are done and verified. Implementation lives in the code +
git history; this list is the feature-level summary.

- **Account + camera setup.** Own two-step config flow: account (email / password /
  region, validated) then per-camera add, each with its own Image-Encryption
  verification code. Per-camera **reconfigure**, account **reauth**, and a **frame-grab
  validation** on save (retry / save-anyway soft block). Simple form with an
  **Advanced** section (thumbnail cadence, main/sub stream; battery cams default to
  the **sub stream** + slower thumbnails) and a **battery-drain warning**. Camera
  entities link to the official `ezviz` device.
- **Cloud protocol core (no runtime `pyezvizapi`).** Hand-rolled region login,
  device discovery, VTDU token, and the VTM/VTDU `ysproto` handshake; RTP/RFC-7798
  HEVC depacketizer; MPEG-PS transport; our own AES-ECB Image-Encryption decryptor
  (one-shot + an incremental streaming variant), byte-for-byte oracle-validated.
  Reconnect across the ~27 s VTDU drop + KeepAlive.
- **Live view.** On-demand local HTTP MPEG-TS view, fanned out from a **single**
  per-camera cloud session to go2rtc (WebRTC), the HLS `stream` component, and
  snapshots - so a dashboard never opens concurrent sessions. Streams only while
  watched (battery-friendly). RTP-clock playback pacing keeps timing smooth. Both
  transports confirmed live in HA.
- **Snapshots.** On-demand JPEG grab, cached (battery cams poll far less), and the
  last good frame is retained across restarts so tiles never go blank.
- **Tooling / CI.** `hassfest` + HACS validation green; duplicate-code pre-commit
  hook; account credentials stay in memory only (no secrets on disk).

## Locked decisions (details in `specification.md`)

- **2FA off for v1** (§7.1) - surface a clear error on the MFA challenge `6002`.
- **Support battery + IPC cams**; decode auto-detects transport RTP/PS/TS (§4).
- **Own config flow + entities** - not injected into official-`ezviz` entries (§7.2).
- **No runtime `pyezvizapi`** - HA core pins `==1.0.0.7` (clash); we hand-roll auth +
  handshake and own the decryptor. `pyezvizapi` is a dev-only test oracle (§8).
- **Apache-2.0** licensed (matches `pyezvizapi`, from which the decrypt algo derives).
- **Serve via a local HTTP MPEG-TS view, not go2rtc `exec:`** - HA-managed go2rtc
  rejects `exec:` (insecure-producer + ffmpeg-only allow-list); go2rtc pulls our
  token-guarded `http://` URL instead (§6). Default HEVC->H.264 transcode is go2rtc's.
- **On-demand only** - stream while watched, stop on idle (battery-friendly).
- **Live buffering: leave it to WebRTC (resolved 2026-07-14).** RTP-clock pacing
  fixed the source-side timing. Any residual edge-chasing is network-bound (link
  jitter/bandwidth), and the decisive playout buffer lives in the browser's WebRTC
  receiver - not in our integration, and not usefully in go2rtc. We do not add an
  integration-level buffer (it would only add latency, not stop the browser skipping).
  Mitigation for a weak link is the **sub-stream** (lower bitrate) + the network
  itself. MJPEG is *not* a fix here - its higher bandwidth worsens a constrained link.

## Remaining

- [ ] **Options flow additions.** Codec (transcode vs native HEVC - needs the go2rtc
      wiring decision); serving mode (needs MJPEG first); diagnostics download.
- [ ] **README / docs** - install + configuration.
- [ ] **HACS brands** - PR icon/logo assets to `home-assistant/brands` before
      default-store submission (CI currently ignores the `brands` check).

## Later / nice-to-have

- [ ] MFA / SMS verification-code login (a differentiator; `Bobsilvio/ezviz_hp7`
      shows the approach works). 2FA fast-follow.
- [ ] Multi-camera niceties; pre-fill the camera picker from existing `ezviz`
      devices (§6.3).
- [ ] **MJPEG serving mode - compatibility fallback only.** Opt-in path that decodes
      to JPEG server-side (no go2rtc/WebRTC, no HEVC-in-browser), via a `mjpeg_source`
      sibling of `broadcast.mpegts_source` through the existing `CameraBroadcast`.
      Scope is *codec/browser incompatibility*, NOT network jitter (its 4-8x bandwidth
      worsens a weak link). Low priority unless a real compatibility gap turns up.

## Container rebuild notes

- `uv sync` restores the venv from `uv.lock` after a rebuild.
- `.git` ownership gotcha: setup commits can leave `.git/objects/*` root-owned,
  blocking `git add` - fix with `sudo chown -R vscode:vscode .git`.
- `.env` (creds), `scripts/in/` + `scripts/out/` (captures), and `*.jpg` are
  gitignored - keep them out of commits.
