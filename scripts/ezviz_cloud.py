"""Shared EZVIZ cloud control-plane + media-plane helpers.

Reusable core for the ``scripts/`` diagnostic tools (``ezviz_stream_probe.py``,
``ezviz_list_cameras.py``). Implements the path documented in
``doc/specification.md`` §§3-4 and ``doc/reference.md`` Parts A-B:

    login (region-aware) -> server info -> device pagelist -> vtdutoken2
      -> VTM/VTDU ysproto:// handshake -> channel-0x01 media
      -> RTP/RFC-7798 de-packetize (HEVC) ; transport auto-detect

Secrets (passwords, session tokens, serials) are never logged in full. 2FA must
be OFF on the account (spec §7.1) - a 2FA account returns MFA challenge 6002,
surfaced as a clear error rather than handled.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import struct
import time
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants (reference.md A.1/A.2, spec §3). Desktop/Studio persona - load-
# bearing: the mobile persona doesn't reliably surface VTM routing data.
# --------------------------------------------------------------------------- #
REGION_CODE = {
    "Europe": "ieu",
    "Africa": "ieu",  # South Africa routes here
    "Asia": "isgp",
    "Singapore": "isgp",
    "India": "iindia",
    "NorthAmerica": "ius",
    "Oceania": "ius",
    "SouthAmerica": "isa",
}
CLIENT = {
    "clientType": "9",
    "clientNo": "shipin7",
    "appId": "ys7",
    "customNo": "1000001",
    "clientVersion": "2,5,1,2109068",
    "featureCode": "0" * 32,
}
VERSION_TAG = "v3.6.3.20221124"  # protobuf fields 3 & 6 (spec §3)

# VTM/VTDU binary protocol (reference.md B.1-B.3)
MAGIC = 0x24
CH_MSG = 0x00
CH_STREAM = 0x01
MSG_STREAMINFO_REQ = 0x13B
MSG_STREAMINFO_RSP = 0x13C
MSG_KEEPALIVE_REQ = 0x132

SC = b"\x00\x00\x00\x01"  # Annex-B start code


def log(msg: str) -> None:
    print(f"[ezviz] {msg}", flush=True)


def redact(token: str | None, keep: int = 6) -> str:
    if not token:
        return "<none>"
    return f"{token[:keep]}…({len(token)} chars)"


def mask_serial(serial: str | None) -> str:
    """Mask a device serial for logging (serials are treated as sensitive)."""
    if not serial:
        return "<none>"
    return f"{serial[:4]}…{serial[-2:]}" if len(serial) > 6 else "***"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


# --------------------------------------------------------------------------- #
# Minimal hand-rolled protobuf (reference.md B.5)
# --------------------------------------------------------------------------- #
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7


def encode_streaminforeq(stream_url: str, vtm_stream_key: str | None) -> bytes:
    """Fields: 1=streamurl, [2=vtmstreamkey], 3=version, 4=proxytype(0), 6=version."""

    def fstr(fn: int, s: str) -> bytes:
        data = s.encode()
        return bytes([(fn << 3) | 2]) + _varint(len(data)) + data

    body = fstr(1, stream_url)
    if vtm_stream_key is not None:
        body += fstr(2, vtm_stream_key)
    body += fstr(3, VERSION_TAG)
    body += bytes([(4 << 3) | 0]) + _varint(0)  # field 4 int32 = 0
    body += fstr(6, VERSION_TAG)
    return body


def decode_protobuf(data: bytes) -> dict[int, list]:
    fields: dict[int, list] = {}
    i = 0
    while i < len(data):
        try:
            tag, i = _read_varint(data, i)
            fn, wt = tag >> 3, tag & 7
            if wt == 0:
                val, i = _read_varint(data, i)
            elif wt == 2:
                ln, i = _read_varint(data, i)
                val, i = data[i : i + ln], i + ln
            elif wt == 5:
                val, i = data[i : i + 4], i + 4
            elif wt == 1:
                val, i = data[i : i + 8], i + 8
            else:
                break
        except IndexError:
            break
        fields.setdefault(fn, []).append(val)
    return fields


def field_str(fields: dict[int, list], fn: int) -> str | None:
    vals = fields.get(fn)
    if not vals or not isinstance(vals[0], bytes | bytearray):
        return None
    try:
        return vals[0].decode()
    except UnicodeDecodeError:
        return None


def scan_ysproto(body: bytes) -> str | None:
    idx = body.find(b"ysproto://")
    if idx < 0:
        return None
    end = idx
    while end < len(body) and 0x20 <= body[end] < 0x7F:
        end += 1
    return body[idx:end].decode()


# --------------------------------------------------------------------------- #
# Framed-socket reader (reference.md B.1 - bodies span TCP segments)
# --------------------------------------------------------------------------- #
class FrameReader:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = bytearray()
        self.closed = False

    def _fill(self) -> bool:
        chunk = self.sock.recv(65536)
        if not chunk:
            self.closed = True  # peer closed (e.g. the ~27s VTDU drop)
            return False
        self.buf += chunk
        return True

    def next_frame(self, deadline: float) -> tuple[int, int, bytes] | None:
        """Return (channel, msgcode, body) or None on timeout/close."""
        while True:
            # Hunt for the 0x24 magic; skip any non-protocol bytes.
            while self.buf and self.buf[0] != MAGIC:
                del self.buf[0]
            if len(self.buf) >= 8:
                _, ch, length, _seq, msg = struct.unpack(">BBHHH", self.buf[:8])
                if len(self.buf) >= 8 + length:
                    body = bytes(self.buf[8 : 8 + length])
                    del self.buf[: 8 + length]
                    return ch, msg, body
            if time.time() >= deadline:
                return None
            try:
                if not self._fill():
                    return None
            except TimeoutError:
                if time.time() >= deadline:
                    return None


# --------------------------------------------------------------------------- #
# RTP/RFC-7798 HEVC de-packetizer - spec §4.1 (proven), and transport detection
# --------------------------------------------------------------------------- #
def depacketize(body: bytes, state: dict) -> bytes:
    if len(body) < 14 or (body[0] >> 6) != 2 or (body[1] & 0x7F) != 96:
        return b""  # not H.265 video RTP
    cc = body[0] & 0x0F
    ext = (body[0] >> 4) & 1
    off = 12 + cc * 4
    if ext:
        if len(body) < off + 4:
            return b""
        extlen = int.from_bytes(body[off + 2 : off + 4], "big")
        off += 4 + extlen * 4
    pl = body[off:]
    if len(pl) < 3:
        return b""
    t = (pl[0] >> 1) & 0x3F
    if t < 48:  # single NAL
        return SC + pl
    if t == 48:  # aggregation packet
        out, i = b"", 2
        while i + 2 <= len(pl):
            sz = int.from_bytes(pl[i : i + 2], "big")
            i += 2
            out += SC + pl[i : i + sz]
            i += sz
        return out
    if t == 49:  # fragmentation unit
        fuh = pl[2]
        s, e, ftype = fuh >> 7, (fuh >> 6) & 1, fuh & 0x3F
        frag = pl[3:]
        if s:  # start: rebuild NAL header
            b0 = (pl[0] & 0x81) | (ftype << 1)
            state["fu"] = bytes([b0, pl[1]]) + frag
        elif state["fu"] is not None:
            state["fu"] += frag
        if e and state["fu"] is not None:
            nal, state["fu"] = state["fu"], None
            return SC + nal
    return b""


def detect_transport(body: bytes) -> str:
    """Auto-detect the channel-0x01 container from the first bytes (spec §4)."""
    if body[:4] == b"\x00\x00\x01\xba":
        return "mpeg-ps"
    if body and body[0] == 0x47:
        return "mpeg-ts"
    if body and (body[0] >> 6) == 2:
        return "rtp"
    return "unknown"


# --------------------------------------------------------------------------- #
# Control plane (HTTPS)
# --------------------------------------------------------------------------- #
class ApiError(RuntimeError):
    pass


def api_host(region: str) -> str:
    code = REGION_CODE.get(region)
    if not code:
        raise ApiError(f"Unknown region {region!r}; known: {sorted(REGION_CODE)}")
    return f"https://api{code}.ezvizlife.com"


def headers(session_id: str | None = None) -> dict[str, str]:
    h = {"User-Agent": "okhttp/3.12.1", "lang": "en", "netType": "WIFI", **CLIENT}
    if session_id:
        h["sessionId"] = session_id
    return h


def _dump(label: str, obj: object, *, debug: bool) -> None:
    if debug:
        text = obj if isinstance(obj, str) else json.dumps(obj, indent=2)
        log(f"{label}:\n{text[:4000]}")


def _deep_find(obj: object, key: str):
    """Recursively search a nested dict/list for the first value of `key`."""
    if isinstance(obj, dict):
        if obj.get(key):
            return obj[key]
        for v in obj.values():
            found = _deep_find(v, key)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find(v, key)
            if found:
                return found
    return None


def login(
    email: str, password: str, region: str, *, debug: bool = False
) -> tuple[str, str]:
    """Return (session_id, api_host). Handles region-redirect (1100), flags 2FA."""
    host = api_host(region)
    md5 = hashlib.md5(password.encode()).hexdigest()  # backend requires MD5
    data = {
        "account": email,
        "password": md5,
        "featureCode": CLIENT["featureCode"],
        "msgType": "0",
        "cuName": base64.b64encode(b"hass-ezviz-stream").decode(),
    }
    for attempt in range(2):  # one region-redirect retry
        log(f"login -> {host}/v3/users/login/v5 (attempt {attempt + 1})")
        resp = requests.post(
            f"{host}/v3/users/login/v5", data=data, headers=headers(), timeout=15
        )
        try:
            body = resp.json()
        except ValueError as exc:
            msg = f"login: non-JSON response ({resp.status_code}): {resp.text[:300]}"
            raise ApiError(msg) from exc
        code = (body.get("meta") or {}).get("code", resp.status_code)
        _dump("login response", body, debug=debug)
        if code == 200:
            sess = (body.get("loginSession") or {}).get("sessionId")
            if not sess:
                raise ApiError(f"login OK but no sessionId: {body.get('meta')}")
            new_host = (body.get("loginArea") or {}).get("apiDomain")
            if new_host:
                host = (
                    new_host if new_host.startswith("http") else f"https://{new_host}"
                )
            log(f"login OK  sessionId={redact(sess)}  host={host}")
            return sess, host
        if code == 1100:  # wrong region - retry against returned node
            new_host = (body.get("loginArea") or {}).get("apiDomain")
            if not new_host:
                raise ApiError("login: wrong region (1100) but no apiDomain to retry")
            host = new_host if new_host.startswith("http") else f"https://{new_host}"
            log(f"region redirect (1100) -> retrying on {host}")
            continue
        if code == 6002:
            raise ApiError(
                "login: account has 2FA enabled (6002). This tool requires 2FA OFF "
                "(spec §7.1) - disable two-step verification on the EZVIZ account."
            )
        hints = {
            1013: "incorrect username",
            1014: "incorrect password",
            1015: "account locked",
            1069: "terminal-bind limit reached (prune terminals in the app)",
            1226: "incorrect credentials",
        }
        raise ApiError(
            f"login failed: code={code} ({hints.get(code, 'see reference A.3')})"
        )
    raise ApiError("login: exhausted region-redirect retries")


def get_auth_addr(host: str, session_id: str, *, debug: bool = False) -> str:
    log(f"server info -> {host}/api/server/info/get")
    try:
        resp = requests.post(
            f"{host}/api/server/info/get",
            data={"sessionId": session_id, "clientType": CLIENT["clientType"]},
            headers=headers(session_id),
            timeout=15,
        )
        body = resp.json()
        _dump("server info response", body, debug=debug)
        auth = _deep_find(body, "authAddr")
        if auth:
            auth = auth if str(auth).startswith("http") else f"https://{auth}"
            log(f"authAddr = {auth}")
            return auth
    except (ValueError, requests.RequestException) as exc:
        log(f"server info failed ({exc}); falling back to api host for token")
    log(f"authAddr not found; using api host {host}")
    return host


def discover_devices(host: str, session_id: str, *, debug: bool = False) -> list[dict]:
    """Return every camera (resourceType>0), each tagged with a streamable flag."""
    url = (
        f"{host}/v3/userdevices/v1/resources/pagelist"
        "?filter=VTM&groupId=-1&limit=50&offset=0"
    )
    log(f"pagelist -> {url}")
    resp = requests.get(url, headers=headers(session_id), timeout=15)
    body = resp.json()
    _dump("pagelist response", body, debug=debug)

    resource_infos = _deep_find(body, "resourceInfos") or []
    vtm_map = _deep_find(body, "VTM") or {}
    dev_infos: dict[str, dict] = {}
    for di in _deep_find(body, "deviceInfos") or []:
        if di.get("deviceSerial"):
            dev_infos[di["deviceSerial"]] = di

    devices: list[dict] = []
    for ri in resource_infos:
        serial = ri.get("deviceSerial")
        if not serial or int(ri.get("resourceType", 0)) <= 0:
            continue
        resource_id = ri.get("resourceId")
        vtm = vtm_map.get(resource_id) or {}
        di = dev_infos.get(serial, {})
        devices.append(
            {
                "serial": serial,
                "name": ri.get("localName") or di.get("name") or "",
                "resource_id": resource_id,
                "resource_type": ri.get("resourceType"),
                "biz": ri.get("streamBizUrl", ""),
                "channel": int(di.get("channelNumber") or 1),
                "category": di.get("deviceCategory", ""),
                "status": di.get("status"),
                "vtm_ip": vtm.get("externalIp"),
                "vtm_port": int(vtm["port"]) if vtm.get("port") else None,
                "streamable": bool(vtm.get("externalIp")),
            }
        )
    streamable = sum(1 for d in devices if d["streamable"])
    log(f"discovered {len(devices)} camera(s), {streamable} streamable")
    return devices


def get_vtdu_token(auth_addr: str, session_id: str, *, debug: bool = False) -> str:
    # sign = the `s` claim decoded from the sessionId JWT payload (reference A.6)
    payload_seg = session_id.split(".")[1]
    payload_seg += "=" * (-len(payload_seg) % 4)  # base64url padding
    claims = json.loads(base64.urlsafe_b64decode(payload_seg))
    sign = claims["s"]
    url = f"{auth_addr}/vtdutoken2?ssid={session_id}&sign={sign}"
    log(f"vtdutoken2 -> {auth_addr}/vtdutoken2?ssid=…&sign={redact(sign)}")
    resp = requests.get(url, headers=headers(session_id), timeout=15)
    body = resp.json()
    _dump("vtdutoken2 response", body, debug=debug)
    tokens = body.get("tokens") or []
    if not tokens:
        raise ApiError(f"no VTDU tokens returned (retcode={body.get('retcode')})")
    log(f"vtdu token = {redact(tokens[0])}")
    return tokens[0]


# --------------------------------------------------------------------------- #
# Media plane (VTM/VTDU handshake - reference.md B.4-B.6)
# --------------------------------------------------------------------------- #
def build_stream_url(ip: str, port: int, dev: dict, token: str, stream: int = 1) -> str:
    """Build the ysproto live URL. ``stream`` selects the encoder track:
    1 = main stream, 2 = sub-stream (lower res, short GOP → a keyframe lands within
    a single VTDU session; see the IPC/keyframe finding in doc/reference.md B.11)."""
    biz = f"&{dev['biz']}" if dev["biz"] else ""
    ts = int(time.time() * 1000)
    return (
        f"ysproto://{ip}:{port}/live?"
        f"dev={dev['serial']}&chn={dev['channel']}&stream={stream}"
        f"&cln={CLIENT['clientType']}"
        f"&isp=0&auth=1&ssn={token}{biz}&vip=0&timestamp={ts}"
    )


def set_stream_param(url: str, stream: int) -> str:
    """Force the ``stream=`` index in a ysproto live URL (used to make the VTM's
    redirect carry the requested track through to the VTDU handshake)."""
    return re.sub(r"stream=\d+", f"stream={stream}", url, count=1)


def streaminfo_exchange(
    sock: socket.socket, stream_url: str, vtm_stream_key: str | None
) -> dict[int, list]:
    body = encode_streaminforeq(stream_url, vtm_stream_key)
    sock.sendall(
        struct.pack(">BBHHH", MAGIC, CH_MSG, len(body), 0, MSG_STREAMINFO_REQ) + body
    )
    reader = FrameReader(sock)
    deadline = time.time() + 10
    while True:
        frame = reader.next_frame(deadline)
        if frame is None:
            raise ApiError("no StreamInfoRsp before timeout")
        ch, msg, rsp = frame
        if msg == MSG_STREAMINFO_RSP:
            return decode_protobuf(rsp)
        log(f"  (ignoring frame ch={ch:#04x} msg={msg:#05x} len={len(rsp)})")


def open_stream(
    dev: dict, token: str, stream: int = 1
) -> tuple[socket.socket, FrameReader, str | None]:
    """VTM handshake -> VTDU redirect -> VTDU handshake. Returns live VTDU socket.

    ``stream`` selects the encoder track (1 = main, 2 = sub-stream)."""
    # 1) VTM
    log(f"connect VTM {dev['vtm_ip']}:{dev['vtm_port']}  (stream={stream})")
    vtm = socket.create_connection((dev["vtm_ip"], dev["vtm_port"]), timeout=10)
    vtm.settimeout(5)
    vtm_url = build_stream_url(dev["vtm_ip"], dev["vtm_port"], dev, token, stream)
    fields = streaminfo_exchange(vtm, vtm_url, None)
    vtm.close()

    result = (fields.get(1) or [None])[0]
    redirect = field_str(fields, 7)
    vtm_key = field_str(fields, 5)
    if not redirect:
        # protobuf field 7 missing - fall back to raw scan of the concatenated strings
        raw = b"".join(
            v for vals in fields.values() for v in vals if isinstance(v, bytes)
        )
        redirect = scan_ysproto(raw)
    if not redirect:
        raise ApiError(
            f"VTM gave no VTDU redirect (result={result}, fields={list(fields)})"
        )
    vtdu_host = redirect.split("//", 1)[1].split("/", 1)[0]
    vtdu_ip, _, vtdu_port = vtdu_host.partition(":")
    log(f"VTM -> VTDU redirect {vtdu_ip}:{vtdu_port}  vtmstreamkey={redact(vtm_key)}")

    # Ensure the requested track survives into the VTDU handshake (the VTM echoes
    # the URL back, but normalise stream= in case it was rewritten to the main track).
    redirect = set_stream_param(redirect, stream)

    # 2) VTDU - reuse the redirect URL verbatim, now carrying the vtmstreamkey
    log(f"connect VTDU {vtdu_ip}:{vtdu_port}")
    vtdu = socket.create_connection((vtdu_ip, int(vtdu_port)), timeout=10)
    vtdu.settimeout(5)
    rsp = streaminfo_exchange(vtdu, redirect, vtm_key)
    result = (rsp.get(1) or [0])[0]
    if result not in (0, None):
        vtdu.close()
        raise ApiError(f"VTDU StreamInfoRsp result={result} (see reference B.12)")
    stream_ssn = field_str(rsp, 4)
    log(f"VTDU handshake OK  result={result}  streamssn={redact(stream_ssn)}")
    return vtdu, FrameReader(vtdu), stream_ssn
