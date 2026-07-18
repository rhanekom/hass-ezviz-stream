"""Tests for the async EZVIZ cloud control-plane client."""

from __future__ import annotations

import base64
import json
import zlib
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
    _cas_time,
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
    resp = MagicMock()
    resp.status = 200
    resp.read = AsyncMock(return_value=b"IMG")
    session.get = AsyncMock(return_value=resp)
    api = EzvizCloudApi(session)
    await api.async_login("user@example.com", "pw", "Europe")
    motion = await api.async_get_last_motion("SN1")
    assert motion is not None
    assert motion.image == b"IMG"
    assert motion.timestamp == pytest.approx(1700000000.0)  # epoch ms -> seconds


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
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=data)
    session.get = AsyncMock(return_value=resp)
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


# --- cloud recordings (playback control plane) ------------------------------ #
# coverPic carries the precise ms start (1752661800000); videoLong is 20 s.
_CLOUD_VIDEOS = {
    "meta": {"code": 200},
    "videos": [
        {
            "seqId": "SEQ1",
            "startTime": "2026-07-16 10:30:00",
            "stopTime": "2026-07-16 10:30:20",
            "fileSize": 123456,
            "crypt": 1,
            "keyChecksum": "abc123",
            "streamUrl": "cas.example.com:6500",
            "storageVersion": 2,
            "videoLong": 20000,
            "coverPic": "https://oss.example/c.jpg?startTime=1752661800000",
        }
    ],
}


async def _logged_in(routes: dict[str, Any]) -> EzvizCloudApi:
    api = EzvizCloudApi(_session({"users/login": _LOGIN_OK, **routes}))
    await api.async_login("user@example.com", "pw", "Europe")
    return api


def test_cas_time_formats_utc() -> None:
    """CAS timestamps are UTC, no separators, second precision."""
    assert _cas_time(0) == "19700101T000000Z"
    assert _cas_time(1000) == "19700101T000001Z"


async def test_get_cloud_videos_parses_descriptors() -> None:
    api = await _logged_in({"clouds/videos/list": _CLOUD_VIDEOS})
    recordings = await api.async_get_cloud_videos("SN1", 1)
    assert len(recordings) == 1
    rec = recordings[0]
    assert rec.seq_id == "SEQ1"
    assert rec.file_size == 123456
    assert rec.crypt is True
    assert rec.key_checksum == "abc123"
    assert rec.storage_version == 2
    assert rec.video_long == 20000
    assert rec.stream_url == "cas.example.com:6500"
    # Precise start comes from coverPic; stop = start + duration.
    assert rec.start_millis == 1752661800000
    assert rec.stop_millis == 1752661820000
    assert rec.begin_cas == _cas_time(1752661800000)
    assert rec.end_cas == _cas_time(1752661820000)


async def test_get_cloud_videos_falls_back_to_start_time_string() -> None:
    """With no coverPic, the UTC startTime string gives the ms start."""
    video = {"seqId": "S2", "startTime": "2026-07-16 10:30:00", "videoLong": 5000}
    api = await _logged_in(
        {"clouds/videos/list": {"meta": {"code": 200}, "videos": [video]}}
    )
    rec = (await api.async_get_cloud_videos("SN1", 1))[0]
    expected = int(
        api_module.dt.datetime(
            2026, 7, 16, 10, 30, tzinfo=api_module.dt.UTC
        ).timestamp()
        * 1000
    )
    assert rec.start_millis == expected
    assert rec.file_size is None  # absent fileSize -> None


async def test_get_cloud_videos_skips_entries_without_seq_id() -> None:
    videos = [{"startTime": "x"}, {"seqId": "S3", "videoLong": 0}]
    api = await _logged_in(
        {"clouds/videos/list": {"meta": {"code": 200}, "videos": videos}}
    )
    recordings = await api.async_get_cloud_videos("SN1", 1)
    assert [r.seq_id for r in recordings] == ["S3"]


async def test_get_cloud_videos_empty_when_meta_not_ok() -> None:
    api = await _logged_in({"clouds/videos/list": {"meta": {"code": 500}}})
    assert await api.async_get_cloud_videos("SN1", 1) == []


async def test_get_cloud_videos_requires_login() -> None:
    api = EzvizCloudApi(_session({}))
    with pytest.raises(EzvizStreamApiError):
        await api.async_get_cloud_videos("SN1", 1)


async def test_get_camera_ticket_returns_ticket() -> None:
    ticket = {"meta": {"code": 200}, "ticketInfo": {"ticket": "TICKET123"}}
    api = await _logged_in({"cameras/ticketInfo": ticket})
    assert await api.async_get_camera_ticket("SN1", 1) == "TICKET123"


async def test_get_camera_ticket_raises_when_missing() -> None:
    api = await _logged_in({"cameras/ticketInfo": {"meta": {"code": 200}}})
    with pytest.raises(CannotConnect):
        await api.async_get_camera_ticket("SN1", 1)


async def test_get_camera_ticket_requires_login() -> None:
    api = EzvizCloudApi(_session({}))
    with pytest.raises(EzvizStreamApiError):
        await api.async_get_camera_ticket("SN1", 1)


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


# --- SD-card recordings (records search + playback control plane) ----------- #
_SD_RECORDS = {
    "meta": {"code": 200},
    "records": [
        {"begin": "2026-07-17 10:35:48", "end": "2026-07-17 10:36:05", "type": 1},
        {"begin": 1752748800000, "end": 1752748860000},  # epoch ms
    ],
}


def test_search_time_is_utc_datetime_string() -> None:
    assert api_module._search_time(0) == "1970-01-01 00:00:00"


def test_record_millis_accepts_datetime_epoch_and_junk() -> None:
    assert api_module._record_millis("2026-07-17 10:35:48") is not None
    assert api_module._record_millis(1752748800000) == 1752748800000  # epoch ms as-is
    assert api_module._record_millis(1752748800) == 1752748800000  # epoch s -> ms
    assert api_module._record_millis("not-a-time") is None
    assert api_module._record_millis(None) is None


async def test_search_records_parses_segments() -> None:
    api = await _logged_in({"streaming/v2/records": _SD_RECORDS})
    recs = await api.async_search_records("SN1", 1, start_millis=0, stop_millis=1)
    assert len(recs) == 2
    first = recs[0]
    assert first.begin_cas == "20260717T103548Z"  # round-trips via _cas_time
    assert first.end_cas == "20260717T103605Z"
    assert first.duration_ms == 17000
    assert first.record_type == 1


async def test_search_records_decodes_compressed_payload() -> None:
    recs = [{"begin": "2026-07-17 10:00:00", "end": "2026-07-17 10:00:10"}]
    blob = base64.b64encode(zlib.compress(json.dumps(recs).encode())).decode()
    api = await _logged_in(
        {"streaming/v2/records": {"meta": {"code": 200}, "records": blob}}
    )
    out = await api.async_search_records("SN1", 1, start_millis=0, stop_millis=1)
    assert len(out) == 1
    assert out[0].begin_cas == "20260717T100000Z"


async def test_search_records_empty_on_device_exception() -> None:
    api = await _logged_in({"streaming/v2/records": {"meta": {"code": 2004}}})
    assert await api.async_search_records("SN1", 1, start_millis=0, stop_millis=1) == []


async def test_search_records_requires_login() -> None:
    api = EzvizCloudApi(_session({}))
    with pytest.raises(EzvizStreamApiError):
        await api.async_search_records("SN1", 1, start_millis=0, stop_millis=1)
