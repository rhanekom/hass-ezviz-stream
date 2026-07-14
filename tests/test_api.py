"""Tests for the async EZVIZ cloud control-plane client."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.ezviz_stream import api as api_module
from custom_components.ezviz_stream.api import (
    CannotConnect,
    EzvizCloudApi,
    EzvizStreamApiError,
    InvalidAuth,
    InvalidRegion,
    MfaRequired,
    _api_host,
    _first_image_url,
    _normalise_host,
)
from custom_components.ezviz_stream.decrypt_image import StillImageDecryptError

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
    "STATUS": {"SN1": {"isEncrypt": 1, "encryptPwd": "deadbeefhash"}},
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
    assert cam.is_encrypted is True  # from STATUS.isEncrypt
    # S105: a test fixture hash, not a real secret.
    assert cam.encrypt_pwd_hash == "deadbeefhash"  # noqa: S105
    assert cam.picker_label == "Front door (SN1)"  # name + serial for the picker


async def test_is_encrypted_tristate() -> None:
    """Encryption is True/False when the device reports it, None when it does not."""

    def _pagelist(status: dict[str, Any]) -> dict[str, Any]:
        return {**_PAGELIST, "STATUS": status}

    api_off = EzvizCloudApi(
        _session(
            {"users/login": _LOGIN_OK, "pagelist": _pagelist({"SN1": {"isEncrypt": 0}})}
        )
    )
    await api_off.async_login("user@example.com", "pw", "Europe")
    assert (await api_off.async_get_cameras())[0].is_encrypted is False

    api_unknown = EzvizCloudApi(
        _session({"users/login": _LOGIN_OK, "pagelist": _pagelist({})})
    )
    await api_unknown.async_login("user@example.com", "pw", "Europe")
    assert (await api_unknown.async_get_cameras())[0].is_encrypted is None


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


async def test_get_last_motion_image_plaintext() -> None:
    """The latest alarm's picUrl is fetched and returned (no wake, unencrypted)."""
    alarm = {"meta": {"code": 200}, "alarms": [{"picUrl": "https://oss.example/x.jpg"}]}
    image_bytes = b"\xff\xd8 plain jpeg from the cloud"
    session = _session({"users/login": _LOGIN_OK, "alarms/v2/advanced": alarm})

    async def _get(url: str, **_kwargs: Any) -> MagicMock:
        assert url == "https://oss.example/x.jpg"
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=image_bytes)
        return resp

    session.get = _get
    api = EzvizCloudApi(session)
    await api.async_login("user@example.com", "pw", "Europe")
    assert await api.async_get_last_motion_image("SN1") == image_bytes


async def test_get_last_motion_image_none_when_no_alarm() -> None:
    """No alarm (or no image URL) yields None rather than an error."""
    alarm = {"meta": {"code": 200}, "alarms": []}
    session = _session({"users/login": _LOGIN_OK, "alarms/v2/advanced": alarm})
    api = EzvizCloudApi(session)
    await api.async_login("user@example.com", "pw", "Europe")
    assert await api.async_get_last_motion_image("SN1") is None


async def test_get_last_motion_returns_image_and_epoch_seconds() -> None:
    """async_get_last_motion returns the image plus the event time in epoch seconds."""
    alarm = {
        "meta": {"code": 200},
        "alarms": [{"picUrl": "https://oss.example/x.jpg", "alarmTime": 1700000000000}],
    }
    session = _session({"users/login": _LOGIN_OK, "alarms/v2/advanced": alarm})

    async def _get(_url: str, **_kwargs: Any) -> MagicMock:
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=b"IMG")
        return resp

    session.get = _get
    api = EzvizCloudApi(session)
    await api.async_login("user@example.com", "pw", "Europe")
    motion = await api.async_get_last_motion("SN1")
    assert motion is not None
    assert motion.image == b"IMG"
    assert motion.timestamp == 1700000000.0  # epoch ms normalised to seconds


# --- pure helpers ----------------------------------------------------------- #
def test_api_host_unknown_region_raises() -> None:
    with pytest.raises(InvalidRegion):
        _api_host("Atlantis")


def test_first_image_url_returns_none_for_non_urls() -> None:
    assert _first_image_url(None) is None  # not a string
    assert _first_image_url("ftp://x;just-text") is None  # no http(s) part
    assert _first_image_url("nope;https://ok/img.jpg") == "https://ok/img.jpg"


def test_normalise_host_handles_empty() -> None:
    assert _normalise_host(None) is None
    assert _normalise_host("") is None
    assert _normalise_host("host") == "https://host"
    assert _normalise_host("http://host") == "http://host"


# --- login edges ------------------------------------------------------------ #
async def test_login_unknown_region_raises() -> None:
    api = EzvizCloudApi(_session({}))
    with pytest.raises(InvalidRegion):
        await api.async_login("user@example.com", "pw", "Atlantis")


async def test_login_exhausts_region_redirects() -> None:
    """Repeated region redirects run out of retries and surface CannotConnect."""
    routes = {
        "users/login": {
            "meta": {"code": 1100},
            "loginArea": {"apiDomain": "apiius.ezvizlife.com"},
        }
    }
    api = EzvizCloudApi(_session(routes))
    with pytest.raises(CannotConnect):
        await api.async_login("user@example.com", "pw", "Europe")


async def test_login_region_redirect_without_host_raises() -> None:
    routes = {"users/login": {"meta": {"code": 1100}, "loginArea": {}}}
    api = EzvizCloudApi(_session(routes))
    with pytest.raises(CannotConnect):
        await api.async_login("user@example.com", "pw", "Europe")


async def test_login_success_without_session_id_raises() -> None:
    routes = {"users/login": {"meta": {"code": 200}, "loginSession": {}}}
    api = EzvizCloudApi(_session(routes))
    with pytest.raises(CannotConnect):
        await api.async_login("user@example.com", "pw", "Europe")


# --- not-logged-in guards --------------------------------------------------- #
async def test_get_cameras_requires_login() -> None:
    api = EzvizCloudApi(_session({}))
    with pytest.raises(EzvizStreamApiError):
        await api.async_get_cameras()


async def test_get_last_motion_image_requires_login() -> None:
    api = EzvizCloudApi(_session({}))
    with pytest.raises(EzvizStreamApiError):
        await api.async_get_last_motion_image("SN1")


async def test_get_vtdu_token_requires_login() -> None:
    api = EzvizCloudApi(_session({}))
    with pytest.raises(EzvizStreamApiError):
        await api.async_get_vtdu_token()


# --- camera discovery edges ------------------------------------------------- #
async def test_get_cameras_skips_non_streamable_resources() -> None:
    """Resources with no serial or a non-positive resourceType are ignored."""
    pagelist = {
        "resourceInfos": [
            {"deviceSerial": "SNX", "resourceId": "R9", "resourceType": 0}
        ],
        "deviceInfos": [],
        "VTM": {},
        "STATUS": {},
    }
    api = EzvizCloudApi(_session({"users/login": _LOGIN_OK, "pagelist": pagelist}))
    await api.async_login("user@example.com", "pw", "Europe")
    assert await api.async_get_cameras() == []


# --- motion image edges ----------------------------------------------------- #
async def test_get_last_motion_image_none_when_meta_not_ok() -> None:
    alarm = {"meta": {"code": 500}}
    session = _session({"users/login": _LOGIN_OK, "alarms/v2/advanced": alarm})
    api = EzvizCloudApi(session)
    await api.async_login("user@example.com", "pw", "Europe")
    assert await api.async_get_last_motion_image("SN1") is None


def _alarm_session_with_get(status: int, data: bytes) -> MagicMock:
    """A session whose alarm query yields one picUrl and whose GET returns ``data``."""
    alarm = {"meta": {"code": 200}, "alarms": [{"picUrl": "https://oss.example/x.jpg"}]}
    session = _session({"users/login": _LOGIN_OK, "alarms/v2/advanced": alarm})

    async def _get(_url: str, **_kwargs: Any) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.read = AsyncMock(return_value=data)
        return resp

    session.get = _get
    return session


async def test_get_last_motion_image_decrypts_encrypted_payload() -> None:
    """An encrypted (hikencodepicture) image is decrypted with the code."""
    encrypted = api_module.HIK_ENCRYPTION_HEADER + b"ciphertext"
    api = EzvizCloudApi(_alarm_session_with_get(200, encrypted))
    await api.async_login("user@example.com", "pw", "Europe")
    with patch.object(api_module, "decrypt_still_image", return_value=b"PLAINJPEG"):
        result = await api.async_get_last_motion_image(
            "SN1", verification_code="123456"
        )
    assert result == b"PLAINJPEG"


async def test_get_last_motion_image_none_on_decrypt_failure() -> None:
    """A wrong verification code (decrypt raises) yields None, not an error."""
    encrypted = api_module.HIK_ENCRYPTION_HEADER + b"ciphertext"
    api = EzvizCloudApi(_alarm_session_with_get(200, encrypted))
    await api.async_login("user@example.com", "pw", "Europe")
    with patch.object(
        api_module,
        "decrypt_still_image",
        side_effect=StillImageDecryptError("wrong code"),
    ):
        assert await api.async_get_last_motion_image("SN1") is None


async def test_get_last_motion_image_none_when_download_not_ok() -> None:
    api = EzvizCloudApi(_alarm_session_with_get(404, b""))
    await api.async_login("user@example.com", "pw", "Europe")
    assert await api.async_get_last_motion_image("SN1") is None


async def test_get_last_motion_image_none_when_download_errors() -> None:
    alarm = {"meta": {"code": 200}, "alarms": [{"picUrl": "https://oss.example/x.jpg"}]}
    session = _session({"users/login": _LOGIN_OK, "alarms/v2/advanced": alarm})

    async def _get(_url: str, **_kwargs: Any) -> MagicMock:
        raise aiohttp.ClientError("network down")

    session.get = _get
    api = EzvizCloudApi(session)
    await api.async_login("user@example.com", "pw", "Europe")
    assert await api.async_get_last_motion_image("SN1") is None


# --- vtdu token + auth addr ------------------------------------------------- #
async def test_get_vtdu_token_rejects_undecodable_session_id() -> None:
    """A session id whose JWT payload will not decode surfaces CannotConnect."""
    login = {
        "meta": {"code": 200},
        "loginSession": {"sessionId": "aaa.bbb.ccc"},  # 'bbb' is not JSON
        "loginArea": {"apiDomain": "apiieu.ezvizlife.com"},
    }
    routes = {"users/login": login, "server/info/get": {"authAddr": "euauth"}}
    api = EzvizCloudApi(_session(routes))
    await api.async_login("user@example.com", "pw", "Europe")
    with pytest.raises(CannotConnect):
        await api.async_get_vtdu_token()


async def test_auth_addr_caches_and_falls_back_to_host_on_error() -> None:
    """A failed server/info/get falls back to the API host, and the result caches."""
    api = EzvizCloudApi(_session({}))
    api._session_id = "sid"
    api._host = "https://apiieu.ezvizlife.com"
    with patch.object(
        api, "_post", AsyncMock(side_effect=CannotConnect("boom"))
    ) as post:
        first = await api._async_auth_addr()
        second = await api._async_auth_addr()  # cached: no second _post
    assert first == "https://apiieu.ezvizlife.com"
    assert second == first
    post.assert_awaited_once()


# --- request wrapping ------------------------------------------------------- #
async def test_request_wraps_client_error() -> None:
    session = MagicMock()
    session.request = AsyncMock(side_effect=aiohttp.ClientError("net"))
    api = EzvizCloudApi(session)
    with pytest.raises(CannotConnect):
        await api._get("https://x")


async def test_request_wraps_non_json_body() -> None:
    resp = MagicMock()
    resp.json = AsyncMock(side_effect=ValueError("not json"))
    session = MagicMock()
    session.request = AsyncMock(return_value=resp)
    api = EzvizCloudApi(session)
    with pytest.raises(CannotConnect):
        await api._get("https://x")


async def test_request_rejects_non_dict_payload() -> None:
    resp = MagicMock()
    resp.json = AsyncMock(return_value=["not", "a", "dict"])
    session = MagicMock()
    session.request = AsyncMock(return_value=resp)
    api = EzvizCloudApi(session)
    with pytest.raises(CannotConnect):
        await api._get("https://x")
