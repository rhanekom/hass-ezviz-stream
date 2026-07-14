# EZVIZ Cloud Live-Stream for Battery Cameras - build spec / handoff

**Purpose:** everything needed to build a *standalone* Home Assistant extension
(HACS custom integration or HA add-on) that provides a **live video stream from
EZVIZ battery cameras via the EZVIZ cloud** - for cameras that have **no local
RTSP** (all recent battery models). This is a clean-room handoff: it captures a
working, *proven* reverse-engineering result so a new project can start from the
solution, not from scratch.

> Status (2026-07-11): **decode proven end-to-end** on an EZVIZ **CB3** battery
> cam. Pulled H.265 from the cloud, de-packetized to a clean HEVC elementary
> stream, decoded with FFmpeg → **2304×1296 @ ~15 fps**. It now streams live in Home Assistant. Original investigation + working scripts lived in a
> throwaway scratchpad; the essential logic is reproduced below.

> **Decisions as built (2026-07-14; some reversed from the original plan):**
>
> - **Auth layer:** **hand-rolled, no runtime dependency on `pyEzvizApi`.** HA core
>   pins an incompatible `pyezvizapi` for the official integration and one environment
>   cannot satisfy both, so we implement login, discovery, the VTDU token, and the
>   VTM/VTDU handshake ourselves; `pyEzvizApi` stays a dev-only decryption test oracle.
> - **Target cameras:** **both battery *and* mains-powered ("normal") cameras.**
>   The battery models drove this project (no local RTSP), but the same cloud path
>   serves normal cams too, so v1 supports both.
> - **Media transport:** **not assumed to be RTP.** The channel-`0x01` body varies
>   by model/firmware - newer models emit RTP/H.265, older (many battery) models
>   emit **MPEG-PS**. The decode path **auto-detects the transport from the first
>   bytes and branches** (see §4 / `reference.md` B.7). This is the single biggest
>   portability risk, and - with normal cams now in scope - an explicit v1 concern,
>   not a footnote.
> - **Codec:** go2rtc transcodes HEVC→H.264 for the browser (WebRTC) automatically;
>   our FFmpeg only remuxes to MPEG-TS (stream copy). A native-HEVC codec option is
>   deferred (see §6.1).
> - **Encryption: supported.** We ship our own AES-ECB Image-Encryption decryptor (a
>   one-shot form plus an incremental streaming variant for continuous IPC live),
>   validated byte-for-byte against `pyEzvizApi`.
> - **2FA:** must be **disabled** on the EZVIZ account for v1 - same stance as the
>   official `ezviz` integration (see §7).
> - **Serving path (§6): a local, token-guarded HTTP MPEG-TS view that go2rtc pulls.**
>   HA-managed go2rtc rejects `exec:` via its API and restricts config `exec:` to the
>   `ffmpeg` binary, so `stream_source()` returns an `http://` URL. One on-demand cloud
>   session per camera is fanned out to go2rtc (WebRTC), the HLS `stream` component,
>   and snapshots.
> - **Transcoding:** **no separate container.** go2rtc does the HEVC→H.264
>   transcode for WebRTC; we run FFmpeg only for the cheap MPEG-TS remux. A dedicated
>   add-on/container was rejected - it costs portability (add-ons require HA OS /
>   Supervised; a `custom_component` works on all install types).
> - **Coupling to the official `ezviz` integration:** **soft, not a dependency.**
>   We ship our own config flow + credentials (§3, §7) and stand alone. We link our
>   camera entity to the *same device* as the official integration via a matching
>   `device_info` identifier (Powercalc-style - see §6.3), and *optionally* read
>   its existing devices to pre-fill the serial picker. We never reach into its
>   `hass.data` internals.

---

## 1. Why this exists

- EZVIZ **battery** cameras (CB3, HP2, etc., recent firmware) have **local RTSP
  removed** - no "Local Service Settings", port 554 closed, EZVIZ support
  confirms battery models don't support it.
- The official HA `ezviz` integration and the HACS `RenierM26/ha-ezviz` build
  both stream **only via local RTSP** (`pyezvizapi`, keyed on the LAN IP), so
  they give **no live view** for battery cams (`camera.*` reports
  `supported_features: 0`; on-demand snapshot 500s while the cam sleeps).
- `go2rtc` alone can't help - it needs a stream source, and there isn't a local
  one.
- **But** the EZVIZ *app* streams these cams fine over the cloud (P2P/VTDU). That
  cloud path is what we decode here.

## 2. High-level pipeline

```
EZVIZ account login (HTTPS)                     # api<region>.ezvizlife.com
  → server info  → authAddr                     # /api/server/info/get
  → device list  → VTM node (ip:port)           # /v3/userdevices/.../pagelist?filter=VTM
  → VTDU tokens                                 # {authAddr}/vtdutoken2
  → TCP to VTM: StreamInfoReq → VTDU redirect    # ysproto:// custom binary proto
  → TCP to VTDU: StreamInfoReq → media packets   # channel 0x01 media
  → detect transport (RTP / MPEG-PS / TS)         # varies by model (§4)
  → RTP: RFC-7798 de-packetize → Annex-B HEVC     # <-- the key decode (RTP models)
  → PS/TS: hand to FFmpeg demuxer                  # older/battery models
  → FFmpeg remux → MPEG-TS → token-guarded HTTP view → go2rtc → WebRTC (HA camera)
```

## 3. Authentication + stream handshake (control plane)

The control plane and handshake are documented by two community projects, which we
used as **protocol references** - the integration implements all of it itself and
takes no runtime dependency on either (see the decisions block and §8):

- **`RenierM26/pyEzvizApi`** - maintained EZVIZ cloud auth/API library (login,
  device list, tokens); reference for the auth/API shapes.
- **`ESJavadex/ezviz-ha-addon`** (`ezviz-camera/ezviz_stream.py`) - a compact,
  dependency-light (`requests` only) implementation of the *whole* control plane
  **plus** the VTM/VTDU socket handshake. This is the clearest reference for the
  binary protocol; the classes below are from it.

Key facts (constants from `ezviz_stream.py`):

- **Region → API subdomain:** `Europe`/`Africa`→`ieu`, `Asia`→`isgp`,
  `NorthAmerica`/`Oceania`→`ius`, `SouthAmerica`→`isa`.
  `api_url = https://api{code}.ezvizlife.com`. (South Africa uses **`ieu`** →
  pass region `"Europe"`.)
- **Client identity** (emulates the PC/Studio client): `clientType=9`,
  `clientNo="shipin7"`, `appId="ys7"`, `customNo="1000001"`,
  `clientVersion="2,5,1,2109068"`, `featureCode` = 32 zeros.
- **Login:** `POST /v3/users/login/v5`, form-encoded
  `account=<email>&password=<MD5(password)>&featureCode=…&cuName=<b64>` →
  `loginSession.sessionId` (a **JWT**). **2FA must be off** (§7); an account with
  2FA enabled returns an MFA challenge (code `6002`) that v1 does not handle.
- **Server info:** `POST /api/server/info/get` → `serverResp.authAddr`.
- **Devices:** `GET /v3/userdevices/v1/resources/pagelist?...&filter=VTM` →
  `resourceInfos[]` (match `deviceSerial`, need `resourceType>0`) and a `VTM`
  map keyed by `resourceId` → `{externalIp, port}`.
- **VTDU tokens:** decode the JWT payload, take claim `s` (`sign`); then
  `GET {authAddr}/vtdutoken2?ssid=<sessionId>&sign=<sign>` → `{tokens:[...]}`.

**VTM/VTDU binary protocol** - 8-byte header then body:

| Off | Len | Field |
|-----|-----|-------|
| 0 | 1 | Magic `0x24` |
| 1 | 1 | Channel |
| 2 | 2 | Length (u16 BE) |
| 4 | 2 | Sequence (u16 BE) |
| 6 | 2 | Message code (u16 BE) |

- Channels: `0x00` unenc-msg, `0x01` unenc-stream, `0x0a` enc-msg, `0x0b`
  enc-stream.
- Message codes: `StreamInfoReq=0x13b`, `StreamInfoRsp=0x13c`,
  `KeepAlive=0x135`.
- **StreamInfoReq body** = hand-rolled protobuf: field 1 = stream URL (string),
  field 3 = `"v3.6.3.20221124"`, field 4 = `0` (int32), field 6 =
  `"v3.6.3.20221124"`. (field 2 = optional vtm_stream_key.)
- **Stream URL:**
  `ysproto://<ip>:<port>/live?dev=<serial>&chn=1&stream=1&cln=9&isp=0&auth=1&ssn=<token>&biz=1&vip=0&timestamp=<ms>`
- Send StreamInfoReq to the **VTM** → its response body contains a
  `ysproto://<vtdu_ip>:<vtdu_port>/…` redirect. Connect to the **VTDU** and send
  StreamInfoReq again with that URL → media starts flowing on channel `0x01`.

## 4. Stream format (the decode - our contribution)

**The channel-`0x01` body is not always the same container** - it varies by camera
model and firmware, and since v1 targets both battery and normal cams we must
handle the spread. Auto-detect from the first bytes of the reassembled body and
branch (full table in `reference.md` B.7):

| First bytes | Transport | Handling |
|-------------|-----------|----------|
| version bits `10` in byte 0, `PT=96` | **RTP** (RFC 7798), dynamic HEVC | de-packetize (§4.1) → Annex-B HEVC |
| `00 00 01 BA` | **MPEG-PS** (pack header) - carries video **and** audio | hand to FFmpeg as `-f mpegts`/PS, or demux the PES; **no §4.1 needed** |
| `0x47` | MPEG-TS | hand to FFmpeg as `-f mpegts` |
| *(other)* | MPEG-4 / unknown | log a sample and treat as unsupported for now |

Roughly: **newer models emit RTP/H.265; many older/battery models emit MPEG-PS**
(the only container observed to carry audio). The spike (§9 milestone 1–2) records
which transport each of our test cameras actually emits. The RTP branch below is
the proven core; the MPEG-PS branch mostly defers to FFmpeg's own demuxer, so it
needs far less bespoke code - validate it against a real PS-emitting camera before
relying on it.

### The RTP branch

When the transport is RTP, each **channel-`0x01`** packet body is **one standard
RTP packet carrying H.265 (RFC 7798)**:

- 12-byte RTP header: `V=2`, **`PT=96`** (dynamic → H.265), seq, timestamp,
  **`SSRC=0x55667788`** (the recurring "magic" bytes are just the SSRC).
  Respect `CC` (CSRC count) and `X` (extension) - though observed video packets
  have `CC=0, X=0`, so payload starts at byte 12.
- Payload = HEVC NAL(s): **single** (type < 48), **AP** aggregation (48),
  **FU** fragmentation (49). Param sets: **VPS=32, SPS=33, PPS=34**. In practice
  the stream is mostly FU fragments plus periodic VPS/SPS/PPS.
- **Non-video RTP is present too** - e.g. `PT=112` packets with the extension
  bit set carry metadata/codec info; **skip anything where `PT != 96`.**
- **Encryption: supported (Image Encryption).** In practice, IPC cameras with Image
  Encryption on still deliver **MPEG-PS** video on channel `0x01`, with the first
  4096 bytes of each video NAL body **AES-ECB** encrypted using the per-camera
  verification code (zero-padded to 16 bytes). `decrypt.py` reverses this (§4.1 /
  `reference.md` B.10), so encrypted IPC cams work. The separate **transport E2EE**
  path (ECDH on `0x0A`/`0x0B`, `LethalEthan/LE-EZVIZ-VS`) is not implemented and not
  needed for these cameras.

### 4.1 De-packetizer (proven working)

Strip the RTP header, reassemble FUs, prepend Annex-B start codes → a clean HEVC
elementary stream FFmpeg reads directly (`-f hevc`). This exact logic produced a
decodable 2304×1296 stream:

```python
SC = b"\x00\x00\x00\x01"   # Annex-B start code

def depacketize(body, state):
    """body = one channel-0x01 packet payload (a full RTP packet).
    state = {'fu': None}. Returns bytes to append to the .h265 output (or b'')."""
    if len(body) < 14 or (body[0] >> 6) != 2 or (body[1] & 0x7f) != 96:
        return b""                                   # not H.265 video RTP
    cc = body[0] & 0x0f
    ext = (body[0] >> 4) & 1
    off = 12 + cc * 4
    if ext:
        if len(body) < off + 4:
            return b""
        extlen = int.from_bytes(body[off + 2:off + 4], "big")
        off += 4 + extlen * 4
    pl = body[off:]
    if len(pl) < 3:
        return b""
    t = (pl[0] >> 1) & 0x3f
    if t < 48:                                       # single NAL
        return SC + pl
    if t == 48:                                      # aggregation packet
        out, i = b"", 2
        while i + 2 <= len(pl):
            sz = int.from_bytes(pl[i:i + 2], "big"); i += 2
            out += SC + pl[i:i + sz]; i += sz
        return out
    if t == 49:                                      # fragmentation unit
        fuh = pl[2]
        s, e, ftype = fuh >> 7, (fuh >> 6) & 1, fuh & 0x3f
        frag = pl[3:]
        if s:                                        # start: rebuild NAL header
            b0 = (pl[0] & 0x81) | (ftype << 1)
            state["fu"] = bytes([b0, pl[1]]) + frag
        elif state["fu"] is not None:
            state["fu"] += frag
        if e and state["fu"] is not None:
            nal, state["fu"] = state["fu"], None
            return SC + nal
    return b""
```

The framing reader (pull VTM packets off the TCP socket):

```python
import struct
buf = b""; state = {"fu": None}
while streaming:
    buf += sock.recv(65536)
    while len(buf) >= 8 and buf[0] == 0x24:
        _, ch, length, seq, msg = struct.unpack(">BBHHH", buf[:8])
        if len(buf) < 8 + length:
            break
        body, buf = buf[8:8 + length], buf[8 + length:]
        if ch == 0x01:
            out_hevc.write(depacketize(body, state))
```

Verify with: `ffprobe -f hevc deck.h265` and
`ffmpeg -f hevc -i deck.h265 -frames:v 1 out.jpg`.

## 5. Operational realities (must handle)

- **Battery cams sleep.** The *first* stream request often returns **0 packets**
  while the cam wakes - **retry** (a second attempt ~seconds later succeeds).
  Requesting the stream is what wakes it (like opening the app).
- **~27 s connection drop.** EZVIZ tears the VTDU connection roughly every 27 s;
  implement a **reconnect loop** (re-run login-cache → handshake → resume).
  Session/token reuse is fine within their TTL; only re-login when needed.
- **On-demand, not 24/7.** Continuous streaming *destroys* battery runtime. The
  extension **must stream only while a client is watching** and stop on idle.
  (The `ESJavadex` add-on streams 24/7 - do **not** copy that for battery cams.)
- **KeepAlive** (`0x135`) may be needed for longer continuous sessions.

## 6. Extension architecture

**Decision: a HACS `custom_component` that serves each camera as a local,
token-guarded HTTP MPEG-TS stream which the bundled go2rtc pulls.** HA-managed go2rtc
will not run a go2rtc `exec:` source (it rejects `exec:` via its API as an insecure
producer, and its config allow-lists only the `ffmpeg` binary), so the originally
planned `exec:` approach was dropped. Instead the integration streams the camera
in-process, remuxes to MPEG-TS with FFmpeg, and exposes it from a Home Assistant HTTP
view; `stream_source()` returns that `http://` URL, which go2rtc pulls and fans out to
WebRTC/HLS. This keeps on-demand start/stop and browser transcoding with no subprocess
and no separate container.

**How it works (as built).**

- On the first viewer, the integration opens one cloud session for the camera
  in-process (async sockets, no subprocess), depacketizes RTP→HEVC or decrypts the
  MPEG-PS, and pipes it through FFmpeg (`-c copy`) to **MPEG-TS**.
- A `CameraBroadcast` fans that single session out to every consumer - go2rtc, the
  HLS `stream` component, and the snapshot - and stops it when the last viewer leaves
  (the battery-friendly behaviour of §5).
- It is served from a token-guarded HA HTTP view (`/api/ezviz_stream/<serial>`);
  `stream_source()` returns its `http://` URL. go2rtc pulls it and does the
  HEVC→H.264 transcode for WebRTC.
- Exposed as a camera entity, device-linked to the official integration (§6.3).

**Standalone add-on - not needed.** An earlier fallback plan (a container serving
HLS/RTSP where go2rtc `exec:` was unavailable) is moot: the HTTP-view approach works
on every install type with the bundled go2rtc, so no add-on ships.

### 6.1 The one real trade-off - HEVC vs H.264

The cams emit **H.265**. Browser live-view (Chrome, HA web UI) over WebRTC
generally needs **H.264**; HEVC only plays via HLS on Safari/iOS. So either:

- **Transcode HEVC→H.264 on demand** (works everywhere) - but this is a
  ~2304×1296 stream, so it's real CPU per viewer. Fine for one viewer; consider
  downscaling. FFmpeg `-c:v libx264 -preset veryfast -tune zerolatency`.
- **Keep native HEVC** (no CPU cost) - plays only in Safari / the iOS companion
  app. Acceptable if that's the user's client.
In practice go2rtc performs this HEVC→H.264 transcode automatically for WebRTC; an
explicit native-HEVC (no-transcode) config option is deferred.

### 6.2 Repo layout

This repo (`hass-ezviz-stream`) is a HACS custom integration; all stream logic lives
inside the integration package. There is no add-on - the bundled go2rtc is the media
hub and streaming runs in-process.

```
hass-ezviz-stream/
  custom_components/ezviz_stream/
    __init__.py       # entry setup, HTTP view registration, runtime data
    config_flow.py    # account + camera subentry flow (add/reconfigure/reauth)
    camera.py         # camera entity, snapshots, stream_source(); device-linked (§6.3)
    api.py            # cloud auth, discovery, VTDU token (hand-rolled)
    stream.py         # VTM/VTDU handshake + media loop; iter_annexb / grab_jpeg
    ysproto.py        # ysproto framing + RTP/RFC-7798 HEVC depacketizer (§4.1)
    decrypt.py        # AES-ECB Image-Encryption decryptor (one-shot + streaming)
    broadcast.py      # on-demand MPEG-TS broadcaster (one session, fanned out)
    stream_view.py    # token-guarded HTTP MPEG-TS view go2rtc pulls
    producer.py       # standalone CLI diagnostic (not used at runtime)
    manifest.json     # requirements: pycryptodome (no pyEzvizApi)
  doc/specification.md
```

Auth (login, discovery, VTDU token) is **hand-rolled in `api.py`** - there is no
runtime `pyEzvizApi` dependency (see the decisions block and §8).

### 6.3 Device-registry linking

We want our live-view camera to appear **on the same device card** as the official
`ezviz` integration's entities, without depending on or modifying it. In HA the
frontend groups entities onto a device card purely by the **`device_id`** stamped
on each entity's registry entry, and a registry entry's `config_entry_id` and
`device_id` are independent - so an entity *we* own can carry the `device_id` of a
device owned by another config entry. The task is just to get the right
`device_id` onto our entity's registry entry.

We create our *own* entity in our *own* config entry and give it a `device_info`
whose `identifiers` reuse the identity the official integration's device already
has:

```python
CameraEntity._attr_device_info = DeviceInfo(
    identifiers={("ezviz", serial)},   # SAME identifier the official integration uses
)
```

When HA registers an entity carrying `device_info`, it calls the device registry's
get-or-create with **our** config-entry id and those identifiers; because a device
with that identifier already exists, HA **merges** - it adds our config entry to
that device's owning set and stamps the device's id onto our entity. Our camera
then lands under the existing EZVIZ device. This uses only public, documented HA
APIs. Notes:

- **No hard dependency.** If the official integration isn't installed, HA simply
  creates a device from our `device_info` - we still work standalone. Do **not**
  add `ezviz` to `dependencies`/`after_dependencies` and do **not** read its
  `hass.data` (private, breaks across HA releases).
- **Own credentials.** We need EZVIZ cloud creds ourselves for the handshake (§3),
  so we ship our own config flow (§7) - we do not borrow the official
  integration's session.
- **Optional convenience.** The config flow *may* enumerate existing `ezviz`
  devices (via the public device/entity registry) to pre-fill the serial picker -
  a nicety, not a requirement.

> **Correction (2026-07-12).** Earlier drafts called this "the Powercalc pattern"
> and implied that project attaches via matching `device_info` identifiers. It
> does **not**: it resolves the *actual* target `DeviceEntry` (from a user-picked
> source entity/device) and binds our entity's `device_id` to it directly - for
> config-entry entities by setting the entity's device reference before
> registration (an HA-core *internal* attribute), and for YAML/platform entities
> by explicitly updating the entity-registry `device_id` afterwards. Crucially, it
> never adds its own config entry to the target device's owning set. Both routes
> reach the same visual result; we deliberately choose the **shared-identifier**
> route above because it is the clean public-API path and degrades gracefully to
> standalone. The full comparison of both techniques (with edge cases and the
> HA registry helpers involved) is in `reference.md` Part D.

## 7. Config & security

- Inputs: EZVIZ **account email + password**, **camera serial(s)**, **region**
  (default `"Europe"`/`ieu` for South Africa).
- **Never** commit credentials. In HA use `secrets.yaml` (gitignored); in a
  dev/scratch context use an untracked env file. The integration authenticates as
  the EZVIZ **PC/Studio** client against the user's own account - legitimate for
  the user's own devices (interoperability), but treat creds as sensitive.
- Cache the `sessionId`/tokens; don't re-login on every reconnect.

### 7.1 Authentication & 2FA

- **2FA (two-step verification) must be disabled** on the EZVIZ account for v1.
  This is the **same stance as the official `ezviz` integration**, which also does
  not support 2FA - so it is a familiar prerequisite for our users, not a novel
  limitation. Document it as a setup requirement in the README/config flow.
- If 2FA is left on, login returns an MFA challenge (observed code `6002`, see §3 /
  `reference.md` A.3) that v1 deliberately does not handle; the config flow should
  surface a clear "disable two-step verification" error rather than failing opaquely.
- **Fast-follow (not v1):** if `pyEzvizApi` exposes the SMS/verification-code step,
  add an MFA step to the config flow - the `Bobsilvio/ezviz_hp7` fork shows the
  SMS-code approach works against this same cloud API, and it would be a genuine
  differentiator over the official integration. Tracked in `TODO.md`.

### 7.2 Config-flow structure - account entry + camera subentries

**Decision (2026-07-13).** The **account is the config entry** (the higher-level
construct / hub); each **camera is a config subentry** under it. We add **our own
entities** via our own flow rather than injecting into the official-`ezviz` config
entries - piggybacking is brittle and **may become default HA behaviour** (if HA
core adopts cloud streaming), which would duplicate/conflict. Owning the account
entry keeps us self-contained and forward-compatible. (This does **not** change
§6.3 device-registry *linking* - our per-camera entities can still surface on the
official device's card via matching `device_info` identifiers.)

Structure:

1. **Account config flow (the entry).** Add the EZVIZ account with **username +
   password + region**; validated against the cloud. `unique_id` = account email;
   2FA must be off (§7.1). No cameras are chosen here.
2. **Camera subentry flow (per device).** From the account's **"Add camera"**, pick
   a streamable camera (those not already added) and supply **its own** verification
   code - optional; blank means the camera isn't encrypted. Each camera is a
   separate subentry (`unique_id` = serial, so it can't be added twice) and can be
   added/removed/reconfigured independently at any time. **Verification codes are
   per-camera - never assume a shared code.**

Config subentries require Home Assistant ≥ 2025.4, but the `hacs.json` floor is
**2026.3.0** - the first release on Python 3.14, which this code requires.
Implemented via `ConfigSubentryFlow` +
`ConfigFlow.async_get_supported_subentry_types`. A camera entity should detect
whether its stream is encrypted and only run the decryptor when needed (decrypting
a clear stream corrupts it) - see §4 and `reference.md` B.11.

## 8. Reference implementations

- `RenierM26/pyEzvizApi` - cloud auth/API + (in ≥1.0.4.8) a cloud-stream/decryption
  stack. We take **no runtime dependency** on it: HA core pins `pyezvizapi==1.0.0.7`
  (a hard `==`, pre-cloud) and HA loads one shared env, so any version we required
  would clash with the official `ezviz` integration. We keep it as a **dev-only**
  dependency - a decryption *oracle* our own decryptor is differential-tested against
  (`custom_components/ezviz_stream/decrypt.py`, `tests/test_ezviz_decrypt.py`). *(Reviewed 2026-07-13.)*
- **Neither the HA-core `ezviz` nor `RenierM26/ha-ezviz` (HACS) streams from the
  cloud** - both are **local-RTSP only** (the verification code is used as the RTSP
  password, not for video decryption). The cloud-stream/decryption code exists only
  in `pyezvizapi`'s CLI, unreleased in any integration. This is our niche; it may
  become native someday (see the config-flow decision in §7.2).
- `ESJavadex/ezviz-ha-addon` - reverse-engineered **cloud** connection + VTM/VTDU
  handshake (`ezviz-camera/ezviz_stream.py`); tested on HP2 battery cam. Streams
  raw channel-0x01 bodies to a pipe (does **not** RTP-depacketize - §4.1 is the
  missing piece).
- `LethalEthan/LE-EZVIZ-VS` - protocol/encryption/codec RE notes
  (`protocol.md`, `encryption.md`, `codecs.md`); needed if supporting the
  **encrypted** channel `0x0b`.

## 9. Build status

All of the below shipped and are live-verified (2026-07-14):

1. **Auth + handshake** - hand-rolled cloud login, discovery, VTDU token, and the
   VTM/VTDU handshake (`api.py`, `stream.py`); no runtime `pyEzvizApi` (§8), with
   cam wake-retry.
2. **De-packetize / decrypt** (§4.1) - RTP/RFC-7798 HEVC depacketizer (`ysproto.py`)
   and the AES-ECB Image-Encryption decryptor (`decrypt.py`), the latter also as an
   incremental streaming variant for continuous IPC live.
3. **Stream core** - continuous media with a reconnect loop across the ~27 s VTDU
   drop + KeepAlive, consumed in-process (no subprocess).
4. **Serve** - remux to MPEG-TS and expose a token-guarded HTTP view that go2rtc
   pulls (§6); go2rtc does the WebRTC HEVC→H.264 transcode.
5. **HA entity + config flow** - account entry + per-camera subentries with
   add / reconfigure / reauth and an on-save frame check; on-demand start/stop;
   snapshots (cached, retained across restarts).
6. **Remaining / nice-to-have:** 2FA login, a native-HEVC codec option, an MJPEG
   fallback, and multi-camera niceties.
