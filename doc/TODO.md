# TODO - hass-ezviz-stream

Forward-looking action list. The authoritative *design* is `specification.md`;
protocol findings are in `reference.md`. Keep this lean - **prune landed work rather
than accumulating it** (details live in the specs + git history).

## Where we are (2026-07-16)

**v0.2.0 released.** The integration is live-verified end to end for both camera
transports (battery RTP/HEVC and mains/IPC encrypted MPEG-PS), installs via HACS,
passes `hassfest` + HACS CI, and has full unit-test coverage. What remains is polish
(options, MJPEG fallback, docs) and the net-add feature backlog.

## Shipped

Done and verified; details live in the code + git history.

- **Recordings & playback (2026-07-17)** - browse and play a camera's **cloud** and
  **SD-card** recordings in HA's media library (`media_source`: per-camera Cloud / SD
  subfolders), served as H.264 fragmented MP4. Cloud-replay TLS transport
  (`cloud_replay.py`) + SD `ysproto` `/playback` (`stream.iter_playback_ps`); record
  listing via `/v3/clouds/videos/list` and `/v3/streaming/v2/records`. **Video for all
  cams; audio for all cams** - plaintext, or AES-ECB-decrypted (clear ADTS header) for
  encrypted cams. Exposure is **opt-in** per account (`enable_recordings`, off by
  default, for privacy) with a per-camera override. Live-validated (Backyard SD, Deck
  encrypted). Protocol in `doc/reference.md` Part E / B.10.3.
- **Recording playback robustness (2026-07-17)** - handle mixed/rotated encryption
  keys: per-clip decode-probe (`broadcast.maybe_decrypt_replay`) serves each clip
  plaintext / decrypted / best-effort instead of trusting the crypt flag; `-g 30`
  keyframe cap on the transcode so short/static clips actually flush fragments and
  play; always-shown (optional-when-clear) verification-code field for decrypting old
  clips; and a load-time repair issue for a camera that is encrypted with no code.
  Live-validated on Front Door / Bar / Deck. See `doc/reference.md` E.4. **Pending:**
  an in-HA media-browser playback smoke test.
- **v0.2** - live-session thumbnails, per-camera H.264 transcode, keepalive fix
  (ended the ~5.5 s VTDU churn), offline/reconnect hardening. See the v0.2.0 release
  and git history for detail.

**v0.1:**

- **Setup / config flow** - two-step (validated account, then per-camera add with
  its own verification code); reconfigure, reauth, frame-grab validation on save,
  Advanced options, battery-drain warning; entities link to the official `ezviz`
  device.
- **Cloud protocol core (no runtime `pyezvizapi`)** - region login, device
  discovery, VTDU token, `ysproto` handshake; RTP/RFC-7798 HEVC depacketizer; MPEG-PS
  transport; oracle-validated AES-ECB decryptor; reconnect across the ~27 s VTDU drop.
- **Live view** - on-demand local MPEG-TS view fanned out from a single per-camera
  cloud session (go2rtc/WebRTC, HLS, snapshots); streams only while watched;
  RTP-clock pacing; both transports confirmed live.
- **Snapshots** - on-demand cached JPEG grab, last good frame retained across
  restarts; battery cams default to the last cloud motion image (no camera wake).
- **Tooling / CI** - `hassfest` + HACS green; duplicate-code hook; creds in memory only.

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

## Feature backlog (net-add vs official `ezviz`)

**Constraint: net-add only** - never duplicate the official `ezviz` integration.
It already ships PTZ (buttons), privacy/defence switches, sound-alarm siren,
firmware update, night-vision / work-mode selects, floodlight light, sensitivity
number, arm/disarm, and motion/alarm sensors - all out of scope. Only build what it
lacks. No runtime `pyezvizapi` (port behaviour into `api.py`, as with auth/decrypt).

- [x] **Recordings / playback (HIGH VALUE) - SHIPPED 2026-07-17.** Cloud + SD-card
      recording playback in the media library, video + audio, all cameras. See the
      Shipped section above and `doc/reference.md` Part E. (Remaining nice-to-haves: an
      event-type timeline / date grouping; in-HA browser smoke test.)
- [ ] **MQTT push notifications (valuable).** Official `ezviz` is polling-only
      (`paho_mqtt` is only a transitive `loggers` entry; no client is started), so
      real-time push is net-add. Use it to drive event-based snapshot refresh and cut
      battery-cam wakes - an enhancement to our own on-demand stream/snapshot path,
      not a duplicate motion sensor. Port `pyezvizapi.mqtt.MQTTClient`:
      - **Handshake (plain HTTPS on the token's `pushAddr`):** register (-> `clientId`)
        -> start (-> `ticket`) -> connect broker `pushAddr:1882` TCP MQTTv3.1.1,
        subscribe `"<appKey>/#"` QoS 2 -> stop tells the server to stop pushing.
      - **Payload:** JSON whose `ext` is a comma-separated string decoded to fields:
        `channel_type, time, device_serial, channel_no, alert_type_code,
        default_pic_url, media_url_alt1/2, resource_type, status_flag, file_id,
        is_encrypted, picChecksum, is_dev_video, metadata, msgId, image, device_name,
        reserved, sequence_number`. `device_serial` + `alert_type_code` + `time`
        target the refresh; `default_pic_url` (+ `is_encrypted`) is a fresh alarm
        image, replacing our alarms-API poll.
      - **Runtime dep:** needs an MQTT client - prefer `aiomqtt` (asyncio) over
        paho's background thread to fit HA's loop. Poll the device first to check
        push/notifications are enabled.

## Later / nice-to-have

- [ ] **Cloud-clip audio decryption (investigate).** Audio decrypt is validated on
      Deck **SD** but produces garbage on Front Door **cloud** clips (`sample_rate=0`,
      AAC-encode fails) while the video decrypts perfectly - so the "clear ADTS header +
      AES-ECB body" scheme doesn't hold across all camera/transport combos. Undecodable
      audio is currently dropped (`-an`) so video still plays (see `reference.md` E.4).
      To finish: get a plaintext oracle for a cloud clip (unencrypted camera, or an
      encryption-off/on pair) and bit-diff the audio transform for the cloud path.
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
