"""
Async EZVIZ cloud API client (control plane: login + camera discovery).

Just the HTTPS control-plane needed for the config flow and coordinator - the
VTM/VTDU media handshake lives in the streaming module (added with the camera
platform). Ported from the proven sync core in ``scripts/ezviz_cloud.py`` to async
on Home Assistant's shared ``aiohttp`` session.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import BATTERY_CAMERA_CATEGORY, HIK_ENCRYPTION_HEADER, REGION_API_CODES
from .decrypt_image import StillImageDecryptError, decrypt_still_image

_LOGGER = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 15
_HTTP_OK = 200
_LOGIN_PATH = "/v3/users/login/v5"
_PAGELIST_PATH = (
    "/v3/userdevices/v1/resources/pagelist?filter=VTM&groupId=-1&limit=50&offset=0"
)
# Most recent motion/alarm event (with its stored still image); limit=1 = latest.
_ALARM_PATH = "/v3/alarms/v2/advanced?queryType=-1&limit=1&stype=-1&deviceSerials="

# Desktop/Studio client persona - load-bearing: the mobile persona doesn't reliably
# surface VTM routing data (doc/reference.md A.1/A.2).
_CLIENT: dict[str, str] = {
    "clientType": "9",
    "clientNo": "shipin7",
    "appId": "ys7",
    "customNo": "1000001",
    "clientVersion": "2,5,1,2109068",
    "featureCode": "0" * 32,
}

# EZVIZ login result codes we translate to typed errors (doc/reference.md A.3).
_MFA_CODE = 6002
_REGION_REDIRECT_CODE = 1100
_AUTH_ERROR_HINTS: dict[int, str] = {
    1013: "incorrect username",
    1014: "incorrect password",
    1015: "account locked",
    1069: "terminal-bind limit reached (prune terminals in the EZVIZ app)",
    1226: "incorrect credentials",
}


class EzvizStreamApiError(Exception):
    """Base error for the EZVIZ cloud client."""


# Names follow the Home Assistant config-flow convention (no "Error" suffix).
class CannotConnect(EzvizStreamApiError):  # noqa: N818
    """The cloud API was unreachable or returned an unusable response."""


class InvalidAuth(EzvizStreamApiError):  # noqa: N818
    """The account credentials were rejected."""


class MfaRequired(EzvizStreamApiError):  # noqa: N818
    """The account has two-step verification enabled (login code 6002)."""


class InvalidRegion(EzvizStreamApiError):  # noqa: N818
    """The configured region is not one we know how to route."""


@dataclass(slots=True)
class EzvizCamera:
    """A camera discovered on the account, with the fields relevant to streaming."""

    serial: str
    name: str
    category: str
    channel: int
    status: int | None
    streamable: bool
    vtm_ip: str | None = None
    vtm_port: int | None = None
    biz: str = ""  # streamBizUrl query fragment appended to the stream URL

    @property
    def is_online(self) -> bool:
        """Return True when the camera reports online (status 1)."""
        return self.status == 1

    @property
    def is_battery(self) -> bool:
        """Return True for a battery-powered camera (slow to wake)."""
        return self.category == BATTERY_CAMERA_CATEGORY

    @property
    def label(self) -> str:
        """A human-friendly label for the config-flow picker."""
        return self.name or self.serial


def _api_host(region: str) -> str:
    code = REGION_API_CODES.get(region)
    if not code:
        raise InvalidRegion(region)
    return f"https://api{code}.ezvizlife.com"


def _headers(session_id: str | None = None) -> dict[str, str]:
    hdrs = {"User-Agent": "okhttp/3.12.1", "lang": "en", "netType": "WIFI", **_CLIENT}
    if session_id:
        hdrs["sessionId"] = session_id
    return hdrs


def _first_image_url(value: Any) -> str | None:
    """Return the first HTTP(S) URL from a picUrl-style value (may be ``;``-joined)."""
    if not isinstance(value, str):
        return None
    for part in value.split(";"):
        text = part.strip()
        if text.startswith(("http://", "https://")):
            return text
    return None


def _deep_find(obj: Any, key: str) -> Any:
    """Return the first truthy value for ``key`` anywhere in a nested dict/list."""
    if isinstance(obj, dict):
        if obj.get(key):
            return obj[key]
        for value in obj.values():
            if (found := _deep_find(value, key)) is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            if (found := _deep_find(value, key)) is not None:
                return found
    return None


class EzvizCloudApi:
    """Minimal async EZVIZ cloud control-plane client."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise with an externally-owned aiohttp session (HA's shared one)."""
        self._session = session
        self._session_id: str | None = None
        self._host: str | None = None
        self._auth_addr: str | None = None

    async def async_login(self, email: str, password: str, region: str) -> None:
        """
        Authenticate and cache the session id + resolved API host.

        Raises InvalidAuth, MfaRequired, CannotConnect, or InvalidRegion.
        """
        host = _api_host(region)
        pwd_md5 = hashlib.md5(password.encode(), usedforsecurity=False).hexdigest()
        data = {
            "account": email,
            "password": pwd_md5,
            "featureCode": _CLIENT["featureCode"],
            "msgType": "0",
            "cuName": base64.b64encode(b"hass-ezviz-stream").decode(),
        }

        for _attempt in range(2):  # one region-redirect (1100) retry
            body = await self._post(f"{host}{_LOGIN_PATH}", data)
            code = (body.get("meta") or {}).get("code")
            if code == _HTTP_OK:
                session_id = (body.get("loginSession") or {}).get("sessionId")
                if not session_id:
                    msg = "login succeeded but no session id returned"
                    raise CannotConnect(msg)
                new_host = (body.get("loginArea") or {}).get("apiDomain")
                self._host = _normalise_host(new_host) or host
                self._session_id = session_id
                return
            if code == _REGION_REDIRECT_CODE:
                area = body.get("loginArea") or {}
                new_host = _normalise_host(area.get("apiDomain"))
                if not new_host:
                    msg = "region redirect without a host to retry"
                    raise CannotConnect(msg)
                host = new_host
                continue
            if code == _MFA_CODE:
                raise MfaRequired
            raise InvalidAuth(_AUTH_ERROR_HINTS.get(code, f"login code {code}"))
        msg = "exhausted region-redirect retries"
        raise CannotConnect(msg)

    async def async_get_cameras(self) -> list[EzvizCamera]:
        """Return the streamable cameras on the account. Requires a prior login."""
        if not self._session_id or not self._host:
            msg = "not logged in"
            raise EzvizStreamApiError(msg)
        body = await self._get(f"{self._host}{_PAGELIST_PATH}")

        vtm_map = _deep_find(body, "VTM") or {}
        dev_infos = {
            di["deviceSerial"]: di
            for di in (_deep_find(body, "deviceInfos") or [])
            if di.get("deviceSerial")
        }
        cameras: list[EzvizCamera] = []
        for resource in _deep_find(body, "resourceInfos") or []:
            serial = resource.get("deviceSerial")
            if not serial or int(resource.get("resourceType", 0)) <= 0:
                continue
            vtm = vtm_map.get(resource.get("resourceId")) or {}
            info = dev_infos.get(serial, {})
            cameras.append(
                EzvizCamera(
                    serial=serial,
                    name=resource.get("localName") or info.get("name") or "",
                    category=info.get("deviceCategory", ""),
                    channel=int(info.get("channelNumber") or 1),
                    status=info.get("status"),
                    streamable=bool(vtm.get("externalIp")),
                    vtm_ip=vtm.get("externalIp"),
                    vtm_port=int(vtm["port"]) if vtm.get("port") else None,
                    biz=resource.get("streamBizUrl", ""),
                )
            )
        return [cam for cam in cameras if cam.streamable]

    async def async_get_last_motion_image(
        self, serial: str, *, verification_code: str = ""
    ) -> bytes | None:
        """
        Return the most recent motion/alarm still image for a camera, or None.

        Fetched over plain HTTPS from the alarms API (reference A.8.1): no VTDU
        session and no camera wake, so it is safe as a battery camera's thumbnail.
        A payload wrapped in the ``hikencodepicture`` envelope is decrypted with the
        verification code (reference B.10.2); a wrong code yields None.
        """
        if not self._session_id or not self._host:
            msg = "not logged in"
            raise EzvizStreamApiError(msg)
        body = await self._get(f"{self._host}{_ALARM_PATH}{serial}")
        if (body.get("meta") or {}).get("code") != _HTTP_OK:
            return None
        image_url = _first_image_url(
            _deep_find(body, "picUrl") or _deep_find(body, "picURL")
        )
        if not image_url:
            return None
        data = await self._get_bytes(image_url)
        if not data or HIK_ENCRYPTION_HEADER not in data:
            return data
        try:
            return decrypt_still_image(data, verification_code)
        except StillImageDecryptError:
            _LOGGER.debug("Could not decrypt motion image (check verification code)")
            return None

    async def async_get_vtdu_token(self) -> str:
        """Fetch a VTDU media token (needed per streaming session). Requires login."""
        if not self._session_id or not self._host:
            msg = "not logged in"
            raise EzvizStreamApiError(msg)
        auth_addr = await self._async_auth_addr()
        # sign = the `s` claim from the sessionId JWT payload (reference A.6).
        payload_seg = self._session_id.split(".")[1]
        payload_seg += "=" * (-len(payload_seg) % 4)  # base64url padding
        try:
            sign = json.loads(base64.urlsafe_b64decode(payload_seg))["s"]
        except (ValueError, KeyError) as err:
            msg = "session id is not a decodable JWT"
            raise CannotConnect(msg) from err
        url = f"{auth_addr}/vtdutoken2?ssid={self._session_id}&sign={sign}"
        body = await self._get(url)
        tokens = body.get("tokens") or []
        if not tokens:
            msg = f"no VTDU tokens returned (retcode={body.get('retcode')})"
            raise CannotConnect(msg)
        return tokens[0]

    async def _async_auth_addr(self) -> str:
        """Resolve (and cache) the auth-node host for token requests."""
        if self._auth_addr is not None:
            return self._auth_addr
        try:
            body = await self._post(
                f"{self._host}/api/server/info/get",
                {
                    "sessionId": self._session_id or "",
                    "clientType": _CLIENT["clientType"],
                },
            )
            auth = _normalise_host(_deep_find(body, "authAddr"))
        except CannotConnect:
            auth = None
        self._auth_addr = auth or self._host
        return self._auth_addr

    async def _post(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        return await self._request("post", url, data=data)

    async def _get(self, url: str) -> dict[str, Any]:
        return await self._request("get", url)

    async def _get_bytes(self, url: str) -> bytes | None:
        """GET raw binary content (e.g. an alarm image); None on any failure."""
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                resp = await self._session.get(url, headers=_headers(self._session_id))
                if resp.status != _HTTP_OK:
                    return None
                return await resp.read()
        except TimeoutError, aiohttp.ClientError:
            return None

    async def _request(
        self, method: str, url: str, *, data: dict[str, str] | None = None
    ) -> dict[str, Any]:
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                resp = await self._session.request(
                    method, url, data=data, headers=_headers(self._session_id)
                )
                # EZVIZ sometimes serves JSON with a non-JSON content-type.
                body = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise CannotConnect(str(err)) from err
        except ValueError as err:  # non-JSON body
            msg = "cloud API returned a non-JSON response"
            raise CannotConnect(msg) from err
        if not isinstance(body, dict):
            msg = "cloud API returned an unexpected payload"
            raise CannotConnect(msg)
        return body


def _normalise_host(host: str | None) -> str | None:
    if not host:
        return None
    return host if host.startswith("http") else f"https://{host}"
