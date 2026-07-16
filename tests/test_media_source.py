"""Tests for the EZVIZ Stream recordings media source (browse + resolve)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.media_source import MediaSourceItem, Unresolvable
from homeassistant.config_entries import ConfigEntryState

from custom_components.ezviz_stream.api import CloudRecording, EzvizCamera
from custom_components.ezviz_stream.const import (
    CAMERA_SUBENTRY_TYPE,
    CONF_ENABLE_RECORDINGS,
    CONF_RECORDINGS_MODE,
    CONF_SERIAL,
    DOMAIN,
    RECORDINGS_MODE_OFF,
    RECORDINGS_MODE_ON,
)
from custom_components.ezviz_stream.media_source import (
    EzvizRecordingsMediaSource,
    async_get_media_source,
)
from custom_components.ezviz_stream.stream_view import register_stream

_CAMERA = EzvizCamera(
    serial="SN1",
    name="Front door",
    category="IPC",
    channel=1,
    status=1,
    streamable=True,
)
_RECORDING = CloudRecording(
    seq_id="SEQ1",
    start_time="2026-07-16 10:30:00",
    stop_time="2026-07-16 10:30:12",
    start_millis=1752661800000,
    video_long=12000,
    file_size=1044612,
    storage_version=2,
    crypt=True,
    key_checksum="abc",
    stream_url="cas.example.com:6500",
)


def _fake_hass_with_camera() -> tuple[MagicMock, AsyncMock]:
    """A hass whose single loaded account has one camera subentry."""
    api = MagicMock()
    api.async_get_cameras = AsyncMock(return_value=[_CAMERA])
    api.async_get_cloud_videos = AsyncMock(return_value=[_RECORDING])

    subentry = SimpleNamespace(
        subentry_type=CAMERA_SUBENTRY_TYPE,
        data={CONF_SERIAL: "SN1"},
        title="Front door",
    )
    entry = SimpleNamespace(
        state=ConfigEntryState.LOADED,
        subentries={"sub1": subentry},
        runtime_data=SimpleNamespace(api=api),
        options={
            CONF_ENABLE_RECORDINGS: True
        },  # recordings are opt-in (off by default)
    )
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_entries.return_value = [entry]
    return hass, api


def _item(hass: Any, identifier: str) -> MediaSourceItem:
    return MediaSourceItem(hass, DOMAIN, identifier, None)


async def test_async_get_media_source_returns_source() -> None:
    source = await async_get_media_source(MagicMock())
    assert isinstance(source, EzvizRecordingsMediaSource)


async def test_browse_root_lists_cameras() -> None:
    hass, _api = _fake_hass_with_camera()
    source = EzvizRecordingsMediaSource(hass)
    root = await source.async_browse_media(_item(hass, ""))
    assert root.can_expand
    assert not root.can_play
    assert root.children is not None
    assert len(root.children) == 1
    assert root.children[0].identifier == "SN1"
    assert root.children[0].title == "Front door"


async def test_browse_root_empty_when_recordings_disabled() -> None:
    """With the account option off (the default), no cameras are exposed."""
    hass, _api = _fake_hass_with_camera()
    hass.config_entries.async_entries.return_value[0].options = {}  # default: disabled
    source = EzvizRecordingsMediaSource(hass)
    root = await source.async_browse_media(_item(hass, ""))
    assert root.children == []


async def test_camera_override_on_shows_despite_account_off() -> None:
    """A camera set to 'on' appears even when the account default is off."""
    hass, _api = _fake_hass_with_camera()
    entry = hass.config_entries.async_entries.return_value[0]
    entry.options = {}  # account default: off
    entry.subentries["sub1"].data[CONF_RECORDINGS_MODE] = RECORDINGS_MODE_ON
    source = EzvizRecordingsMediaSource(hass)
    root = await source.async_browse_media(_item(hass, ""))
    assert root.children is not None
    assert [c.identifier for c in root.children] == ["SN1"]


async def test_camera_override_off_hides_despite_account_on() -> None:
    """A camera set to 'off' is hidden even when the account default is on."""
    hass, _api = _fake_hass_with_camera()  # account on
    hass.config_entries.async_entries.return_value[0].subentries["sub1"].data[
        CONF_RECORDINGS_MODE
    ] = RECORDINGS_MODE_OFF
    source = EzvizRecordingsMediaSource(hass)
    root = await source.async_browse_media(_item(hass, ""))
    assert root.children == []


async def test_browse_camera_lists_recordings() -> None:
    hass, api = _fake_hass_with_camera()
    source = EzvizRecordingsMediaSource(hass)
    node = await source.async_browse_media(_item(hass, "SN1"))
    api.async_get_cloud_videos.assert_awaited_once_with("SN1", 1)
    assert node.children is not None
    assert len(node.children) == 1
    clip = node.children[0]
    assert clip.identifier == "SN1/SEQ1"
    assert clip.can_play
    assert not clip.can_expand


async def test_browse_unknown_camera_raises() -> None:
    hass, _api = _fake_hass_with_camera()
    source = EzvizRecordingsMediaSource(hass)
    with pytest.raises(Unresolvable):
        await source.async_browse_media(_item(hass, "NOPE"))


async def test_browse_clip_is_not_a_folder() -> None:
    hass, _api = _fake_hass_with_camera()
    source = EzvizRecordingsMediaSource(hass)
    with pytest.raises(Unresolvable):
        await source.async_browse_media(_item(hass, "SN1/SEQ1"))


async def test_resolve_builds_token_guarded_url() -> None:
    hass, api = _fake_hass_with_camera()
    register_stream(hass, "SN1", "TOK", MagicMock(), api, "123456")
    source = EzvizRecordingsMediaSource(hass)
    media = await source.async_resolve_media(_item(hass, "SN1/SEQ1"))
    assert media.mime_type == "video/mp4"
    assert media.url == "/api/ezviz_stream/SN1/replay/SEQ1?token=TOK"


async def test_resolve_without_registered_camera_raises() -> None:
    hass, _api = _fake_hass_with_camera()  # nothing registered in the stream registry
    source = EzvizRecordingsMediaSource(hass)
    with pytest.raises(Unresolvable):
        await source.async_resolve_media(_item(hass, "SN1/SEQ1"))


async def test_resolve_bad_identifier_raises() -> None:
    hass, _api = _fake_hass_with_camera()
    source = EzvizRecordingsMediaSource(hass)
    with pytest.raises(Unresolvable):
        await source.async_resolve_media(_item(hass, "SN1"))  # no seq id
