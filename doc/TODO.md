# TODO - hass-ezviz-stream

Forward-looking action list. The authoritative *design* is `specification.md`;
protocol findings are in `reference.md`; shipped features per release are in
`../VERSION_HISTORY.md`. Keep this lean - **prune landed work rather than accumulating
it** (details live in `VERSION_HISTORY.md`, the specs, and git history).

## Where we are (2026-07-18)

**v0.3.0 released (cloud + SD recordings & playback landed since v0.2.0).** The integration is
live-verified end to end for both camera transports (battery RTP/HEVC and mains/IPC
encrypted MPEG-PS), installs via HACS, passes `hassfest` + HACS CI, and has full
unit-test coverage. The flagship net-add (cloud + SD recordings) has landed; what
remains is the deferred/nice-to-have backlog below.

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

## Feature backlog (net-add vs official `ezviz`)

**Constraint: net-add only** - never duplicate the official `ezviz` integration.
It already ships PTZ (buttons), privacy/defence switches, sound-alarm siren,
firmware update, night-vision / work-mode selects, floodlight light, sensitivity
number, arm/disarm, and motion/alarm sensors - all out of scope. Only build what it
lacks. No runtime `pyezvizapi` (port behaviour into `api.py`, as with auth/decrypt).

- [ ] **MQTT push notifications (valuable; DEFERRED - not scheduled soon).** Official
      `ezviz` is polling-only - its Motion sensor is a 30 s coordinator poll, and
      `paho_mqtt` is only a transitive `loggers` entry (no client started) - so
      real-time push is net-add. Use it to drive event-based snapshot refresh and cut
      battery-cam wakes; scope v1 to **thumbnail refresh**, not a duplicate motion
      sensor (the official polled one already exists on the same device). Port
      `pyezvizapi.mqtt.MQTTClient` (dev-only oracle) into our own module:
      - **Prerequisite:** login today captures only `session_id` + `host`; MQTT also
        needs the EZVIZ internal `username` and the `pushAddr` (service URLs), so
        `api.py` login must capture both first.
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
      - **Runtime dep:** prefer **paho-mqtt directly** - HA core already bundles it, so
        no new runtime dependency and no version-clash risk (cost: its background
        thread needs a `call_soon_threadsafe` hop to the loop). `aiomqtt` is a nicer
        async fit but adds a dep pinning paho, which must be checked against HA's paho
        first. One account-wide client in the entry lifecycle, fanning events to
        cameras by serial; register/start/stop over our existing aiohttp session.

## Later / nice-to-have

- [ ] **Recordings polish.** An event-type timeline / date grouping in the media
      browser, and an in-HA media-browser playback smoke test (the feature itself
      shipped in 0.3.0 - see `../VERSION_HISTORY.md`).
- [ ] **Cloud-clip audio decryption (investigate).** Audio decrypt is validated on
      Deck **SD** but produces garbage on Front Door **cloud** clips (`sample_rate=0`,
      AAC-encode fails) while the video decrypts perfectly - so the "clear ADTS header +
      AES-ECB body" scheme doesn't hold across all camera/transport combos. Undecodable
      audio is currently dropped (`-an`) so video still plays (see `reference.md` E.4).
      To finish: get a plaintext oracle for a cloud clip (unencrypted camera, or an
      encryption-off/on pair) and bit-diff the audio transform for the cloud path.
- [ ] MFA / SMS verification-code login (a differentiator; `Bobsilvio/ezviz_hp7`
      shows the approach works). 2FA fast-follow.
- [ ] **MJPEG serving mode - compatibility fallback only.** Opt-in path that decodes
      to JPEG server-side (no go2rtc/WebRTC, no HEVC-in-browser), via a `mjpeg_source`
      sibling of `broadcast.mpegts_source` through the existing `CameraBroadcast`.
      Scope is *codec/browser incompatibility*, NOT network jitter (its 4-8x bandwidth
      worsens a weak link). Low priority unless a real compatibility gap turns up.
