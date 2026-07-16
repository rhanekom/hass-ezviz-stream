# `scripts/` - EZVIZ cloud diagnostic tools

Standalone command-line tools used to reverse-engineer and validate the EZVIZ
cloud streaming path against **real cameras**. They are the manual test harness
for the pipeline; the protocol logic proven here is what gets ported into the
integration proper (`custom_components/ezviz_stream/`). They are **not** part of
the shipped Home Assistant component.

The authoritative design is [`../doc/specification.md`](../doc/specification.md);
the protocol details and empirical findings are in
[`../doc/reference.md`](../doc/reference.md); next actions are in
[`../doc/TODO.md`](../doc/TODO.md).

## Prerequisites

- Run everything through uv: `uv run python scripts/<tool>.py …`.
- **Credentials** come from the untracked repo-root `.env` (copy `.env.example`):
  `EZVIZ_EMAIL`, `EZVIZ_PASSWORD`, `EZVIZ_REGION` (default `Europe`, which is the
  route for South Africa). Any script flag (`--email`, …) overrides the env.
- **2FA must be disabled** on the account (spec §7.1); a 2FA account is rejected
  with a clear error (login code `6002`).
- `ffmpeg` / `ffprobe` on `PATH` are used for frame decoding.
- Serials are treated as sensitive and **masked** in output by default.

## The tools

### `ezviz_cloud.py` - shared core (library, not a CLI)

The reusable control-plane + media-plane module the CLIs import: region-aware
login, device discovery, VTDU token, the VTM/VTDU `ysproto://` binary handshake,
the RTP/RFC-7798 HEVC de-packetizer (spec §4.1), transport auto-detection, and
KeepAlive. Depends on `requests`.

### `ezviz_list_cameras.py` - list account cameras

Logs in and prints every camera linked to the account with the details relevant
to streaming (VTM node, channel, category, online status, whether it is
streamable). Serials are masked unless you pass `--full-serials`.

```bash
uv run python scripts/ezviz_list_cameras.py
```

### `ezviz_stream_probe.py` - capture + decode a frame per camera

Drives the full path (login → handshake → channel-0x01 media → transport-detect →
de-packetize/dump → FFmpeg) and writes a capture plus a decoded `.jpg` per camera
to `scripts/out/` (wiped at the start of every run; gitignored). Reconnects across
the ~27 s VTDU drop and sends periodic KeepAlive.

```bash
uv run python scripts/ezviz_stream_probe.py                 # all streamable cams
uv run python scripts/ezviz_stream_probe.py --serial <SN>   # one camera
uv run python scripts/ezviz_stream_probe.py --duration 120  # bigger reconnect budget
uv run python scripts/ezviz_stream_probe.py --stream 2      # sub-stream (short GOP)
```

For **IPC cams with Image Encryption ON**, use the low-GOP sub-stream and pass the
camera's verification code so the AES-encrypted H.264 is decrypted before FFmpeg
(uses `pyezvizapi`). One shared code can cover all encrypted cams via
`EZVIZ_VERIFY_CODE` in `.env`:

```bash
uv run python scripts/ezviz_stream_probe.py --serial <SN> --stream 2 \
    --verify-code <6-LETTER-CODE>     # or set EZVIZ_VERIFY_CODE in .env
```

It also has an experimental opcode-sweep mode used to hunt for an on-demand
I-frame request on IPC/MPEG-PS cameras (see the open item in `TODO.md`):

```bash
uv run python scripts/ezviz_stream_probe.py --probe-iframe   # sweep 0x130–0x145
```

### `parse_ysproto_pcap.py` - find the I-frame opcode from a capture

Decodes the `ysproto` control messages out of a packet capture (taken while the
official EZVIZ client - Studio/app - live-views a camera) and flags any
client→server opcode we don't already know: the candidate I-frame request. Reads
the pcap directly with `scapy` (a dev dependency), so it runs the same on Windows
or in the devcontainer.

```bash
uv run python scripts/parse_ysproto_pcap.py capture.pcapng
```

> The `StreamInfoReq` body contains your stream token; review the output before
> sharing, and redact any token/serial. The opcode itself is not sensitive.

### `ezviz_producer.py` - stream Annex-B HEVC to stdout

Drives the *actual integration* code (`api` + `stream.stream_annexb`) to stream a
battery camera's continuous Annex-B HEVC to stdout, so the live cloud path can be
verified outside Home Assistant (pipe into a player). RTP/HEVC only; encrypted
MPEG-PS (IPC) is not handled here. Unlike the other tools it reads a JSON
`--creds-file` (`username` / `password` / `region` / `serial` / optional `stream`)
rather than `.env`:

```bash
uv run python scripts/ezviz_producer.py --creds-file creds.json | ffplay -
```

## Output & safety

- Captures and decoded frames go only to `scripts/out/` (gitignored).
- `.env` (real credentials) is gitignored - keep it out of commits.
- These tools make real calls to the EZVIZ cloud with the account's own
  credentials against the user's own devices.
