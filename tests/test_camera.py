"""Tests for the EZVIZ Stream camera platform."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigSubentry, ConfigSubentryData
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_stream.api import EzvizCamera
from custom_components.ezviz_stream.camera import EzvizStreamCamera
from custom_components.ezviz_stream.const import (
    CAMERA_SUBENTRY_TYPE,
    CONF_REGION,
    CONF_SERIAL,
    CONF_VERIFICATION_CODE,
    DOMAIN,
    OFFICIAL_EZVIZ_DOMAIN,
)
from custom_components.ezviz_stream.stream_view import DATA_STREAMS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_ACCOUNT = {
    CONF_USERNAME: "user@example.com",
    CONF_PASSWORD: "hunter2",
    CONF_REGION: "Europe",
}


def _entry_with_camera() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data=_ACCOUNT,
        subentries_data=[
            ConfigSubentryData(
                data={CONF_SERIAL: "SN1", CONF_VERIFICATION_CODE: "ABCDEF"},
                subentry_type=CAMERA_SUBENTRY_TYPE,
                title="Front door",
                unique_id="SN1",
            )
        ],
    )


async def test_camera_entity_created_per_subentry(hass: HomeAssistant) -> None:
    """Account setup creates a camera entity per subentry on the EZVIZ device."""
    entry = _entry_with_camera()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ezviz_stream.EzvizCloudApi",
        return_value=AsyncMock(async_login=AsyncMock(return_value=None)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id("camera", DOMAIN, "SN1")
    assert entity_id is not None
    assert hass.states.get(entity_id) is not None

    device = dr.async_get(hass).async_get_device(
        identifiers={(OFFICIAL_EZVIZ_DOMAIN, "SN1")}
    )
    assert device is not None
    assert device.serial_number == "SN1"


async def test_camera_created_when_subentry_added_after_setup(
    hass: HomeAssistant,
) -> None:
    """A camera added as a subentry after setup gets its entity (reload listener)."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user@example.com", data=_ACCOUNT)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.ezviz_stream.EzvizCloudApi",
        return_value=AsyncMock(async_login=AsyncMock(return_value=None)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        assert registry.async_get_entity_id("camera", DOMAIN, "SN9") is None

        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data={CONF_SERIAL: "SN9", CONF_VERIFICATION_CODE: ""},
                subentry_type=CAMERA_SUBENTRY_TYPE,
                title="New cam",
                unique_id="SN9",
            ),
        )
        await hass.async_block_till_done()

    assert er.async_get(hass).async_get_entity_id("camera", DOMAIN, "SN9") is not None


async def test_snapshot_cached_within_ttl(hass: HomeAssistant) -> None:
    """A second image request within the TTL is served from cache (no re-grab)."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(
        return_value=[
            EzvizCamera(
                "SN1",
                "Front door",
                "IPC",
                1,
                1,
                streamable=True,
                vtm_ip="1.1.1.1",
                vtm_port=6001,
            )
        ]
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(api=api, stream_semaphore=asyncio.Semaphore(1))
    )
    subentry = SimpleNamespace(
        data={CONF_SERIAL: "SN1", CONF_VERIFICATION_CODE: ""},
        title="Front door",
        subentry_id="x",
    )
    camera = EzvizStreamCamera(entry, subentry)
    camera.hass = hass

    with (
        patch(
            "custom_components.ezviz_stream.camera.grab_jpeg",
            AsyncMock(return_value=b"J" * 6000),
        ) as grab,
        patch(
            "custom_components.ezviz_stream.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="ffmpeg"),
        ),
    ):
        first = await camera.async_camera_image()
        second = await camera.async_camera_image()

    assert first == b"J" * 6000
    assert second == first
    assert grab.call_count == 1  # second call served from cache


async def test_stream_source_and_registry_lifecycle(hass: HomeAssistant) -> None:
    """stream_source is a token-guarded local HTTP URL; add/remove (de)registers it."""
    entry = SimpleNamespace(
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "hunter2",
            CONF_REGION: "Europe",
        },
        runtime_data=SimpleNamespace(
            api=AsyncMock(), stream_semaphore=asyncio.Semaphore(1)
        ),
    )
    subentry = SimpleNamespace(
        data={CONF_SERIAL: "SN1", CONF_VERIFICATION_CODE: "ABCDEF"},
        title="Front door",
        subentry_id="x",
    )
    camera = EzvizStreamCamera(entry, subentry)
    camera.hass = hass
    hass.http = SimpleNamespace(server_port=8123)

    await camera.async_added_to_hass()
    registry = hass.data[DOMAIN][DATA_STREAMS]
    assert "SN1" in registry
    assert registry["SN1"].broadcast is camera._broadcast

    source = await camera.stream_source()
    assert source.startswith("http://127.0.0.1:8123/api/ezviz_stream/SN1?token=")
    assert camera._token in source
    assert len(camera._token) >= 32  # a real random token, not empty/predictable
    assert "hunter2" not in source  # account creds never touch the URL

    await camera.async_will_remove_from_hass()
    assert "SN1" not in registry
