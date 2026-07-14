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

from custom_components.ezviz_stream import async_remove_config_entry_device
from custom_components.ezviz_stream.api import EzvizCamera
from custom_components.ezviz_stream.camera import EzvizStreamCamera
from custom_components.ezviz_stream.const import (
    CAMERA_SUBENTRY_TYPE,
    CONF_IS_BATTERY,
    CONF_REGION,
    CONF_SERIAL,
    CONF_SLOW_THUMBNAILS,
    CONF_SNAPSHOT_INTERVAL,
    CONF_THUMBNAIL_MODE,
    CONF_VERIFICATION_CODE,
    DOMAIN,
    OFFICIAL_EZVIZ_DOMAIN,
    THUMBNAIL_MOTION,
    THUMBNAIL_STATIC,
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


def _entry_with_two_cameras() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data=_ACCOUNT,
        subentries_data=[
            ConfigSubentryData(
                data={CONF_SERIAL: serial, CONF_VERIFICATION_CODE: ""},
                subentry_type=CAMERA_SUBENTRY_TYPE,
                title=serial,
                unique_id=serial,
            )
            for serial in ("SN1", "SN2")
        ],
    )


async def test_removing_one_camera_keeps_the_others(hass: HomeAssistant) -> None:
    """Deleting one camera subentry leaves the others - and does not reload."""
    entry = _entry_with_two_cameras()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.ezviz_stream.EzvizCloudApi",
        return_value=AsyncMock(async_login=AsyncMock(return_value=None)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        assert registry.async_get_entity_id("camera", DOMAIN, "SN1") is not None
        assert registry.async_get_entity_id("camera", DOMAIN, "SN2") is not None

        sid1 = next(
            sid for sid, se in entry.subentries.items() if se.data[CONF_SERIAL] == "SN1"
        )
        # A pure removal must not trigger a reload (which would re-login the account).
        with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
            hass.config_entries.async_remove_subentry(entry, sid1)
            await hass.async_block_till_done()
        reload.assert_not_called()

    assert registry.async_get_entity_id("camera", DOMAIN, "SN1") is None  # removed
    assert registry.async_get_entity_id("camera", DOMAIN, "SN2") is not None  # kept


async def test_remove_config_entry_device_removes_only_that_camera(
    hass: HomeAssistant,
) -> None:
    """Deleting a camera's device removes just its subentry, not the account."""
    entry = _entry_with_two_cameras()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.ezviz_stream.EzvizCloudApi",
        return_value=AsyncMock(async_login=AsyncMock(return_value=None)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        device = dr.async_get(hass).async_get_device(
            identifiers={(OFFICIAL_EZVIZ_DOMAIN, "SN1")}
        )
        assert device is not None

        removed = await async_remove_config_entry_device(hass, entry, device)
        await hass.async_block_till_done()

    assert removed is True
    serials = {se.data[CONF_SERIAL] for se in entry.subentries.values()}
    assert serials == {"SN2"}  # only SN1 removed; the account entry survives


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
        runtime_data=SimpleNamespace(api=api, snapshot_semaphore=asyncio.Semaphore(1))
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


async def test_stale_snapshot_served_immediately_then_refreshed(
    hass: HomeAssistant,
) -> None:
    """A stale frame is served at once; the refresh runs in the background."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(
        return_value=[EzvizCamera("SN1", "Front door", "IPC", 1, 1, streamable=True)]
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(api=api, snapshot_semaphore=asyncio.Semaphore(1))
    )
    subentry = SimpleNamespace(
        data={CONF_SERIAL: "SN1", CONF_VERIFICATION_CODE: ""},
        title="Front door",
        subentry_id="x",
    )
    camera = EzvizStreamCamera(entry, subentry)
    camera.hass = hass
    camera._image = b"OLD" * 2000
    camera._image_at = 0.0  # far in the past -> stale

    with (
        patch(
            "custom_components.ezviz_stream.camera.grab_jpeg",
            AsyncMock(return_value=b"NEW" * 2000),
        ) as grab,
        patch(
            "custom_components.ezviz_stream.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="ffmpeg"),
        ),
    ):
        immediate = await camera.async_camera_image()
        assert immediate == b"OLD" * 2000  # stale frame returned without blocking
        await hass.async_block_till_done()  # let the background refresh run

    assert grab.await_count == 1
    assert camera._image == b"NEW" * 2000  # cache refreshed in the background


def test_snapshot_interval_sets_cache_ttl() -> None:
    """The configured snapshot interval is the camera's cache TTL."""
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            api=AsyncMock(), snapshot_semaphore=asyncio.Semaphore(1)
        )
    )
    camera = EzvizStreamCamera(
        entry,
        SimpleNamespace(
            data={CONF_SERIAL: "SN1", CONF_SNAPSHOT_INTERVAL: 120},
            title="Cam",
            subentry_id="x",
        ),
    )
    assert camera._cache_ttl == 120


def test_battery_attribute_from_stored_value() -> None:
    """The stored battery flag is exposed as a read-only camera attribute."""
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            api=AsyncMock(), snapshot_semaphore=asyncio.Semaphore(1)
        )
    )
    camera = EzvizStreamCamera(
        entry,
        SimpleNamespace(
            data={CONF_SERIAL: "SN1", CONF_IS_BATTERY: True},
            title="Cam",
            subentry_id="x",
        ),
    )
    assert camera.extra_state_attributes == {"battery_camera": True}


async def test_battery_attribute_resolved_when_absent(hass: HomeAssistant) -> None:
    """A camera added before the flag was recorded resolves it once from the cloud."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(
        return_value=[EzvizCamera("SN1", "Cam", "BatteryCamera", 1, 1, streamable=True)]
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(api=api, snapshot_semaphore=asyncio.Semaphore(1))
    )
    camera = EzvizStreamCamera(
        entry,
        SimpleNamespace(data={CONF_SERIAL: "SN1"}, title="Cam", subentry_id="x"),
    )
    camera.hass = hass

    assert camera.extra_state_attributes == {"battery_camera": None}  # unknown at first
    await camera._async_resolve_battery()
    assert camera.extra_state_attributes == {"battery_camera": True}


def test_legacy_slow_thumbnails_maps_to_interval() -> None:
    """Subentries predating the interval map the old boolean to a sensible TTL."""
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            api=AsyncMock(), snapshot_semaphore=asyncio.Semaphore(1)
        )
    )

    def _cam(*, slow: bool) -> EzvizStreamCamera:
        return EzvizStreamCamera(
            entry,
            SimpleNamespace(
                data={CONF_SERIAL: "SN1", CONF_SLOW_THUMBNAILS: slow},
                title="Cam",
                subentry_id="x",
            ),
        )

    assert _cam(slow=True)._cache_ttl > _cam(slow=False)._cache_ttl


async def test_motion_thumbnail_uses_alarm_image(hass: HomeAssistant) -> None:
    """With the motion-thumbnail option on, the tile comes from the alarm image."""
    api = AsyncMock()
    api.async_get_last_motion_image = AsyncMock(return_value=b"MOTION" * 100)
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(api=api, snapshot_semaphore=asyncio.Semaphore(1))
    )
    subentry = SimpleNamespace(
        data={
            CONF_SERIAL: "SN1",
            CONF_VERIFICATION_CODE: "ABCDEF",
            CONF_THUMBNAIL_MODE: THUMBNAIL_MOTION,
        },
        title="Cam",
        subentry_id="x",
    )
    camera = EzvizStreamCamera(entry, subentry)
    camera.hass = hass

    with patch("custom_components.ezviz_stream.camera.grab_jpeg", AsyncMock()) as grab:
        image = await camera.async_camera_image()

    assert image == b"MOTION" * 100
    api.async_get_last_motion_image.assert_awaited_once_with(
        "SN1", verification_code="ABCDEF"
    )
    grab.assert_not_called()  # the camera was never woken for a live grab


async def test_motion_thumbnail_seeds_with_live_grab(hass: HomeAssistant) -> None:
    """No stored motion image yet + nothing cached -> seed once with a live grab."""
    api = AsyncMock()
    api.async_get_last_motion_image = AsyncMock(return_value=None)
    api.async_get_cameras = AsyncMock(
        return_value=[EzvizCamera("SN1", "Cam", "BatteryCamera", 1, 1, streamable=True)]
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(api=api, snapshot_semaphore=asyncio.Semaphore(1))
    )
    subentry = SimpleNamespace(
        data={
            CONF_SERIAL: "SN1",
            CONF_VERIFICATION_CODE: "",
            CONF_THUMBNAIL_MODE: THUMBNAIL_MOTION,
        },
        title="Cam",
        subentry_id="x",
    )
    camera = EzvizStreamCamera(entry, subentry)
    camera.hass = hass

    with (
        patch(
            "custom_components.ezviz_stream.camera.grab_jpeg",
            AsyncMock(return_value=b"SEED" * 100),
        ) as grab,
        patch(
            "custom_components.ezviz_stream.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="ffmpeg"),
        ),
    ):
        image = await camera.async_camera_image()

    assert image == b"SEED" * 100
    grab.assert_awaited_once()  # seeded via one live grab when no motion image


async def test_static_thumbnail_grabbed_once_then_frozen(
    hass: HomeAssistant,
) -> None:
    """Static mode grabs one live frame, then never refreshes (infinite TTL)."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(
        return_value=[EzvizCamera("SN1", "Cam", "IPC", 1, 1, streamable=True)]
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(api=api, snapshot_semaphore=asyncio.Semaphore(1))
    )
    subentry = SimpleNamespace(
        data={
            CONF_SERIAL: "SN1",
            CONF_VERIFICATION_CODE: "",
            CONF_THUMBNAIL_MODE: THUMBNAIL_STATIC,
        },
        title="Cam",
        subentry_id="x",
    )
    camera = EzvizStreamCamera(entry, subentry)
    camera.hass = hass
    assert camera._cache_ttl == float("inf")  # never goes stale

    with (
        patch(
            "custom_components.ezviz_stream.camera.grab_jpeg",
            AsyncMock(return_value=b"STATIC" * 100),
        ) as grab,
        patch(
            "custom_components.ezviz_stream.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="ffmpeg"),
        ),
    ):
        first = await camera.async_camera_image()
        await hass.async_block_till_done()
        second = await camera.async_camera_image()

    assert first == b"STATIC" * 100
    assert second == first
    grab.assert_awaited_once()  # captured once, never refreshed


async def test_snapshot_persisted_and_restored_across_restart(
    hass: HomeAssistant,
) -> None:
    """A grabbed frame is written to disk and a fresh entity restores it as fallback."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(
        return_value=[EzvizCamera("SN1", "Front door", "IPC", 1, 1, streamable=True)]
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(api=api, snapshot_semaphore=asyncio.Semaphore(1))
    )
    subentry = SimpleNamespace(
        data={CONF_SERIAL: "SN1", CONF_VERIFICATION_CODE: ""},
        title="Front door",
        subentry_id="x",
    )
    cam1 = EzvizStreamCamera(entry, subentry)
    cam1.hass = hass

    with (
        patch(
            "custom_components.ezviz_stream.camera.grab_jpeg",
            AsyncMock(return_value=b"J" * 6000),
        ),
        patch(
            "custom_components.ezviz_stream.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="ffmpeg"),
        ),
    ):
        assert await cam1.async_camera_image() == b"J" * 6000
    assert cam1._snapshot_path.exists()  # persisted on a successful grab

    # A fresh entity (same serial, e.g. after a restart) restores the frame on add.
    cam2 = EzvizStreamCamera(entry, subentry)
    cam2.hass = hass
    await cam2.async_added_to_hass()
    assert cam2._image == b"J" * 6000
    assert not cam2._image_at  # stale (0.0), so a fresh grab is still attempted first

    # Removing the camera deletes the persisted frame.
    await cam2.async_will_remove_from_hass()
    assert not cam2._snapshot_path.exists()


async def test_stream_source_and_registry_lifecycle(hass: HomeAssistant) -> None:
    """stream_source is a token-guarded local HTTP URL; add/remove (de)registers it."""
    entry = SimpleNamespace(
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "hunter2",
            CONF_REGION: "Europe",
        },
        runtime_data=SimpleNamespace(
            api=AsyncMock(), snapshot_semaphore=asyncio.Semaphore(1)
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
