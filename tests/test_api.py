"""Tests for the async EZVIZ cloud control-plane client."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ezviz_stream.api import (
    CannotConnect,
    EzvizCloudApi,
    InvalidAuth,
    MfaRequired,
)

# A sessionId shaped like a JWT whose payload carries the `s` (sign) claim.
_SIGN = "SIGNVALUE"
_PAYLOAD = (
    base64.urlsafe_b64encode(json.dumps({"s": _SIGN}).encode()).decode().rstrip("=")
)
_SESSION_ID = f"hdr.{_PAYLOAD}.sig"

_LOGIN_OK = {
    "meta": {"code": 200},
    "loginSession": {"sessionId": _SESSION_ID},
    "loginArea": {"apiDomain": "apiieu.ezvizlife.com"},
}
_PAGELIST = {
    "resourceInfos": [
        {
            "deviceSerial": "SN1",
            "resourceId": "R1",
            "resourceType": 1,
            "streamBizUrl": "biz=1",
        }
    ],
    "deviceInfos": [
        {
            "deviceSerial": "SN1",
            "deviceCategory": "IPC",
            "channelNumber": 1,
            "status": 1,
            "name": "Front door",  # the app-set camera name lives here
        }
    ],
    "VTM": {"R1": {"externalIp": "10.0.0.1", "port": "6001"}},
}


def _session(routes: dict[str, dict[str, Any]]) -> MagicMock:
    """A fake aiohttp session dispatching by URL substring to canned JSON bodies."""

    async def _request(_method: str, url: str, **_kwargs: Any) -> MagicMock:
        for key, body in routes.items():
            if key in url:
                resp = MagicMock()
                resp.json = AsyncMock(return_value=body)
                return resp
        raise AssertionError(f"unexpected URL: {url}")

    session = MagicMock()
    session.request = _request
    return session


async def test_login_success_and_cameras() -> None:
    api = EzvizCloudApi(
        _session({"users/login": _LOGIN_OK, "resources/pagelist": _PAGELIST})
    )
    await api.async_login("user@example.com", "pw", "Europe")

    cameras = await api.async_get_cameras()
    assert len(cameras) == 1
    cam = cameras[0]
    assert cam.serial == "SN1"
    assert cam.name == "Front door"  # from deviceInfos[].name, not the serial
    assert cam.vtm_ip == "10.0.0.1"
    assert cam.vtm_port == 6001
    assert cam.biz == "biz=1"
    assert cam.is_online


@pytest.mark.parametrize(
    ("code", "error"),
    [(1014, InvalidAuth), (6002, MfaRequired)],
)
async def test_login_errors(code: int, error: type[Exception]) -> None:
    api = EzvizCloudApi(_session({"users/login": {"meta": {"code": code}}}))
    with pytest.raises(error):
        await api.async_login("user@example.com", "pw", "Europe")


async def test_login_region_redirect() -> None:
    routes = {
        "apiieu.ezvizlife.com/v3/users/login": {
            "meta": {"code": 1100},
            "loginArea": {"apiDomain": "apiius.ezvizlife.com"},
        },
        "apiius.ezvizlife.com/v3/users/login": _LOGIN_OK,
    }
    api = EzvizCloudApi(_session(routes))
    await api.async_login("user@example.com", "pw", "Europe")  # should not raise


async def test_get_vtdu_token() -> None:
    routes = {
        "users/login": _LOGIN_OK,
        "server/info/get": {"authAddr": "euauth.ezvizlife.com"},
        "vtdutoken2": {"tokens": ["TOK123"]},
    }
    api = EzvizCloudApi(_session(routes))
    await api.async_login("user@example.com", "pw", "Europe")
    assert await api.async_get_vtdu_token() == "TOK123"


async def test_get_vtdu_token_none_returned() -> None:
    routes = {
        "users/login": _LOGIN_OK,
        "server/info/get": {"authAddr": "euauth.ezvizlife.com"},
        "vtdutoken2": {"retcode": "1"},
    }
    api = EzvizCloudApi(_session(routes))
    await api.async_login("user@example.com", "pw", "Europe")
    with pytest.raises(CannotConnect):
        await api.async_get_vtdu_token()
