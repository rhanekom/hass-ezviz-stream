# EZVIZ Open Platform API (official developer REST API)

This documents the **official EZVIZ Open Platform** HTTP API - the token-based
developer REST API at `open.ys7.com` / regional `*open.ezviz.com` hosts, using
`/api/lapp/*` paths and an `ezopen://` streaming scheme.

**This is a different API from the one this integration uses.** Our runtime path is
the private mobile/desktop **app** API (`apiXXX.ezvizlife.com`, VTM/VTDU `ysproto`
binary handshake) reverse-engineered in [`reference.md`](reference.md). The Open
Platform is documented here for two reasons: to **validate** the assumptions we made
while reverse-engineering (see the validation section), and to record it as a
possible **alternative / fallback** path (see the opportunity section).

## Confidence and sources

The official docs (`open.ys7.com/help/en/<id>` and `/doc/en/HTTP/*.html`) are a
JavaScript SPA that does not render for a fetcher, and some pages now show a
deprecation-redirect notice. The catalog below was reconstructed from open-source
wrapper libraries (`pkg.go.dev/github.com/wei193/ys7`,
`github.com/chenwochong/ys7`), the `RenierM26/pyEzvizApi` and `ha-ezviz` projects,
and search-engine caches of the doc pages. **Items that could not be confirmed
against a primary rendered doc are tagged `[UNVERIFIED]` or `[INFERRED]`** - do not
treat those as authoritative; confirm them live before relying on them.

## Conventions

- All calls are `POST` with `Content-Type: application/x-www-form-urlencoded`.
- Every call except `token/get` takes `accessToken` as a form parameter.
- Response envelope: `{ "code": "200", "msg": "...", "data": ... }` - `code` is a
  string, `"200"` = success.

## 1. Auth

### `POST /api/lapp/token/get`

Exchange developer app credentials for an access token.

| Param | Required | Description |
|---|---|---|
| `appKey` | yes | Developer app key |
| `appSecret` | yes | Developer app secret |

`data`: `accessToken`, `expireTime` (absolute expiry as **epoch milliseconds**, not
a duration), `areaDomain` (the regional base URL the account must use for all
subsequent calls).

- Token lifetime commonly cited as ~7 days `[UNVERIFIED]` - rely on `expireTime`,
  do not hardcode. Cache the token; never call `token/get` per request.

## 2. Region hosts and selection

All hosts serve the same `/api/lapp/*` paths:

| Region | Host |
|---|---|
| Mainland China | `open.ys7.com` |
| Europe | `ieuopen.ezviz.com` |
| Singapore / SE Asia | `isgpopen.ezviz.com` |
| North America | `iusopen.ezviz.com` |
| South America | `isaopen.ezviz.com` |
| India | `iindiaopen.ezviz.com` |

Selection: call `token/get` on the host where the **app** is registered; the
response `areaDomain` is the correct regional base for that account - honour it for
all later calls. The `appKey` must match the account region. For a South-Africa
deployment the account is typically on the **Europe (`ieuopen`)** region, matching
the `ieu` node our private path uses (see reference.md A.2).

## 3. Images / snapshot

### `POST /api/lapp/device/capture` - real-time capture

| Param | Required | Description |
|---|---|---|
| `deviceSerial` | yes | Device serial |
| `channelNo` | yes | Channel (1 for single-lens) |

`data.picUrl` = a **freshly captured** JPEG on EZVIZ cloud (URL time-limited). This
grabs from the device now, so on a battery/PIR camera it **wakes the device**
`[INFERRED - not doc-confirmed]`. Same battery cost as opening a stream.

### `POST /api/lapp/device/uuid/picture` - `[UNVERIFIED]`

`GetPictureByUuid(uuid, size)` -> `data.picUrl`; retrieves a picture by stored UUID.

### Stored image WITHOUT waking the device

There is **no dedicated "return last snapshot" endpoint**. Two fields give a stored
image without a capture:

- `camera/list` -> `picUrl` = the camera's last cover/thumbnail (stored). See §5.
- `alarm/list` -> `alarmPicUrl` = the picture captured at the last alarm event
  (stored). This is the closest thing to a "latest motion image". See §4.

## 4. Alarm / motion

### `POST /api/lapp/alarm/list` (account-wide) and `/api/lapp/alarm/device/list` (one device)

| Param | Required | Description |
|---|---|---|
| `deviceSerial` | required for `device/list`; null = all devices on `alarm/list` | |
| `startTime` / `endTime` | no | Range (epoch ms) |
| `alarmType` | no | Filter by type; `-1` = all |
| `status` | no | Read status: `0` unread / `1` read / `2` all `[UNVERIFIED]` |
| `pageStart` / `pageSize` | no | Page index (0-based), size (default 10, max ~50) |

`data[]` fields: `alarmId`, `alarmName`, `alarmType`, `alarmTime`/`alarmStart`
(epoch ms), `channelNo`, `channelName`, `deviceSerial`, **`alarmPicUrl`**,
**`isEncrypt`** (1 = image encrypted), `isChecked`, plus a `page`
`{ total, page, size }`.

**Motion filtering** via `alarmType`. Verified motion-type values (from `ha-ezviz`):
`10000` PIR event, `10002` / `12000` motion detection, `10010` PIR alarm, `10035`
baby motion, `10013` line crossing, `10029` region entrance, `10030` region exit,
`10028` fast-moving. There is **no single "latest motion image" endpoint** - query
`alarm/list` with a motion `alarmType` and take the newest record's `alarmPicUrl`.
Use the record's `alarmType` field, not the type number embedded in the pic URL's
`fileId=` (observed unreliable).

**Encryption:** when `isEncrypt = 1` the `alarmPicUrl` image is encrypted and is
decrypted with the device **verification code** (the 6-uppercase-letter label code,
which is the default image-encryption password). This matches our still-image
`hikencodepicture` scheme (reference.md B.10.2). `[Exact cipher not stated in a
primary Open-Platform doc - our B.10.2 comes from pyEzvizApi.]`

## 5. Device list / info / status (battery and encryption)

- `POST /api/lapp/device/list` -> `data[]`: `deviceSerial`, `deviceName`,
  `deviceType`, `status` (1=online), `defence`, `deviceVersion`.
- `POST /api/lapp/device/info` (`deviceSerial`) -> `model`, `status`, `defence`,
  **`isEncrypt`** (1 = encryption on), `alarmSoundMode`, `offlineNotify`.
- `POST /api/lapp/device/status/get` (`deviceSerial`, `channel`) -> **battery lives
  here**: **`battryStatus`** (battery level 0-100 - note the API's misspelling),
  `privacyStatus`, `pirStatus`, `diskNum`, `diskState`, `cloudStatus`, ...
- `POST /api/lapp/camera/list` (and `/api/lapp/device/camera/list`) -> `Camera[]`:
  `deviceSerial`, `channelNo`, `channelName`, **`picUrl`** (last cover image),
  `videoLevel`, **`isEncrypt`**, `status`.

### Encryption / verification-code endpoints

- `POST /api/lapp/device/encrypt/on` (`deviceSerial`).
- `POST /api/lapp/device/encrypt/off` (`deviceSerial`, **`validateCode`**).
- `POST /api/lapp/device/add` (`deviceSerial`, `validateCode`).

`validateCode` is the 6-letter label code - the same verification code that decrypts
encrypted streams and alarm images. It is never returned in a response (it is
physically on the device label). Other device endpoints exist for capability query
(`device/capacity`), defence, name, PTZ, presets, switches, and IPC-under-NVR.

## 6. Live streaming

### `POST /api/lapp/live/address/get`

| Param | Required | Description |
|---|---|---|
| `deviceSerial` | yes | |
| `channelNo` | no | Default 1 |
| `protocol` | no | `1`=ezopen, `2`=HLS, `3`=RTMP, `4`=FLV (`2`->`.m3u8` confirmed; other values `[UNVERIFIED]`) |
| `quality` | no | `1`=HD/main, `2`=fluent/sub `[UNVERIFIED]` |
| `expireTime` | no | Seconds the URL stays valid |
| `type` | no | `1`=live, `2`=playback `[UNVERIFIED]` |
| `supportH265` | no | Client HEVC support `[UNVERIFIED]` |

`data`: `id`, `url`, `expireTime`. Related: `live/address/limited`,
`live/video/list`, `live/video/open` / `close`.

**`ezopen://` scheme:** EZVIZ's own scheme consumed by the EZUIKit player SDKs, of
the form `ezopen://[verification_code@]open.ys7.com/<serial>/<channelNo>[.hd].live`
(and `.rec` for playback) `[grammar reconstructed, UNVERIFIED]`. It is **not**
directly playable by ffmpeg/go2rtc - request `protocol=2/3/4` for an HLS/RTMP/FLV
URL. Note the optional `verification_code@` prefix mirrors our decrypt-with-code
model.

## 7. Rate limits / quotas

Mostly **`[UNVERIFIED]`** - no published per-endpoint QPS/daily numbers were
retrievable. The one concrete tier limit found: the **free tier allows 3 concurrent
live-view channels** (paid tiers raise this). Practical guidance: cache the token to
`expireTime`; avoid tight polling of `device/capture` / `alarm/list`. Confirm real
limits on the developer console.

## 8. App registration / cost

The Open Platform **requires registering a developer app** (self-service on the
regional developer console) to obtain `appKey`/`appSecret`. A normal EZVIZ account
holder can self-serve; there is a **free tier** (with the 3-channel live limit). Paid
tiers exist for higher concurrency. `[Exact free-tier terms / business verification
= UNVERIFIED.]`

---

## Validation of our reverse-engineered assumptions

How the official Open Platform docs bear on the assumptions in `reference.md`. Note
the Open Platform is a different surface, so validation is at the **concept** level;
it cannot confirm our exact private endpoints (VTM/VTDU, `vtdutoken2`, pagelist).

| Assumption (private path, reference.md) | Official Open Platform says | Verdict |
|---|---|---|
| Region-pinned account; SA routes via the EU (`ieu`) node; follow the redirect (login `1100` -> `apiDomain`, A.2). | Account is region-pinned; `token/get` returns `areaDomain` to use for all calls; EU host is `ieuopen.ezviz.com`. | **Confirmed (concept).** Same model; `areaDomain` == our `apiDomain` idea. |
| The 6-char verification code IS the image/video encryption password and AES key source (B.10). | `validateCode` = the label code; used to disable encryption and (per wrappers) decrypt images/streams. | **Confirmed.** |
| Encryption is a per-device toggle we must handle (we auto-detect by trial-decrypt). | `device/info` / `camera/list` expose `isEncrypt` (1=on). | **Confirmed** - and an explicit flag exists that we do not read (opportunity). |
| Battery cameras are a distinct, slow-to-wake class; we key off `deviceCategory`. | Battery is first-class: `device/status/get` -> `battryStatus` (level %). | **Confirmed** - official even exposes battery level. |
| A stored alarm/motion image can be fetched WITHOUT waking the camera; a live grab wakes it (A.8.1). | `alarmPicUrl` = image captured at the alarm moment (stored); `device/capture` = fresh grab. | **Confirmed for the stored path;** capture-wakes is inferred, not doc-stated. |
| Still-image alarm encryption = `hikencodepicture` AES-CBC (B.10.2), distinct from video AES-ECB. | Alarm images are encrypted (`isEncrypt`) and decrypted with the verification code. | **Confirmed in spirit;** exact cipher not in a primary Open-Platform doc. |
| ~27 s VTDU drop, keep-alive, `ysproto` handshake, StreamInfoRsp codes (B.4-B.12). | Not covered - streaming is hidden behind ezopen/HLS/RTMP media servers. | **Not validated (different surface);** no contradiction. |
| Concurrency is capped (codes 5504/5546, B.12). | Free tier = 3 concurrent live channels. | **Consistent** (both cap concurrency); numbers not comparable. |

Net: **nothing in the official docs contradicts our reverse-engineered model.** The
core assumptions (region pinning, verification-code-as-encryption-key, battery class,
stored-vs-fresh image behaviour, encryption toggle) are all corroborated.

## Opportunity assessment

1. **A "last motion image" (your target).** No dedicated endpoint exists on either
   API. The mechanism is: query the alarm list **filtered to a motion `alarmType`**
   and take the newest `alarmPicUrl`. We already fetch the latest alarm image on the
   private API (`/v3/alarms/v2/advanced` with `queryType=-1` = **all** types). The
   actionable win is to **filter to motion types** so the thumbnail is specifically
   the last *motion*, not any event (e.g. a doorbell press or offline notice). This
   is a refinement to our existing `async_get_last_motion_image` and likely doable on
   the private endpoint's own type filter - **no Open-Platform adoption required**.
   `[Confirm the private endpoint's motion-type/stype filter values before relying on
   it.]`
2. **Battery level.** `device/status/get` -> `battryStatus` gives a battery
   percentage. We just added a battery yes/no field; a battery-**level** sensor is a
   natural follow-on. Worth checking whether the private pagelist / a status endpoint
   exposes the same field so we can do it without the Open Platform.
3. **Explicit `isEncrypt`.** Reading an encryption flag would let us skip
   trial-decrypt auto-detection. Minor robustness win if the private path exposes an
   equivalent capability flag.
4. **Adopt the Open Platform wholesale?** Trade-offs:
   - *Pros:* officially supported and stable; documented; HLS/RTMP URLs play
     directly (no `ysproto` handshake, no reconnect loop); explicit battery and
     encryption fields.
   - *Cons:* the user must **register a developer app** (appKey/appSecret - real
     friction, not everyone will); free tier capped at **3 concurrent live
     channels**; streaming rides EZVIZ media servers (added latency, their transcode,
     quotas) instead of our direct HEVC; unknown daily/QPS limits; a whole second
     integration path to maintain.
   - **Recommendation:** keep the private API as the default (zero user setup, direct
     stream). Treat the Open Platform as an optional **fallback** worth revisiting
     later - its stored `alarmPicUrl` and `battryStatus` are the most attractive
     pieces for battery-friendly thumbnails and a battery sensor.

## Unverified items (need a live check or a primary render)

- Exact token lifetime (rely on `expireTime`).
- `device/capture` wake-vs-stored behaviour (strongly inferred, not doc-confirmed).
- `live/address/get` `protocol`/`quality`/`type`/`supportH265` enum values (only
  `protocol=2` -> HLS confirmed).
- Alarm-image cipher specifics on the Open Platform.
- Per-endpoint rate limits / daily quotas.
- Free-tier registration terms and cost beyond the 3-channel limit.
- `ezopen://` exact segment grammar.
