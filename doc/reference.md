# EZVIZ Cloud API & Streaming Reference

**Scope.** This is a technical reference for the EZVIZ cloud services this
integration depends on: the HTTPS **control plane** (authentication, device
discovery, tokens) and the binary **media plane** (the VTM/VTDU streaming
protocol, RTP/PS framing, HEVC de-packetization, and encryption). It closes with
an analysis of the Home-Assistant **device-registry linking** technique we use to
attach our camera to the existing EZVIZ device card.

This document is our own analysis, assembled from black-box observation of the
EZVIZ cloud protocol and public reverse-engineering of the wire format. It is a
reference companion to `specification.md` (the build spec) - where the two
overlap, the build spec's decisions win.

> **This document covers the private mobile/desktop app API only** (the path this
> integration uses). EZVIZ also publishes an official **Open Platform** developer
> REST API (`/api/lapp/*`, appKey/appSecret, `ezopen://` streaming) - a *different*
> surface. It is documented separately in [`open-platform.md`](open-platform.md),
> which also validates the assumptions below against the official docs.

> **Legend.** `<region>` = the routing code (e.g. `ieu`); serials, tokens,
> passwords are always treated as secrets and never logged. All multi-byte binary
> fields are **big-endian** unless stated otherwise.

---

## Part A - Control plane (HTTPS API)

The control plane is a conventional JSON-over-HTTPS API. Everything up to and
including obtaining a stream token happens here; only then do we open a raw TCP
socket for media (Part B).

### A.1 Two client personas

The EZVIZ backend behaves differently depending on which official client you
impersonate, and this choice is **load-bearing**:

| Persona | `clientType` | `clientNo` | What it unlocks |
|---------|-------------|-----------|-----------------|
| **Mobile app** | `3` | `web_site` | Standard mobile API surface; device list geared to app features. |
| **Desktop / Studio** | `9` | `shipin7` | The same control plane **plus** the VTM streaming resources needed for the cloud media path. |

Because our goal is the cloud stream, we present as the **desktop/Studio persona
(`clientType=9`)**. The mobile persona does not reliably surface the VTM routing
data in the device list.

Common client-identity fields sent as headers and/or form fields:

| Field | Value we use | Meaning |
|-------|-------------|---------|
| `clientType` | `9` | Client class (desktop). |
| `clientNo` | `shipin7` | Client channel identifier. |
| `appId` | `ys7` | Application id. |
| `customNo` | `1000001` | Customer/build number. |
| `clientVersion` | e.g. `2,5,1,2109068` | Emulated client version string. |
| `featureCode` | 32 hex chars | Per-terminal hardware fingerprint (see below). |
| `cuName` | base64 of a display name | Terminal name shown in the account's "terminal management" list. |

**`featureCode`.** A 32-character hex string identifying "this terminal". The
backend uses it for terminal binding and MFA. Three strategies exist in the wild:
a fixed all-zero string, a random-but-persisted value, and a value derived from
the host MAC (`md5(getnode())`). We should **generate once and persist** a stable
value per install - a churning `featureCode` looks like a new terminal on every
login and can trip the "too many logged-in terminals" limit.

**`cuName`.** Cosmetic but visible to the account owner - pick a clear brand
string so the user can recognise and revoke our terminal.

### A.2 Region routing

The API base host is `https://api<region>.ezvizlife.com`, where `<region>` is a
short routing code. Russia is special-cased onto a different TLD
(`*.ezvizru.com`).

| Region | Routing code | Notes |
|--------|-------------|-------|
| Europe / **Africa** | `ieu` | **South Africa routes here** - use region "Europe". |
| Asia / Singapore | `isgp` | |
| India | `iindia` | |
| North America / Oceania | `ius` | |
| South America | `isa` | |
| Russia | *(special)* | `api.ezvizru.com`, not the `api<code>` form. |

Two important routing behaviours:

- **The login response is authoritative.** The initial host is only a best guess.
  The login reply carries the account's real home node (an `apiDomain` /
  region-info block); adopt that host for all subsequent calls.
- **Region-redirect retry.** If login returns a "wrong region" status (observed
  code `1100`), the reply still contains the correct node. Re-point to it and
  retry login once. A robust client implements this fallback so users who don't
  know their region still succeed.

### A.3 Login

```
POST https://api<region>.ezvizlife.com/v3/users/login/v5
Content-Type: application/x-www-form-urlencoded

account=<email>&password=<md5-hex(password)>&featureCode=<fc>&cuName=<b64-name>
```

- **Password** is transmitted as a **single-round MD5 hex digest** of the
  plaintext - no salt. (This is weak, but it is what the backend expects; TLS is
  the only real transport protection.)
- **MFA / 2FA.** If the account has 2FA enabled, login returns a challenge status
  (observed code `6002`). The flow is: request an SMS/verification code
  (`POST /v3/sms/nologin/checkcode` with the account and a bind bizType), then
  re-issue the login with the code plus a `msgType`/`bizType`/`smsCode` triple.
  Our config flow must model this as a second step.
- **Login error codes** worth handling explicitly:

  | Code | Meaning |
  |------|---------|
  | `1012` | Invalid MFA code |
  | `1013` / `1226` | Incorrect username / credentials |
  | `1014` | Incorrect password |
  | `1015` | Account locked |
  | `1069` | Terminal-bind limit reached (prune terminals in the app) |
  | `1100` | Wrong region - retry against returned node |
  | `6002` | MFA required |

**What login returns.** A session block containing:

- `sessionId` - the primary bearer token. **It is a JWT** (see A.6).
- `rfSessionId` - a refresh token used to renew the session without a full
  re-login.
- A region/area block (`apiDomain`, area id/name, web domain) - the authoritative
  host.
- User and terminal-status blocks.

The `sessionId` is thereafter sent as an HTTP header named `sessionId` (and, on
some legacy endpoints, as a query/form parameter of the same name).

### A.4 Node discovery (server info)

Before streaming we need the auth/token node address. Under the desktop persona:

```
POST /api/server/info/get      (form: sessionId, clientType)
```

The reply (a `serverResp` object) enumerates backend nodes. The field that
matters for streaming is **`authAddr`** - the host we later hit for the VTDU
stream token (A.6). The same reply also carries a global VTM address, STUN
addresses (for P2P/NAT traversal - not used by our TCP path), and push/TTS nodes.

> A parallel endpoint (`GET /v3/configurations/system/info`) returns a
> pipe-delimited `sysConf` string used by the mobile persona for the CAS control
> channel (encryption-key retrieval, defence state). We do not need CAS for the
> unencrypted stream path, but it is where per-camera encryption keys come from if
> we later support encrypted streams (A.7).

### A.5 Device discovery (page list)

```
GET /v3/userdevices/v1/resources/pagelist?filter=VTM,STATUS&groupId=-1&limit=50&offset=0
    (sessionId + client identity as headers/params)
```

`filter` selects which sections the reply includes (comma-separated). For streaming
we need `VTM`; we also request `STATUS` for the encryption flag (below). A fuller
client requests many more sections (channels, switches, wifi, capabilities, etc.)
and paginates via a `page` block (`offset`, `limit`, `totalResults`, `hasNext`),
deep-merging pages.

The reply is a set of **sections, each a map keyed by `resourceId`**. The pieces
that matter to build a stream:

| Section / field | Use |
|-----------------|-----|
| `resourceInfos[]` → `deviceSerial`, `resourceId`, `resourceType` | Match the target serial; require `resourceType > 0` (a live channel). The `resourceId` keys the VTM lookup. |
| `resourceInfos[]` → `streamBizUrl` | A pre-baked `key=value&…` fragment ("biz") spliced into the stream URL verbatim. |
| `resourceInfos[]` → `videoLevel` | Default stream quality. |
| `VTM[resourceId]` → `externalIp`, `port` | **The VTM node to connect to** for this device. |
| `VTM[resourceId]` → `publicKey.{key,version}` | The VTM's ECDH public key (only needed for the encrypted path). |
| `deviceInfos[]` → `channelNumber` | Device channel index (the `chn` URL param). |
| `deviceInfos[]` → `supportExt` / `ezDeviceCapability` | Capability maps. Encryption support (and ECDH v2 support) is discoverable here - used to decide whether the stream will be encrypted. |
| `deviceInfos[]` → `status`, `deviceCategory` | Online state; battery-camera classification. |
| `STATUS[serial]` → `isEncrypt` | Image Encryption on/off (1/0). Requires `STATUS` in the filter. We surface it as `EzvizCamera.is_encrypted` and make the verification code required in the config flow when set. |
| `STATUS[serial]` → `encryptPwd` | The verification code's **double-MD5 hex** (`md5(md5(code).hex).hex`), the same hash used by the still-image envelope (B.10.2). Lets us validate an entered code without a frame grab. |

### A.6 VTDU stream token

The stream URL needs an opaque per-stream token, fetched from the **`authAddr`**
node (A.4), not the main API host:

```
GET {authAddr}/vtdutoken2?ssid=<sessionId>&sign=<sign>
```

- `ssid` is the full `sessionId` JWT.
- `sign` is **the `s` claim decoded from that JWT**. Because `sessionId` is a JWT,
  we base64url-decode its payload segment and read claim `s`. (No signature
  verification is required client-side - we only need to read the claim.)
- The reply is `{ tokens: [...], retcode }` with `retcode == 0` on success.
  **`tokens[0]`** is the value used as the `ssn=` parameter in the stream URL.

### A.7 Session lifecycle

- **Token is a self-describing JWT.** The `sessionId` carries `exp` (expiry) and
  `s` (the stream sign). We can check expiry locally without a round-trip.
- **Refresh before re-login.** When both the session and refresh tokens are
  cached, prefer refresh: `PUT /v3/apigateway/login` with the refresh token and
  `featureCode`. Success rotates **both** tokens; a `403` means the refresh token
  is dead → fall back to a full credential login.
- **Auto-relogin on 401.** Wrap API calls so a `401` triggers one login-and-retry
  (bounded, e.g. 3 attempts).
- **Cache and reuse.** Persist `{sessionId, rfSessionId, api host}` in the config
  entry so restarts renew rather than re-authenticate. Do not log in on every
  reconnect - reuse the session within its TTL.
- **Logout** (optional, polite): `DELETE /v3/users/logout/v2`.

### A.8 Other useful control-plane endpoints

Not required for the basic unencrypted stream, but relevant to features on the
roadmap:

- **Camera encryption key** - `POST /api/device/query/encryptkey` returns the
  per-camera key used to decrypt an **encrypted** stream (the user's image/video
  encryption password). `GET /v3/devconfig/authcode/query/{serial}` returns the
  sticker verification code. `PUT /v3/devices/encryptedInfo/risk` toggles/change
  encryption.
- **Wake / alarm** (battery-camera wake, whistle, siren) -
  `POST /api/device/cancelAlarm`, `…/sendAlarm`, `…/alarm/*`. Useful because a
  sleeping battery camera often needs a nudge before the first stream request
  succeeds.
- **PTZ / capture** - `PUT /v3/devices/{serial}/ptzControl`,
  `PUT /v3/devconfig/v1/{serial}/{channel}/capture`.
- **Switches** - `PUT …/switchStatus` toggles privacy, sleep, all-day recording,
  auto-sleep, etc.
- **Alarms / messages** - `GET /v3/alarms/v2/advanced`,
  `GET /v3/unifiedmsg/list` for event history. See A.8.1 for the last-motion image.
- **Push (MQTT)** - a separate app-key/secret channel for real-time
  notifications.

### A.8.1 Last-motion / alarm image (battery-friendly thumbnail)

A camera's most recent motion event carries a still image **already captured and
stored in the cloud** - retrievable *without waking the camera or opening a media
session*. That makes it an ideal thumbnail for a sleeping battery camera: polling a
live frame wakes the camera on every cache expiry and drains it, whereas this image
is free (already server-side) and refreshes whenever the camera detects motion.
Trade-off: it is *last motion*, not *live*, so it can be stale between events.

- **List endpoint:** `GET /v3/alarms/v2/advanced` with query params
  `deviceSerials=<serial>&queryType=-1&limit=1&stype=-1` returns the most recent
  alarm(s) (pyezvizapi `get_alarminfo(serial, limit=1)`; meta code `200` = OK,
  `500` = server busy → retry). `limit=1` gives just the latest.
- **Response:** an `alarms[]` list; each item carries a **`picUrl`** (the stored
  snapshot - an HTTPS URL on an EZVIZ/OSS host), a `picChecksum`, and event
  metadata (time, type).
- **Fetch:** a plain HTTPS `GET picUrl` - no VTDU token, no handshake, no wake. The
  bytes are a JPEG, possibly wrapped in the still-image encryption envelope
  (B.10.2) when the camera has Image Encryption on.
- **Decrypt:** if the payload contains the marker `hikencodepicture`, decrypt per
  B.10.2 with the verification code; otherwise use the bytes verbatim (pyezvizapi
  guards on `HIK_ENCRYPTION_HEADER in image_data` before decrypting).

---

## Part B - Media plane (streaming protocol)

Once we have a VTM node (A.5) and a stream token (A.6), media is obtained over a
**custom binary TCP protocol** - not RTSP, not plain RTP. The backend calls the
two node roles **VTM** (Video Transmission Management - effectively a load
balancer) and **VTDU** (Video Transmission Data Unit - the node that actually
pushes media). We talk to the VTM, it redirects us to a VTDU, and the VTDU streams.

### B.1 Frame format ("VTM packet")

Every message in both directions, on both sockets, is a fixed **8-byte header**
followed by a body:

```
 byte 0    1        2   3        4   5        6   7
+--------+--------+--------+--------+--------+--------+--------+--------+
| 0x24   | chan   |   length (u16) |  sequence (u16) | msg code (u16)  |
+--------+--------+--------+--------+--------+--------+--------+--------+
| body: `length` bytes follow immediately                             |
```

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| 0 | 1 | Magic | Always `0x24` (`$`) - the sync byte to hunt for during TCP reassembly. |
| 1 | 1 | Channel | Selects message-vs-stream and plaintext-vs-encrypted (B.2). |
| 2 | 2 | Length | Body length. |
| 4 | 2 | Sequence | Present but, in practice, outbound sequence can be left `0`. |
| 6 | 2 | Message code | The opcode (B.3). |

This resembles RTSP interleaved framing, but with added sequence and opcode
fields and **protobuf** bodies (rather than RTCP).

**TCP reassembly.** Bodies routinely span TCP segments. Read the 8-byte header,
then loop until `length` body bytes are collected. Any run of bytes that does not
begin with the `0x24` magic is treated as non-protocol/continuation data and
skipped rather than being fatal.

### B.2 Channels

| Channel | Carries | Encrypted |
|---------|---------|-----------|
| `0x00` | Control / signalling messages | No |
| `0x01` | **Media stream** | No |
| `0x0A` | Control / signalling messages | Yes (E2EE) |
| `0x0B` | **Media stream** | Yes (E2EE) |

Modern cameras default to the **encrypted** channels; older models (and cameras
with image encryption disabled) use the plaintext `0x00`/`0x01` pair. **Our v1
targets the unencrypted path (`0x00`/`0x01`)** and requires image encryption to
be off; the encrypted path is B.7.

### B.3 Message codes (opcodes)

The full opcode space is large (playback, seek, pause/resume, speed, ECDH
notify, etc.). The ones exercised by a live "watch now" stream:

| Opcode | Name | Use |
|--------|------|-----|
| `0x13B` | **StreamInfoReq** | Ask a node to start a stream (sent to VTM, then VTDU). |
| `0x13C` | **StreamInfoRsp** | Reply - carries the VTDU redirect and stream metadata. |
| `0x132` | **KeepAliveReq** | Keep the VTDU session alive. |
| `0x133` | KeepAliveRsp | Keep-alive acknowledgement. |

Other opcodes seen but not needed for basic live view include start/stop stream
(`0x12E`–`0x131`), playback (`0x137`–`0x13A`), and an ECDH-notify used by the
encrypted handshake (`0x14A`).

> **Verified (2026-07-13, body format corrected 2026-07-15).** `0x132` on channel
> `0x00` carrying the `streamssn` (StreamInfoRsp field 4) is the working keep-alive
> opcode, and it is **required**, not optional: on an RTP camera, without a periodic
> keep-alive the media stalls after the initial parameter sets (only ~18 packets
> arrive); with it, hundreds of packets/second flow. This *corrects* the earlier
> "keep-alive is unreliable, prefer reconnect" guidance (B.11) - you need **both**.
> **The body must be the `streamssn` wrapped as protobuf field 1, NOT the raw string**
> (see the 2026-07-15 capture below); the raw form is fatal.
>
> **Keep-alive body format (verified 2026-07-15, packet capture).** The keep-alive
> body must be the `streamssn` **wrapped as protobuf field 1** (`0a <len> <ssn>`),
> exactly as the official client sends it. Sending the raw string is fatal: the VTDU
> replies with a **FIN ~0.2 s later** and closes the session. This was the cause of
> severe live-view buffering and the day/night ("grayscale-to-colour") flip - the
> session churned every **~5.5 s** (media flowed ~5 s, we sent one raw-string
> keep-alive, the server closed ~0.5 s after it). Byte diff of the two keep-alive
> frames - identical 8-byte header (magic `24`, channel `00`, opcode `0132`), only
> the body differs:
>
> | Sender | Body | Frame bytes |
> |--------|------|-------------|
> | Official client (session lived 141 s) | `0a 33` + `<51-byte ssn>` | 61 |
> | Our old (raw) form (FIN after ~5.5 s) | `<51-byte ssn>` | 59 |
>
> `0a` = protobuf tag (field 1, wire type 2), `33` = length 51. The official client
> sends it **every ~10 s** and **increments the frame sequence** each time (`seq` =
> 1, 2, 3, …). Evidence: `scripts/in/EzViz_Capture_Full.pcapng` (official app: one
> **141 s** session on `104.166.134.53:6002`, keep-alive every 10 s, accepted) vs
> `EzViz_Capture_HA.pcapng` (our old client: sessions dying at ~5.5 s, keep-alive
> never acked, immediate FIN). Both use the identical cloud `ysproto` VTDU path - the
> only difference was this 2-byte protobuf wrapper.
>
> **No I-frame / force-IDR opcode is known.** A sweep of `0x130`–`0x145` (excl. the
> five known opcodes), each sent on channel `0x00` with the `streamssn` body ~1.5 s
> into a fresh session, produced **no** on-demand keyframe on an IPC camera (see the
> IPC/GOP finding in B.11). The real opcode must be recovered by capturing the
> official EZVIZ client (`scripts/parse_ysproto_pcap.py`) - still outstanding.
>
> **First real-client capture (2026-07-13): the IPC cams took the LAN P2P path.**
> In `scripts/in/EzViz_Capture.pcapng` the app streamed the powered IPC cams
> **directly over the LAN** (phone ↔ camera `192.168.68.55`, ctrl port 9010 /
> media 9020) using EZVIZ's private P2P protocol (magic `9e ba ac e9`, XML-
> negotiated, stream opcode `0x3105`/`0x3106`) - **not** cloud `ysproto`. Only the
> two **BatteryCamera** cams went via the cloud VTM/VTDU (RTP/HEVC), so the parser
> saw only them. The one unknown **cloud** opcode, **`0x130`**, is **stream-stop/
> teardown** (start/stop range above; sent last after the keepalives; `streamssn`
> body; already swept → no IDR), not a force-IDR - the client sent no force-IDR on
> these cams. Handshake confirmed: StreamInfoReq to the VTM (`:8554`) then the VTDU
> (`:600x`); keep-alive `0x132`/`streamssn`.
>
> The LAN IPC media (`0x3106`) does contain a real **SPS+PPS+IDR** cluster, so the
> IPC cam **emits keyframes on a fresh stream start** - the cloud IPC problem is
> about how the **VTDU relays** the stream (a persistent/shared device GOP joined
> mid-stream, plus the ~27 s drop), not the cam withholding IDRs. To find any cloud
> force-IDR opcode (if one exists), re-capture with the phone **off the LAN**
> (cellular only) so an IPC cam is forced through the cloud path.

### B.4 The handshake

```
 client                     VTM (externalIp:port)                 VTDU
   │  TCP connect ───────────────▶│                                 │
   │  StreamInfoReq (0x13B) ──────▶│   body: ysproto:// URL          │
   │◀──── StreamInfoRsp (0x13C) ───│   body: VTDU ysproto:// + key   │
   │  parse VTDU ip:port + vtmstreamkey                              │
   │                                                                 │
   │  TCP connect ─────────────────────────────────────────────────▶│
   │  StreamInfoReq (0x13B, + vtmstreamkey) ───────────────────────▶│
   │◀──── StreamInfoRsp (0x13C) ─────────────────────────────────────│
   │  KeepAlive (0x132, body = streamssn as pb field 1, seq++) ────▶│
   │◀═════ channel 0x01 media packets ═══════════════════════════════│
```

1. Connect to the VTM (`externalIp:port` from the page list).
2. Send **StreamInfoReq** with a `ysproto://` stream URL in the body.
3. The VTM replies with **StreamInfoRsp** whose body contains a **redirect
   `ysproto://` URL** pointing at the assigned VTDU, plus a `vtmstreamkey`.
4. Connect to that VTDU and send **StreamInfoReq** again - same URL, now also
   including the `vtmstreamkey`.
5. The VTDU replies with StreamInfoRsp (result code, stream session id, and
   encryption-related fields), then begins pushing media on **channel `0x01`**.

### B.5 Message bodies (hand-rolled protobuf)

The bodies are protobuf, but a minimal hand-rolled encoder/decoder suffices - we
only touch a handful of fields.

**StreamInfoReq** - fields we populate:

| Field # | Type | Meaning | When set |
|---------|------|---------|----------|
| 1 | string | `streamurl` - the `ysproto://` URL | Always |
| 2 | string | `vtmstreamkey` - key returned by VTM | Only on the VTDU request |
| 3 | string | client/user-agent version tag | Always |
| 4 | int32 | proxy type (`0`) | Always |
| 6 | string | client version tag | Always |

**StreamInfoRsp** - fields we read:

| Field # | Type | Meaning |
|---------|------|---------|
| 1 | int32 | `result` - `0` = OK, else an error (B.9) |
| 4 | string | `streamssn` - stream session id (sent in each keep-alive, wrapped as protobuf field 1) |
| 5 | string | `vtmstreamkey` - key to present to the VTDU |
| 7 | string | `streamurl` - **the VTDU redirect URL** |
| 9 | string | `aesmd5` - MD5 bound to the AES stream key (encrypted path) |
| 11 | string | `peerpbkey` - server ECDH public key (encrypted path) |

> A pragmatic shortcut for the VTM step: rather than fully protobuf-decoding the
> reply, scan the body for the literal `ysproto://` and parse the VTDU host/port
> out of it. Full decoding is only needed once we consume the encryption fields.

### B.6 The `ysproto://` stream URL

The stream URL is where the device serial, channel, token, and "biz" fragment are
carried:

```
ysproto://<ip>:<port>/live?dev=<serial>&chn=<channel>&stream=1&cln=<clientType>
    &isp=0&auth=1&ssn=<vtdu-token>&<streamBizUrl>&vip=0&timestamp=<epoch-ms>
```

| Param | Meaning |
|-------|---------|
| `dev` | Device serial |
| `chn` | Device channel number (from the page list) |
| `stream` | Stream type - `1` = main stream |
| `cln` | Client type (`9`) |
| `isp` | `0` |
| `auth` | `1` |
| `ssn` | The VTDU token (`tokens[0]` from A.6) |
| `<streamBizUrl>` | The device's "biz" fragment, spliced in raw |
| `vip` | `0` |
| `timestamp` | Unix epoch milliseconds |

**Key ordering matters** - the query string is assembled in a fixed order rather
than being sorted, so build it manually. Additional parameters exist for other
modes: `e2ee=1` (encrypted channel), `weakstream=1`, `begin/end/seg` (playback),
and a `ysudp://` scheme with `linkid=` for UDP transport (not used here).

### B.7 Media framing on the stream channel

The body of each channel-`0x01` packet is **not always the same container** - it
varies by camera model and firmware. Auto-detect from the first bytes:

| First bytes | Transport |
|-------------|-----------|
| `00 00 01 BA` | MPEG-PS (pack header) - carries video **and** audio |
| `0x47` | MPEG-TS |
| version bits `10` in byte 0 | **RTP** (RFC 3550), dynamic HEVC |
| *(other)* | MPEG-4 |

Roughly: **newer models emit RTP/H.265; a large set of (mostly older/battery)
models emit MPEG-PS.** MPEG-PS is the only container observed to carry audio; the
RTP models have been video-only in practice. This container variance is the single
biggest portability risk in the pipeline - our decode path must branch on the
detected transport, not assume RTP.

> **Verified (2026-07-13).** On our 4-camera EU test account the split ran by
> *camera class*, not simply age: the two **battery cameras emit RTP/H.265**
> (`PT=96`), the two **mains-powered IPC cameras emit MPEG-PS carrying H.264**
> (`00 00 01 BA` pack headers; ffprobe confirms H.264). Both battery-cam RTP
> streams decoded end-to-end to real JPEGs (2304×1296 and 1280×720). Detection by
> first-bytes works, **but a reconnected session can start mid-PES** (no leading
> pack header) and mis-detect as *unknown* - so lock the transport to the first
> clearly-detected value across reconnects rather than re-detecting each session.

#### RTP header (12 bytes fixed)

```
 0               1               2               3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|V=2|P|X| CC  |M|     PT      |       sequence number         |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                          timestamp                            |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                             SSRC                              |
+---------------------------------------------------------------+
| optional CSRC (CC×4)  |  optional extension (if X=1)          |
+---------------------------------------------------------------+
```

- `V` (version) = 2. `P` (padding), `X` (extension), `CC` (CSRC count), `M`
  (marker = last packet of a frame), `PT` (payload type).
- **Payload type selects codec/stream:** `PT == 96` is the dynamic **HEVC video**
  payload - the branch we depacketize. A range of other PTs (`0`, `8`, `11`,
  `100`, and a scattering of dynamic values) map to audio and are currently
  ignored. **Skip anything where `PT != 96`.**
- Respect `CC` and `X`: payload starts at `12 + CC*4`, plus the extension block if
  `X` is set (extension length is counted in 32-bit words). In observed video
  streams `CC=0` and `X=0`, so payload usually starts at byte 12 - but a correct
  parser honours both. The extension, when present, appears to carry
  codec/profile info set at stream start.

### B.8 HEVC de-packetization (RFC 7798)

RTP payloads **omit** the Annex-B start code, so we prepend it. The HEVC NAL type
is the 6-bit field `(payload[0] >> 1) & 0x3F`:

| NAL type | Meaning | Handling |
|----------|---------|----------|
| 32 | VPS | Parameter set - emit as a single NAL |
| 33 | SPS | Parameter set - emit as a single NAL |
| 34 | PPS | Parameter set - emit as a single NAL |
| < 48 | Single NAL unit | Prepend start code, emit whole payload |
| 48 | Aggregation packet (AP) | Split the 2-byte-length-prefixed sub-NALs, emit each with a start code |
| 49 | Fragmentation unit (FU) | Reassemble across packets (below) |

**Fragmentation unit reassembly.** The FU header sits at `payload[2]`; its top bit
marks the **start** fragment and the next bit the **end**. On the start fragment,
reconstruct the original 2-byte HEVC NAL header from the FU type bits plus the
outer header, prepend a start code, and begin accumulating. Continuation fragments
append their payload (from byte 3). On the end fragment, flush the completed NAL.

> Reverse-engineered FU/AP handling seen in the wild is often approximate (partial
> NAL-header reconstruction, missing AP support). Our de-packetizer implements the
> full RFC-7798 single/AP/FU cases - that logic is the core contribution and is
> documented in `specification.md §4.1`. The output is a clean Annex-B HEVC
> elementary stream FFmpeg reads directly with `-f hevc`.

> **Timing: the RTP timestamp is the only presentation clock.** The depacketized
> Annex-B elementary stream carries **no timing** of its own - fed to `ffmpeg -f hevc`
> raw, FFmpeg assumes a default 25 fps and runs the playback clock too fast (the buffer
> drains and the player rebuffers). The RTP header's 32-bit **timestamp (90 kHz)** is
> the camera's real presentation clock, so downstream must supply timing from it. We
> pace released frames to that timestamp (rebasing to "now" on a reconnect's fresh RTP
> base or a 32-bit wrap), which reproduces the true capture cadence. MPEG-PS does not
> need this - it carries its own PTS.

### B.9 Codecs & container summary

- **Video:** H.265/HEVC (RTP models) and H.264/AVC and HEVC inside MPEG-PS.
  Observed resolutions around 1080p–1296p at ~15 fps. *(Verified 2026-07-13:* the
  RTP→HEVC path decodes end-to-end - real 2304×1296 and 1280×720 JPEGs off battery
  cams; MPEG-PS on IPC cams is confirmed H.264 but blocked on the keyframe interval,
  see B.11.*)*
- **Audio:** present in MPEG-PS streams; RTP audio is unresolved (video-only in
  practice, likely G.711 when present).
- **Downstream:** the reconstructed elementary stream is remuxed to MPEG-TS with
  FFmpeg (`-c copy`) and handed to go2rtc, which does the **HEVC→H.264 transcode**
  for the browser (WebRTC) on demand. A native-HEVC (no-transcode) path for
  Safari/iOS is a possible config option. MPEG-PS is remuxed the same way, and its
  PTS is preserved.

### B.10 Encryption

Two independent layers exist. The **payload (Image Encryption)** layer is
**implemented** (see layer 2); the **transport E2EE** layer is documented for
completeness but is not implemented and is not needed for the cameras we support.

1. **Transport E2EE (link to VTM/VTDU).** Present when the device advertises ECDH
   support. Uses **ECDHE on curve P-256 (secp256r1/prime256v1)**: we generate an
   ephemeral keypair and send our public key; the peer's public key arrives via
   the page list (`VTM.publicKey`) or the StreamInfoRsp (`peerpbkey` / a pds
   list). Traffic then rides channels `0x0A`/`0x0B` and the stream URL carries
   `e2ee=1`.
2. **Payload encryption (the "image/video encryption" password).** Believed to be
   **AES-128**. The related still-image scheme is known: a marker header plus a
   double-MD5 of the password, discard a 48-byte head, then AES-CBC with a static
   IV and PKCS5 unpadding. Whether video reused the still-image scheme was originally unconfirmed; our
   implementation (`decrypt.py`) **confirms** it for **MPEG-PS video**: the first
   4096 bytes of each video NAL body are **AES-ECB** with the verification code
   zero-padded to 16 bytes (no IV), and the media stays on channel `0x01`. StreamInfoRsp exposes `datakey` and
   `aesmd5` that likely bind/verify the AES key; devices also expose a
   "permanent key" thought to derive (not be) the stream key.

#### B.10.1 MPEG-PS Image-Encryption byte structure (as implemented)

Decrypting correctly - and incrementally, on a live stream - depends on framing rules
that are not obvious from "AES-ECB the first 4096 bytes of each NAL":

- **The cipher stream is the concatenation of the video-PES *payloads*, not the raw
  container.** AES 16-byte blocks accumulate **across** contiguous video-PES packets;
  the MPEG-PS framing bytes between payloads (pack/PES headers) are skipped, never fed
  to the cipher. A single NAL body routinely spans several video-PES packets.
- **A "run" is the atomic unit, and it resets on a non-video PES packet.** Metadata
  packets (pack header `BA`, system header `BB`, program-stream-map `BC`, padding
  `BE`) do **not** break a run; an audio / private PES (`C0`-`DF`, `BD`, `BF`) does.
  The 4096-byte counter and the AES-block alignment reset at each run boundary.
- **The 4096-byte encrypted prefix is per NAL**, measured from
  `<start-code> + nalu_header_size`; the counter resets at every NAL start code found
  within the run.
- **`nalu_header_size`** (clear codec-header bytes kept before the AES region) is **2**
  for HEVC, **1** for H.264 with a clear NAL header, and **0** for H.264 whose NAL
  header is itself encrypted (observed on our IPC cams). It is auto-detected by
  trial-decrypting candidate NAL headers and scoring which interpretation yields the
  most plausible headers.
- **Consequence for live decryption:** a streaming decryptor can only cut and emit at a
  **video-PES run boundary** (where the AES state resets); the still-open last run must
  be buffered. `decrypt.StreamingPsDecryptor` does exactly this and is validated
  byte-for-byte against the one-shot `decrypt_ps_video` across arbitrary chunk splits.

**Recommendation (updated 2026-07-13):** encryption support is **proven**, not a
research item - decrypt on `0x01` with the verification code (AES-ECB; see B.11).
The config flow should collect the verification code for any encrypted cam and
auto-detect whether a stream is encrypted (decrypting a clear stream corrupts it).

#### B.10.2 Still-image (alarm snapshot) encryption

A **separate** scheme from the video payload one (B.10.1). It applies to stored
**still images** - the alarm/motion snapshots behind `picUrl` (A.8.1) - when the
camera has Image Encryption on. Per pyezvizapi `decrypt_image` (`utils.py`):

- **Envelope marker:** the ASCII bytes `hikencodepicture` (16 B). If the payload
  does not contain it, the image is plaintext - return as-is. Any preamble before
  the first marker is trimmed. Several `hikencodepicture` blocks can be
  concatenated in one file; each is decrypted independently and the parts joined.
- **Per-block layout:** `hikencodepicture` (16 B) + **password hash** (32 B, ASCII
  hex) + **AES-CBC ciphertext**.
- **Password check:** the 32-byte hash must equal `md5(md5(code).hexdigest())` -
  double MD5, hex-encoded, ASCII (`return_password_hash` in pyezvizapi). A mismatch
  means the wrong verification code.
- **Cipher:** **AES-128-CBC**. Key = the verification code zero-padded (`\x00`) to
  16 bytes then truncated to 16. **Static IV** =
  `30 31 32 33 34 35 36 37 00 00 00 00 00 00 00 00` (ASCII `"01234567"` + eight
  nulls). The ciphertext is truncated to a 16-byte multiple before decrypt; the
  final block is **PKCS#7-style** unpadded (last byte = pad length).

Contrast with video (B.10.1): video is **AES-ECB, no IV, first 4096 B per NAL**;
still images are **AES-CBC, fixed IV, whole payload, envelope + hash**. Same
verification-code-derived key family, different mode and framing - so a still-image
decryptor is a **new, small helper**, not reusable from `decrypt.py`.

#### B.10.3 Audio (AAC) encryption (verified 2026-07-17)

The **audio** track of an encrypted stream is encrypted the same way as video, keyed
on the same verification code - only the "clear framing header" differs:

- Each audio PES (`C0`-`DF`) carries **one ADTS AAC frame** (its `frame_length` equals
  the PES payload length). The **ADTS header stays clear** - 7 bytes normally, 9 when
  the CRC-present bit (`byte1 & 0x01 == 0`) is set - and the **AAC body after it is
  AES-ECB encrypted** (whole 16-byte blocks; a trailing partial block stays clear).
- Unlike video, it is **per-frame**, not accumulated across PES: each frame's body is
  its own independent ECB region (the ADTS header is the per-frame clear prefix, the
  audio analogue of `nalu_header_size`). No 4096-byte cap (frames are far smaller).
- Same AES-ECB key as video (verification code, zero-padded/truncated to 16 B).
- **How it was cracked:** the Deck-camera trace
  `EzViz_Capture_Replay_SD_Unec_Enc.pcapng` - the verification code decrypts the Deck
  **video** cleanly (proving the key), then audio with ECB + clear ADTS header decodes
  with **0 errors over 657 frames / 3 sessions**. Implemented as
  `decrypt_stream.decrypt_ps_audio`; `StreamingPsDecryptor(decrypt_audio=True)` applies
  it after the video pass (default off preserves the video oracle invariant).
- **Gotcha:** an audio-PES scanner must advance past each PES by its length; skipping
  only a few bytes lets it scan **into** a video payload and misread Annex-B NAL start
  codes as audio PES. Cameras with audio disabled have no audio PES - it is a no-op.

### B.11 Operational realities

- **Battery cameras sleep.** The *first* stream request often returns few/zero
  video packets while the camera wakes; the act of requesting is what wakes it.
  Retry. In practice an RTP battery cam took **~2 sessions** to yield a keyframe -
  the first session brought only the parameter sets (VPS/SPS/PPS), the second (a
  full ~27 s window) carried a decodable keyframe.
- **~27–30 s VTDU drop.** The VTDU tears the connection roughly every half-minute
  (**observed on every session** across all cameras). Implement a **reconnect
  loop** (reuse cached session/token → re-handshake → resume) and, at the serving
  layer, use discontinuity-tolerant segmenting so the viewer doesn't see a hard
  stop. Note: reconnected sessions are independent live streams and **cannot be
  byte-spliced** into one file (each may start mid-frame/mid-PES).
- **Keep-alive is required, not optional - and its body must be protobuf-wrapped.**
  *(Corrected 2026-07-13; body format corrected 2026-07-15.)* Earlier guidance said
  keep-alive was unreliable; in fact `0x132` carrying the `streamssn` is what keeps
  media flowing - without it an RTP stream stalls after the parameter sets. But the
  body must be the `streamssn` **wrapped as protobuf field 1** (`0a <len> <ssn>`), as
  the official client sends it every ~10 s (seq incrementing); sending the **raw**
  string makes the VTDU FIN the connection ~0.2 s later, which churns the session
  every ~5.5 s and is what caused heavy live-view buffering and the day/night flip
  (see B.3). Use keep-alive **and** the reconnect loop; they solve different problems.
- **IPC cameras: the blocker was video encryption, not GOP length - now solved.**
  *(Resolved 2026-07-13.)* Earlier we read the IPC failure as a long keyframe
  interval on the **main** stream (a ~170 s sweep saw a single IDR). The fix has two
  parts, both proven live:
  1. **Substream (`stream=2`)** - delivers **SPS+PPS+IDR in every ~5 s session**
     (verified by the probe's per-session NAL census), so one VTDU session carries a
     full keyframe. The main stream's minutes-long GOP does not.
  2. **Decrypt the video** - the MPEG-PS container and PPS are in the clear, but the
     **VCL slice NALs are AES-encrypted**. This is **EZVIZ Image Encryption**. The
     scheme (per `pyezvizapi.stream.decrypt_hikvision_ps_video`): **AES-ECB, no IV**,
     key = the **verification code** `.encode()` zero-padded/truncated to **16 B**;
     only the first `4096` B of each video NAL body is encrypted, starting after the
     `nalu_header_size` codec-header bytes (**0** for these H.264 cams - the NAL
     header itself is encrypted - vs `2` for HEVC; auto-detected by scoring trial
     decrypts). Decrypting before FFmpeg yields clean **H.264 768×432**.
  Net path: **substream + decrypt with the verification code**. Battery (RTP/HEVC)
  cams need neither - their streams are in the clear. Alternatively a user can
  **disable Image Encryption** on the device for a clear stream. Tooling:
  `ezviz_stream_probe.py --stream 2 --verify-code <code>` (or `EZVIZ_VERIFY_CODE`).
- **Account concurrency is limited, and churn is not the cap.** A burst of
  simultaneous session opens - a dashboard cold-loading many snapshots at once - trips
  `5405` (signalling/CAS timeout), a *churn* symptom that clears once requests are
  spaced or serialised, well before the true per-account / per-VTDU concurrency ceiling
  (`5504`/`5546`, B.12). Share **one** session per camera (fanned out to all viewers),
  rate-limit snapshot grabs, and treat `5504`/`5546` - not `5405` - as the signal that
  the account is genuinely out of stream slots.
- **On-demand only.** Continuous streaming destroys battery runtime. Stream **only
  while a client is watching** and stop on idle (e.g. a short idle timeout after
  the last viewer disconnects) - never 24/7.

### B.12 StreamInfoRsp error codes (`result` field)

| Code | Meaning |
|------|---------|
| `5404` | Device offline |
| `5405` | Signalling/CAS timeout |
| `5406` / `5411` / `5412` / `5413` | Token/session invalid |
| `5409` | Privacy protection on |
| `5416` | Resources limited |
| `5451` | Stream type unsupported |
| `5452` | Device→stream-server link failed |
| `5457`–`5459` | E2EE / ECDH negotiation failures |
| `5491` | Duplicate request rejected |
| `5503` | VTM could not allocate a VTDU |
| `5504` / `5546` | VTDU / user concurrency limit |
| `5544` | No video source |
| `6518` | Packet too large |
| `6519` / `6520` | Unstable network |
| `7005` | VTDU disconnected |

---

## Part C - Process flow: authorisation → login → stream URL → play

```mermaid
sequenceDiagram
    autonumber
    participant U as User / HA client
    participant I as Integration
    participant API as EZVIZ API (HTTPS)
    participant VTM as VTM node
    participant VTDU as VTDU node
    participant FF as FFmpeg + go2rtc

    Note over U,I: Authorisation (config flow, once)
    U->>I: email + password + region (+ camera serial)
    I->>API: POST /v3/users/login/v5 (md5 password)
    alt MFA required (6002)
        API-->>I: challenge
        I->>API: request SMS code + re-login with code
    end
    alt wrong region (1100)
        API-->>I: correct node
        I->>API: retry login on returned host
    end
    API-->>I: sessionId (JWT) + refresh token + home node

    Note over I,API: Discovery + token (per stream start)
    I->>API: POST /api/server/info/get  → authAddr
    I->>API: GET /v3/userdevices/.../pagelist?filter=VTM
    API-->>I: VTM ip:port, resourceId, channel, biz, caps
    I->>API: GET {authAddr}/vtdutoken2?ssid=&sign=(JWT.s)
    API-->>I: tokens[0]

    Note over I,VTDU: Build stream URL + media handshake
    I->>I: build ysproto:// URL (dev, chn, ssn=token, biz, ts)
    I->>VTM: StreamInfoReq (0x13B)
    VTM-->>I: StreamInfoRsp (0x13C) → VTDU redirect + key
    I->>VTDU: StreamInfoReq (0x13B, + vtmstreamkey)
    VTDU-->>I: StreamInfoRsp (0x13C, result=0)

    Note over I,FF: Play (on-demand, reconnecting)
    loop until client stops watching
        VTDU-->>I: channel 0x01 media packets (RTP/PS)
        I->>I: detect transport; depacketize → Annex-B HEVC
        I->>FF: elementary stream (HEVC→H.264 transcode)
        FF-->>U: WebRTC / HLS / RTSP
        Note over I,VTDU: ~27s drop → reconnect (reuse session/token)
    end
```

**Condensed pipeline:**

```
login (region-aware, MFA-aware)
  → sessionId JWT
  → server info      → authAddr
  → page list (VTM)  → VTM ip:port + resourceId + channel + biz
  → vtdutoken2       → stream token
  → build ysproto:// URL
  → StreamInfoReq → VTM  → VTDU redirect
  → StreamInfoReq → VTDU → media on channel 0x01
  → detect transport (RTP / MPEG-PS) → depacketize → Annex-B HEVC
  → FFmpeg (HEVC→H.264) → go2rtc → HA camera (start on watch, stop on idle)
```

---

## Part D - Attaching our entity to the existing device (frontend "device merge")

**Goal.** Our live-view camera should appear on the **same device card** as the
official EZVIZ integration's entities, without depending on or modifying that
integration. In Home Assistant, the frontend groups entities onto a device card
purely by the **`device_id`** stamped on each entity's registry entry - so the
task is to get our entity's registry entry to point at the *existing* device.

Two distinct config entries can reference the same device. Registry entries have
independent `config_entry_id` and `device_id` fields: an entity owned by **our**
config entry can carry a `device_id` belonging to a device owned by **another**
config entry. The device itself is never re-created or re-owned.

### D.1 Two ways to achieve the merge

**Approach 1 - shared device identifier (public, recommended for us).**
We create our own entity with a `DeviceInfo` whose `identifiers` reuse the *same*
identity the target device already has:

```
device_info = DeviceInfo(identifiers={("ezviz", serial)})
```

When HA registers an entity that carries `device_info`, it calls the device
registry's get-or-create with **our** config-entry id and those identifiers.
Because a device with that identifier already exists, HA **merges**: it adds our
config entry to the device's owning set and stamps that device's id on our
entity. Result - our camera lands on the existing EZVIZ device card. This uses
only public, documented HA APIs and degrades gracefully: if the official
integration isn't installed, HA simply creates the device from our `device_info`
and we still work standalone.

**Approach 2 - direct device resolution + bind (what avoids co-owning the
device).**
If the aim is to attach *without* adding our config entry to the device's owning
set, resolve the real device object and bind to it instead of supplying
`device_info`:

1. From a user-picked **source entity id**, look it up in the entity registry to
   get its `device_id`, then fetch the `DeviceEntry` from the device registry.
   (Or let the user pick a device directly via a device selector.)
2. Attach that resolved device to our entity **before** it is added. For entities
   created under a config entry, HA will read the entity's device reference during
   registration and stamp its `device_id` onto our entity's registry entry - while
   calling get-or-create with *our* config-entry id, so the device is **not**
   re-associated to us.
3. For entities that are **not** created under a config entry (e.g. YAML/platform
   entities), HA does *not* read that device reference automatically. In that case,
   after the entity is added, explicitly update its registry entry to set
   `device_id` to the resolved device's id (via the entity registry's update call),
   guarding for idempotency (skip if already linked).

The important subtlety: Approach 2 never calls device-registry get-or-create with
our config-entry id *and* the target device's `device_info`, and never adds our
config entry to the device's `config_entries`. It only writes the `device_id`
field on our own entity's registry entry. The device stays wholly owned by the
original integration.

> Note: setting a device reference on the entity object before registration relies
> on an HA-core internal attribute rather than a documented public API, so it
> should be treated as version-sensitive and defensively guarded.

### D.2 Resolution helpers

- Entity → device: get the entity registry, look up the source entity id, read its
  `device_id`.
- Device lookup: get the device registry, fetch the `DeviceEntry` by id; every
  lookup can return `None` (device removed) and must be guarded.
- Config flow: offer an **entity selector** (optionally domain-restricted) or a
  **device selector** so the user picks the camera to attach to; persist the chosen
  device/serial in the config entry so the link survives reloads.

### D.3 Edge cases to handle

- **Source has no device.** If the source entity isn't registry-backed or has no
  `device_id`, skip linking - never fabricate a device. We still function as a
  standalone device from our own `device_info`.
- **Stale/removed device.** Guard every registry lookup for `None`.
- **Idempotency.** Skip the bind when the entity is already pointed at the target
  device (avoid double-writes/races), and skip entities HA already linked
  automatically.
- **No orphan/duplicate devices.** Because we either reuse an existing identifier
  (Approach 1) or reference a resolved existing device (Approach 2), we never
  accidentally mint a second, competing device for the same camera.

### D.4 Our decision

We ship our **own config flow and credentials** (we need cloud auth for the media
handshake - we do **not** borrow another integration's session), and we link our
camera to the existing EZVIZ device via **Approach 1** (shared `("ezviz", serial)`
identifier) because it is the clean public-API path and works standalone. We do
**not** add `ezviz` to our manifest dependencies and do **not** read another
integration's private runtime data. The config flow may optionally enumerate
existing EZVIZ devices from the public registries to pre-fill the serial picker -
a convenience, not a requirement.

## Part E - Recordings & playback (verified 2026-07-17)

Two independent recording sources, both reusing the existing decode/decrypt/serve
tail (channel-0x01 MPEG-PS -> `StreamingPsDecryptor` -> ffmpeg H.264 fMP4). Surfaced
in HA's media library via a `media_source` platform (root -> camera -> Cloud / SD
subfolders) and served by a token-guarded replay view; exposure is opt-in per account
(`enable_recordings`, off by default) with a per-camera override.

### E.1 Cloud recordings (cloud-replay, TLS)

- **List:** `GET /v3/clouds/videos/list` (`deviceSerial`, `channelNo`, `limit`,
  `videoType=2`) -> `videos[]` with `seqId`, `startTime`/`stopTime`, `fileSize`,
  `crypt`, `keyChecksum`, `streamUrl` (`host:port`), `storageVersion`, `videoLong`.
  Precise ms start is in the `coverPic` URL's `startTime` query param. Optionally
  enrich via `POST /v3/clouds/videoDetails`.
- **Ticket:** `GET /v3/cameras/ticketInfo` -> `ticketInfo.ticket`.
- **Transport:** a **separate TLS socket** (NOT ysproto) to `streamUrl`. 32-byte frame
  header `>IIIIIIII` (magic `0x9EBAACE9`, ver 1, seq, 0, cmd, 0, len, 0) + XML + MD5.
  OPEN cmd `0x5003` with a `<Request>` XML (Token=ticket, PlayType=2, File Id=seqId,
  Time Begin/End in CAS, CameraInfo SubSerial=`{serial}_{channel}`); HB cmd `0x5010`
  every 5 s; server frames carry `data_type` 0/1/2 = media, 100 = EOF; the server may
  also just close the socket to end. Payload is encrypted MPEG-PS. Ported from
  `pyezvizapi.stream.download_ezviz_cloud_replay` into `cloud_replay.py`.
- **Container:** an 80-byte "IMKH" (Hikvision) header precedes standard MPEG-PS;
  ffmpeg's PS demuxer resyncs past it (first pack `000001BA` at offset 80).

### E.2 SD-card recordings (LAN, ysproto `/playback`)

- **List:** `GET /v3/streaming/v2/records` (`deviceSerial`, `channelNo`, `startTime`,
  `stopTime`, `size`, `sortBy=0`, `requireLabel=0`). **Time format is UTC
  `"%Y-%m-%d %H:%M:%S"`** - epoch ms / epoch s / CAS all return a device exception
  (`meta.code 2004`, `DEVICE_EXCEPTION 61`). Records carry `begin`/`end` (or
  `startTime`/`stopTime`); the list may be **base64+zlib** JSON under `records`.
- **Transport:** the **same ysproto handshake as live** (Part B), differing only in the
  stream URL: path `/playback` with an added CAS time range, e.g.
  `ysproto://<vtm>/playback?dev=..&chn=1&stream=2&begin=<CAS>&end=<CAS>&serial=..&streamtag=NULL&..&ssn=<token>&biz=1&a=1&timestamp=..`.
  Everything else matches `/live`. A finite session: the VTDU closes at clip end (no
  reconnect). `stream.iter_playback_ps` + `ysproto.build_stream_url(time_range=...)`.
- **CAS time** = `"%Y%m%dT%H%M%SZ"` (same as cloud begin/end).
- **Clock skew:** camera clocks can run **ahead** of real time (observed ~15 min), so
  a just-recorded segment is timestamped in the near future; list windows must extend
  past "now" (we add +1 h) or recent footage is missed.

### E.3 Audio in recordings

Audio is ADTS AAC-LC (16 kHz mono observed). Plaintext on an unencrypted camera;
on an encrypted one the AAC body is AES-encrypted with a clear ADTS header - see
**B.10.3**. The replay view decrypts it (encrypted) or copies it (plaintext) and
always serves it; audio-disabled cameras (no audio PES) are a no-op.

### E.4 Mixed / rotated keys and robust serving (verified 2026-07-17)

- **Per-clip decryption auto-detect (`broadcast.maybe_decrypt_replay`).** A camera's
  Image Encryption can be toggled or its code rotated over time, so one camera's clips
  are a mix of plaintext, current-key, and old-key. The per-clip `crypt` /
  per-camera `is_encrypted` flags reflect the *current* setting, not the clip's - so
  decrypting on the flag corrupts plaintext clips. Instead the replay view fetches the
  **raw** clip (`verification_code=""`) and wraps it: buffer the first keyframe
  (`_PROBE_BYTES` 768 KB), decode-probe it raw vs decrypted with a short ffmpeg
  (`_probe_frame_count`, counts frames from `-progress`; garbage ~0, valid >=2), then
  stream raw (plaintext), decrypted (current key), or raw+warn (old/unknown key). A
  NAL-header heuristic can't do this: HEVC keeps the 2-byte NAL header clear on both
  plaintext and encrypted, so only the body differs (needs decoding to tell apart).
  Old-key clips are unrecoverable without that key. The probe process is reaped via
  `_terminate` in a `finally` (an orphaned asyncio subprocess transport raises in
  `__del__` on Python 3.14 when GC'd off the loop thread).
- **Drop undecodable audio (`_probe_audio_encodable`).** The fragmented-MP4 muxer
  writes *nothing at all* if a mapped output stream never gets a packet, so a bad audio
  track sinks the (good) video: the AAC encoder fails (`-22`) and ffmpeg emits only the
  init segment. `_prepare_replay` probes whether the served sample's audio can be
  AAC-encoded and passes `audio=` to the transcode accordingly - dropping audio
  (`-an`) when it is absent, corrupt, or encrypted-with-a-key-we-lack. Observed on
  Front Door **cloud** clips: the decrypted audio is garbage (`sample_rate=0`) even
  though the video decrypts perfectly - so cloud audio decryption for some cameras is
  an open follow-up, but playback is video-only rather than failing outright. (Audio
  decrypt is validated on Deck **SD**; the cloud path differs.)
- **Keyframe cap on the transcode (`mp4_replay_source -g 30`).** `frag_keyframe`
  flushes an MP4 fragment only at a keyframe; libx264's default 250-frame GOP means a
  short/static clip (no scene-cut keyframe) produces one fragment never flushed until
  EOF, so a live-streamed clip delivers only the init segment and never starts in the
  browser. `-g 30` (~1-2 s) makes fragments flush regularly - a clip with motion
  happened to work via a scene-cut IDR, which is why it looked clip-specific.
- **Config flow:** the verification-code field is always shown - Required when
  encrypted, Optional otherwise - so an unencrypted camera can still be given a code to
  decrypt older clips from an encrypted period (`_code_hint` wording).
- **Load validation:** on every (re)load, a camera reporting encrypted with no stored
  code is marked unavailable and raises an `encrypted_no_code` repair issue
  (`__init__._async_flag_encrypted_without_code`), instead of serving undecodable video.
