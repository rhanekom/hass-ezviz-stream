"""Tests for the token-guarded MPEG-TS media view."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from homeassistant.setup import async_setup_component

from custom_components.ezviz_stream.stream_view import (
    EzvizStreamMediaView,
    register_stream,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.core import HomeAssistant


class _FakeBroadcast:
    """A broadcaster that yields a fixed pair of MPEG-TS chunks."""

    async def subscribe(self) -> AsyncIterator[bytes]:
        yield b"AB"
        yield b"CD"


async def _client(hass: HomeAssistant, hass_client_no_auth):  # noqa: ANN001, ANN202
    """Register the view (before the server starts) and return a test client."""
    assert await async_setup_component(hass, "http", {})
    hass.http.register_view(EzvizStreamMediaView(hass))
    return await hass_client_no_auth()


async def test_get_streams_for_valid_token(hass, hass_client_no_auth) -> None:  # noqa: ANN001
    """A correct token streams the broadcaster's MPEG-TS chunks."""
    register_stream(hass, "SN1", "good-token", _FakeBroadcast(), MagicMock(), "")
    client = await _client(hass, hass_client_no_auth)

    resp = await client.get("/api/ezviz_stream/SN1?token=good-token")
    assert resp.status == 200
    assert resp.content_type == "video/mp2t"
    assert await resp.read() == b"ABCD"


async def test_bad_token_is_not_found(hass, hass_client_no_auth) -> None:  # noqa: ANN001
    """A wrong token is rejected as 404 (indistinguishable from an unknown serial)."""
    register_stream(hass, "SN1", "good-token", _FakeBroadcast(), MagicMock(), "")
    client = await _client(hass, hass_client_no_auth)

    resp = await client.get("/api/ezviz_stream/SN1?token=wrong")
    assert resp.status == 404


async def test_unknown_serial_is_not_found(hass, hass_client_no_auth) -> None:  # noqa: ANN001
    """An unregistered serial is 404 even with a non-empty token."""
    client = await _client(hass, hass_client_no_auth)

    resp = await client.get("/api/ezviz_stream/NOPE?token=whatever")
    assert resp.status == 404


# --- replay view (cloud + SD) ------------------------------------------------- #
def _cloud_rec(seq: str = "42"):  # noqa: ANN202
    """A CloudRecording with valid begin/end CAS (start_millis set) and a stream URL."""
    from custom_components.ezviz_stream.api import CloudRecording  # noqa: PLC0415

    return CloudRecording(
        seq_id=seq,
        start_time="2026-07-17 10:00:00",
        stop_time="2026-07-17 10:00:20",
        start_millis=1_700_000_000_000,
        video_long=20000,
        file_size=1000,
        storage_version=2,
        crypt=True,
        key_checksum="",
        stream_url="host:6001",
    )


def _replay_api(*, cameras=None, videos=None, videos_error=False):  # noqa: ANN001, ANN202
    """A mock cloud API for the replay view."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from custom_components.ezviz_stream.api import (  # noqa: PLC0415
        EzvizCamera,
        EzvizStreamApiError,
    )

    api = AsyncMock()
    api.async_get_cameras = AsyncMock(
        return_value=[EzvizCamera("SN1", "Cam", "IPC", 1, 1, streamable=True)]
        if cameras is None
        else cameras
    )
    if videos_error:
        api.async_get_cloud_videos = AsyncMock(side_effect=EzvizStreamApiError("boom"))
    else:
        api.async_get_cloud_videos = AsyncMock(
            return_value=[_cloud_rec()] if videos is None else videos
        )
    api.async_get_camera_ticket = AsyncMock(return_value="ticket")
    return api


def _fake_replay_source(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
    """Stand in for broadcast.replay_mp4_source: yield a fixed MP4 chunk."""

    async def _gen() -> AsyncIterator[bytes]:
        yield b"MP4DATA"

    return _gen()


async def _replay_client(hass, hass_client_no_auth, api):  # noqa: ANN001, ANN202
    """Register the replay view + a stream keyed to SN1 and return a client."""
    from types import SimpleNamespace  # noqa: PLC0415

    from custom_components.ezviz_stream.stream_view import (  # noqa: PLC0415
        EzvizReplayView,
    )

    assert await async_setup_component(hass, "http", {})
    register_stream(hass, "SN1", "good-token", _FakeBroadcast(), api, "CODE")
    hass.http.register_view(EzvizReplayView(hass))
    patcher = patch.multiple(
        "custom_components.ezviz_stream.stream_view",
        replay_mp4_source=_fake_replay_source,
        get_ffmpeg_manager=MagicMock(return_value=SimpleNamespace(binary="ffmpeg")),
    )
    patcher.start()
    client = await hass_client_no_auth()
    return client, patcher


async def test_replay_cloud_streams_mp4(hass, hass_client_no_auth) -> None:  # noqa: ANN001
    """A valid cloud clip id streams transcoded fragmented MP4."""
    client, patcher = await _replay_client(hass, hass_client_no_auth, _replay_api())
    try:
        resp = await client.get(
            "/api/ezviz_stream/SN1/replay/cloud/42?token=good-token"
        )
        assert resp.status == 200
        assert resp.content_type == "video/mp4"
        assert await resp.read() == b"MP4DATA"
    finally:
        patcher.stop()


async def test_replay_sd_streams_mp4(hass, hass_client_no_auth) -> None:  # noqa: ANN001
    """A valid SD segment ident (begin-end millis) streams transcoded fragmented MP4."""
    client, patcher = await _replay_client(hass, hass_client_no_auth, _replay_api())
    try:
        resp = await client.get(
            "/api/ezviz_stream/SN1/replay/sd/1000-2000?token=good-token"
        )
        assert resp.status == 200
        assert await resp.read() == b"MP4DATA"
    finally:
        patcher.stop()


async def test_replay_bad_token_is_not_found(hass, hass_client_no_auth) -> None:  # noqa: ANN001
    """A wrong token on the replay view is 404."""
    client, patcher = await _replay_client(hass, hass_client_no_auth, _replay_api())
    try:
        resp = await client.get("/api/ezviz_stream/SN1/replay/cloud/42?token=wrong")
        assert resp.status == 404
    finally:
        patcher.stop()


async def test_replay_unknown_kind_is_not_found(hass, hass_client_no_auth) -> None:  # noqa: ANN001
    """A kind that is neither cloud nor sd is 404."""
    client, patcher = await _replay_client(hass, hass_client_no_auth, _replay_api())
    try:
        resp = await client.get(
            "/api/ezviz_stream/SN1/replay/bogus/42?token=good-token"
        )
        assert resp.status == 404
    finally:
        patcher.stop()


async def test_replay_unknown_clip_is_not_found(hass, hass_client_no_auth) -> None:  # noqa: ANN001
    """A cloud seq id that matches no clip is 404 (source resolves to None)."""
    client, patcher = await _replay_client(hass, hass_client_no_auth, _replay_api())
    try:
        resp = await client.get(
            "/api/ezviz_stream/SN1/replay/cloud/999?token=good-token"
        )
        assert resp.status == 404
    finally:
        patcher.stop()


async def test_replay_missing_camera_is_not_found(hass, hass_client_no_auth) -> None:  # noqa: ANN001
    """A serial not found on the account is 404."""
    client, patcher = await _replay_client(
        hass, hass_client_no_auth, _replay_api(cameras=[])
    )
    try:
        resp = await client.get(
            "/api/ezviz_stream/SN1/replay/cloud/42?token=good-token"
        )
        assert resp.status == 404
    finally:
        patcher.stop()


async def test_replay_api_error_is_not_found(hass, hass_client_no_auth) -> None:  # noqa: ANN001
    """An API error while resolving the clip is surfaced as 404."""
    client, patcher = await _replay_client(
        hass, hass_client_no_auth, _replay_api(videos_error=True)
    )
    try:
        resp = await client.get(
            "/api/ezviz_stream/SN1/replay/cloud/42?token=good-token"
        )
        assert resp.status == 404
    finally:
        patcher.stop()
