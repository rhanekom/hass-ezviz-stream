"""Tests for the EZVIZ Stream camera platform."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigSubentry, ConfigSubentryData
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_stream import (
    async_remove_config_entry_device,
    async_remove_entry,
)
from custom_components.ezviz_stream.api import EzvizCamera, MotionImage
from custom_components.ezviz_stream.camera import (
    EzvizStreamCamera,
    _snapshot_path_for,
    _write_snapshot,
    async_setup_entry,
    remove_snapshot_file,
)
from custom_components.ezviz_stream.const import (
    CAMERA_SUBENTRY_TYPE,
    CONF_IS_BATTERY,
    CONF_REGION,
    CONF_SERIAL,
    CONF_SLOW_THUMBNAILS,
    CONF_SNAPSHOT_INTERVAL,
    CONF_STATIC_ANCHOR,
    CONF_STREAM,
    CONF_THUMBNAIL_MODE,
    CONF_VERIFICATION_CODE,
    DOMAIN,
    OFFICIAL_EZVIZ_DOMAIN,
    THUMBNAIL_MOTION,
    THUMBNAIL_STATIC,
    THUMBNAIL_STATIC_MOTION,
)
from custom_components.ezviz_stream.stream_view import DATA_STREAMS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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

        # Persist a frame for each camera; deleting SN1 must remove only SN1's file.
        sn1_snap = _snapshot_path_for(hass, "SN1")
        sn2_snap = _snapshot_path_for(hass, "SN2")
        await hass.async_add_executor_job(_write_snapshot, sn1_snap, b"J" * 100)
        await hass.async_add_executor_job(_write_snapshot, sn2_snap, b"K" * 100)

        removed = await async_remove_config_entry_device(hass, entry, device)
        await hass.async_block_till_done()

    assert removed is True
    serials = {se.data[CONF_SERIAL] for se in entry.subentries.values()}
    assert serials == {"SN2"}  # only SN1 removed; the account entry survives
    assert not sn1_snap.exists()  # SN1's persisted frame deleted with its device
    assert sn2_snap.exists()  # SN2's frame untouched


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


async def test_static_serves_cached_frame_without_grabbing(
    hass: HomeAssistant,
) -> None:
    """Static mode never grabs on the request path - it serves the cached frame."""
    camera = _make_camera(
        hass, {CONF_SERIAL: "SN1", CONF_THUMBNAIL_MODE: THUMBNAIL_STATIC}
    )
    camera._image = b"CACHED" * 100
    camera._image_at = 0.0  # would look stale, but static ignores the request path
    with patch("custom_components.ezviz_stream.camera.grab_jpeg", AsyncMock()) as grab:
        image = await camera.async_camera_image()
    assert image == b"CACHED" * 100
    grab.assert_not_called()


async def test_static_refreshes_from_live_view(hass: HomeAssistant) -> None:
    """Opening a stream refreshes the static thumbnail from the live view (no grab)."""
    camera = _make_camera(
        hass, {CONF_SERIAL: "SN1", CONF_THUMBNAIL_MODE: THUMBNAIL_STATIC}
    )
    hass.http = SimpleNamespace(server_port=8123)

    async def _empty() -> AsyncIterator[bytes]:
        yield b""  # a live-view tap (contents irrelevant; the decoder is mocked)

    subscribe = Mock(return_value=_empty())
    camera._broadcast = SimpleNamespace(subscribe=subscribe, is_running=True)

    with (
        patch(
            "custom_components.ezviz_stream.camera.capture_jpeg_from_ts",
            AsyncMock(return_value=b"LIVE" * 100),
        ) as capture,
        patch(
            "custom_components.ezviz_stream.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="ffmpeg"),
        ),
        patch("custom_components.ezviz_stream.camera._STREAM_CAPTURE_DELAY", 0.0),
    ):
        await camera.stream_source()
        assert camera._capture_task is not None
        await camera._capture_task

    subscribe.assert_called_once_with(start_if_idle=False)  # tap a live session only
    capture.assert_awaited_once()
    assert camera._image == b"LIVE" * 100


async def test_static_capture_skipped_when_broadcast_idle(hass: HomeAssistant) -> None:
    """A stream_source() registration call must not spawn a decoder for an idle cam."""
    camera = _make_camera(
        hass, {CONF_SERIAL: "SN1", CONF_THUMBNAIL_MODE: THUMBNAIL_STATIC}
    )
    hass.http = SimpleNamespace(server_port=8123)
    # HA also calls stream_source() to register the source at startup, with nobody
    # watching: the broadcast is idle, so the capture must bail before any ffmpeg.
    subscribe = Mock()
    camera._broadcast = SimpleNamespace(subscribe=subscribe, is_running=False)

    with (
        patch(
            "custom_components.ezviz_stream.camera.capture_jpeg_from_ts",
            AsyncMock(),
        ) as capture,
        patch("custom_components.ezviz_stream.camera._STREAM_CAPTURE_DELAY", 0.0),
    ):
        await camera.stream_source()
        assert camera._capture_task is not None
        await camera._capture_task

    subscribe.assert_not_called()
    capture.assert_not_awaited()
    assert camera._image is None


def _static_motion_camera(
    hass: HomeAssistant, motion: MotionImage
) -> EzvizStreamCamera:
    """A static_motion camera (anchor 1000) with a pre-captured baseline."""
    api = AsyncMock()
    api.async_get_last_motion = AsyncMock(return_value=motion)
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(api=api, snapshot_semaphore=asyncio.Semaphore(1))
    )
    subentry = SimpleNamespace(
        data={
            CONF_SERIAL: "SN1",
            CONF_VERIFICATION_CODE: "",
            CONF_THUMBNAIL_MODE: THUMBNAIL_STATIC_MOTION,
            CONF_STATIC_ANCHOR: 1000.0,
        },
        title="Cam",
        subentry_id="x",
    )
    camera = EzvizStreamCamera(entry, subentry)
    camera.hass = hass
    camera._static_image = b"STATIC" * 100  # baseline already captured
    return camera


async def test_static_then_motion_shows_newer_alarm(hass: HomeAssistant) -> None:
    """A motion event newer than the anchor replaces the static baseline (no wake)."""
    camera = _static_motion_camera(hass, MotionImage(b"ALARM" * 100, 2000.0))
    with patch("custom_components.ezviz_stream.camera.grab_jpeg", AsyncMock()) as grab:
        image = await camera.async_camera_image()
    assert image == b"ALARM" * 100
    grab.assert_not_called()  # newer alarm shown from the cloud, camera not woken


async def test_static_then_motion_keeps_static_when_alarm_older(
    hass: HomeAssistant,
) -> None:
    """An event at/older than the anchor is suppressed; the static baseline shows."""
    camera = _static_motion_camera(hass, MotionImage(b"OLD" * 100, 500.0))
    with patch("custom_components.ezviz_stream.camera.grab_jpeg", AsyncMock()):
        image = await camera.async_camera_image()
    assert image == b"STATIC" * 100


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

    # An unload (e.g. a restart/reload) must NOT delete the frame - it is the fallback.
    await cam2.async_will_remove_from_hass()
    assert cam2._snapshot_path.exists()


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


# --- setup, source, background + edge paths --------------------------------- #
def _make_camera(
    hass: HomeAssistant, data: dict[str, object], *, api: AsyncMock | None = None
) -> EzvizStreamCamera:
    """Build a standalone camera entity bound to a fake account entry."""
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            api=api or AsyncMock(), snapshot_semaphore=asyncio.Semaphore(1)
        )
    )
    camera = EzvizStreamCamera(
        entry, SimpleNamespace(data=data, title="Cam", subentry_id="x")
    )
    camera.hass = hass
    return camera


async def test_setup_skips_non_camera_subentries(hass: HomeAssistant) -> None:
    """Subentries that are not cameras are ignored during platform setup."""
    entry = SimpleNamespace(
        subentries={"a": SimpleNamespace(subentry_type="not_a_camera", subentry_id="a")}
    )
    add = Mock()
    await async_setup_entry(hass, entry, add)
    add.assert_not_called()


async def test_make_source_builds_mpegts_source(hass: HomeAssistant) -> None:
    """The source factory wires the camera's details into mpegts_source."""
    api = AsyncMock()
    camera = _make_camera(
        hass,
        {CONF_SERIAL: "SN1", CONF_VERIFICATION_CODE: "CODE", CONF_STREAM: 2},
        api=api,
    )
    with (
        patch(
            "custom_components.ezviz_stream.camera.mpegts_source", return_value="SRC"
        ) as ms,
        patch(
            "custom_components.ezviz_stream.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="ffmpeg"),
        ),
    ):
        result = camera._make_source()
        await hass.async_block_till_done()

    assert result == "SRC"
    assert ms.call_args.args[0] is api
    assert ms.call_args.args[1] == "SN1"
    assert ms.call_args.kwargs["stream"] == 2
    assert ms.call_args.kwargs["verification_code"] == "CODE"


async def test_resolve_battery_swallows_error(hass: HomeAssistant) -> None:
    """A failed battery lookup is logged and leaves the flag unknown (never raises)."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(side_effect=RuntimeError("boom"))
    camera = _make_camera(hass, {CONF_SERIAL: "SN1"}, api=api)
    await camera._async_resolve_battery()  # must not raise
    assert camera.extra_state_attributes == {"battery_camera": None}


async def test_resolve_battery_writes_state_once_added(hass: HomeAssistant) -> None:
    """Once the entity is added, resolving battery status writes the new state."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(
        return_value=[EzvizCamera("SN1", "Cam", "BatteryCamera", 1, 1, streamable=True)]
    )
    camera = _make_camera(hass, {CONF_SERIAL: "SN1"}, api=api)
    camera.entity_id = "camera.test"
    with patch.object(camera, "async_write_ha_state") as write:
        await camera._async_resolve_battery()
    assert camera._is_battery is True
    write.assert_called_once()


async def test_schedule_refresh_skipped_when_locked(hass: HomeAssistant) -> None:
    """No background refresh is scheduled while a grab already holds the lock."""
    camera = _make_camera(hass, {CONF_SERIAL: "SN1"})
    await camera._image_lock.acquire()
    try:
        with patch.object(hass, "async_create_background_task") as create:
            camera._schedule_snapshot_refresh()
        create.assert_not_called()
    finally:
        camera._image_lock.release()


async def test_background_refresh_bails_when_fresh(hass: HomeAssistant) -> None:
    """A background refresh that finds the cache already fresh does no grab."""
    camera = _make_camera(hass, {CONF_SERIAL: "SN1", CONF_SNAPSHOT_INTERVAL: 100})
    camera._image = b"FRESH" * 100
    camera._image_at = time.monotonic()  # within TTL
    with patch("custom_components.ezviz_stream.camera.grab_jpeg", AsyncMock()) as grab:
        await camera._async_background_refresh()
    grab.assert_not_called()


async def test_background_refresh_swallows_grab_error(hass: HomeAssistant) -> None:
    """A grab failure during background refresh is swallowed; the old frame stays."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(
        return_value=[EzvizCamera("SN1", "Cam", "IPC", 1, 1, streamable=True)]
    )
    camera = _make_camera(
        hass, {CONF_SERIAL: "SN1", CONF_SNAPSHOT_INTERVAL: 100}, api=api
    )
    camera._image = b"OLD" * 100
    camera._image_at = 0.0  # stale
    with (
        patch(
            "custom_components.ezviz_stream.camera.grab_jpeg",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch(
            "custom_components.ezviz_stream.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="ffmpeg"),
        ),
    ):
        await camera._async_background_refresh()  # must not raise
    assert camera._image == b"OLD" * 100  # unchanged after the failed refresh


async def test_static_then_motion_grabs_baseline_when_missing(
    hass: HomeAssistant,
) -> None:
    """First static_motion refresh with no baseline grabs one live and persists it."""
    api = AsyncMock()
    api.async_get_last_motion = AsyncMock(return_value=None)  # no motion event
    api.async_get_cameras = AsyncMock(
        return_value=[EzvizCamera("SN1", "Cam", "IPC", 1, 1, streamable=True)]
    )
    camera = _make_camera(
        hass,
        {
            CONF_SERIAL: "SN1",
            CONF_VERIFICATION_CODE: "",
            CONF_THUMBNAIL_MODE: THUMBNAIL_STATIC_MOTION,
            CONF_STATIC_ANCHOR: 1000.0,
        },
        api=api,
    )
    with (
        patch(
            "custom_components.ezviz_stream.camera.grab_jpeg",
            AsyncMock(return_value=b"BASE" * 100),
        ) as grab,
        patch(
            "custom_components.ezviz_stream.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="ffmpeg"),
        ),
    ):
        image = await camera.async_camera_image()

    assert image == b"BASE" * 100
    grab.assert_awaited_once()
    assert camera._static_image == b"BASE" * 100
    assert camera._snapshot_path.exists()  # the baseline is persisted to disk
    await hass.async_add_executor_job(camera._snapshot_path.unlink)


async def test_store_frame_ignores_none(hass: HomeAssistant) -> None:
    """A None frame (a failed grab) leaves the cache untouched."""
    camera = _make_camera(hass, {CONF_SERIAL: "SN1"})
    await camera._store_frame(None, persist=True)
    assert camera._image is None


async def test_grab_live_returns_none_when_camera_missing(
    hass: HomeAssistant,
) -> None:
    """A live grab for a serial no longer on the account returns None (no session)."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(return_value=[])  # SN1 not present
    camera = _make_camera(hass, {CONF_SERIAL: "SN1"}, api=api)
    with (
        patch("custom_components.ezviz_stream.camera.grab_jpeg", AsyncMock()) as grab,
        patch(
            "custom_components.ezviz_stream.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="ffmpeg"),
        ),
    ):
        result = await camera._async_grab_live()
    assert result is None
    grab.assert_not_called()


async def test_grab_live_taps_live_view_instead_of_new_session(
    hass: HomeAssistant,
) -> None:
    """With a viewer's session up, a live grab taps it - no rival cloud session.

    Opening a second session would preempt the live one and reset the camera's
    day/night exposure (the grayscale->colour flip), so the interval refresh must
    reuse the running broadcast, not call grab_jpeg or even look the camera up.
    """
    api = AsyncMock()
    camera = _make_camera(hass, {CONF_SERIAL: "SN1"}, api=api)

    async def _empty() -> AsyncIterator[bytes]:
        yield b""  # a live-view tap (contents irrelevant; the decoder is mocked)

    subscribe = Mock(return_value=_empty())
    camera._broadcast = SimpleNamespace(subscribe=subscribe, is_running=True)

    with (
        patch(
            "custom_components.ezviz_stream.camera.capture_jpeg_from_ts",
            AsyncMock(return_value=b"LIVE" * 100),
        ) as capture,
        patch(
            "custom_components.ezviz_stream.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="ffmpeg"),
        ),
        patch("custom_components.ezviz_stream.camera.grab_jpeg", AsyncMock()) as grab,
    ):
        result = await camera._async_grab_live()

    assert result == b"LIVE" * 100
    subscribe.assert_called_once_with(start_if_idle=False)  # tapped, not opened
    capture.assert_awaited_once()
    grab.assert_not_called()  # no independent (rival) cloud session
    api.async_get_cameras.assert_not_called()  # no control-plane lookup either


async def test_grab_live_opens_session_when_no_viewer(hass: HomeAssistant) -> None:
    """With nothing watching, a live grab opens an independent session via grab_jpeg."""
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(
        return_value=[EzvizCamera("SN1", "Cam", "IPC", 1, 1, streamable=True)]
    )
    camera = _make_camera(hass, {CONF_SERIAL: "SN1"}, api=api)  # real broadcast: idle

    with (
        patch(
            "custom_components.ezviz_stream.camera.grab_jpeg",
            AsyncMock(return_value=b"GRAB" * 100),
        ) as grab,
        patch(
            "custom_components.ezviz_stream.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="ffmpeg"),
        ),
        patch(
            "custom_components.ezviz_stream.camera.capture_jpeg_from_ts", AsyncMock()
        ) as capture,
    ):
        result = await camera._async_grab_live()

    assert result == b"GRAB" * 100
    grab.assert_awaited_once()
    capture.assert_not_awaited()  # idle -> no live tap


async def test_static_motion_restores_baseline_on_add(hass: HomeAssistant) -> None:
    """A restart restores the persisted frame as the static_motion baseline."""
    data = {
        CONF_SERIAL: "SN1",
        CONF_VERIFICATION_CODE: "",
        CONF_THUMBNAIL_MODE: THUMBNAIL_STATIC_MOTION,
        CONF_IS_BATTERY: False,  # skip the battery-resolve background task
    }
    camera = _make_camera(hass, data)
    await hass.async_add_executor_job(
        _write_snapshot, camera._snapshot_path, b"BASE" * 100
    )
    await camera.async_added_to_hass()
    assert camera._static_image == b"BASE" * 100  # restored baseline
    assert camera._image == b"BASE" * 100  # also seeded as the failure fallback
    await camera.async_will_remove_from_hass()


async def test_snapshot_survives_unload_and_removed_on_delete(
    hass: HomeAssistant,
) -> None:
    """A persisted frame survives an unload; it is deleted only on real removal."""
    camera = _make_camera(hass, {CONF_SERIAL: "SN1", CONF_IS_BATTERY: False})
    await hass.async_add_executor_job(
        _write_snapshot, camera._snapshot_path, b"J" * 6000
    )
    await camera.async_added_to_hass()

    await camera.async_will_remove_from_hass()  # an unload, e.g. a restart/reload
    assert camera._snapshot_path.exists()  # frame kept for the restart fallback

    await hass.async_add_executor_job(remove_snapshot_file, hass, "SN1")
    assert not camera._snapshot_path.exists()  # gone only on a real removal


async def test_account_removal_deletes_all_snapshots(hass: HomeAssistant) -> None:
    """Removing the whole account entry deletes every camera's persisted frame."""
    entry = _entry_with_two_cameras()
    entry.add_to_hass(hass)
    sn1 = _snapshot_path_for(hass, "SN1")
    sn2 = _snapshot_path_for(hass, "SN2")
    await hass.async_add_executor_job(_write_snapshot, sn1, b"J" * 100)
    await hass.async_add_executor_job(_write_snapshot, sn2, b"K" * 100)

    await async_remove_entry(hass, entry)

    assert not sn1.exists()
    assert not sn2.exists()


# --- availability + offline fast-fail ---------------------------------------- #
def _cam(status: int, *, category: str = "IPC") -> EzvizCamera:
    """Build an EzvizCamera with a given online status/category for lookups."""
    return EzvizCamera("SN1", "Cam", category, 1, status, streamable=True)


@pytest.mark.parametrize(
    ("is_battery", "is_online", "expected"),
    [
        (False, False, False),  # a mains camera that is genuinely offline
        (False, True, True),  # a mains camera that is online
        (False, None, True),  # online state not yet known - stay available
        (True, False, True),  # a battery camera "offline" is just asleep
    ],
)
def test_available_reflects_online_state(
    hass: HomeAssistant,
    is_battery: bool,
    is_online: bool | None,
    expected: bool,
) -> None:
    """Only a known-offline mains camera is unavailable; battery cams stay available."""
    camera = _make_camera(hass, {CONF_SERIAL: "SN1"})
    camera._is_battery = is_battery
    camera._is_online = is_online
    assert camera.available is expected


async def test_stream_source_refuses_offline_mains_camera(hass: HomeAssistant) -> None:
    """An offline mains camera fast-fails so HA never starts a retrying worker."""
    api = AsyncMock(async_get_cameras=AsyncMock(return_value=[_cam(2)]))  # status 2
    camera = _make_camera(hass, {CONF_SERIAL: "SN1", CONF_IS_BATTERY: False}, api=api)
    hass.http = SimpleNamespace(server_port=8123)

    with pytest.raises(HomeAssistantError):
        await camera.stream_source()
    assert camera._is_online is False  # the lookup recorded the offline state


async def test_stream_source_serves_online_mains_camera(hass: HomeAssistant) -> None:
    """An online mains camera returns its media URL as normal."""
    api = AsyncMock(async_get_cameras=AsyncMock(return_value=[_cam(1)]))  # status 1
    camera = _make_camera(hass, {CONF_SERIAL: "SN1", CONF_IS_BATTERY: False}, api=api)
    hass.http = SimpleNamespace(server_port=8123)

    url = await camera.stream_source()
    assert "/api/ezviz_stream/SN1?token=" in url
    assert camera._is_online is True


async def test_stream_source_never_refuses_battery_camera(hass: HomeAssistant) -> None:
    """A battery camera is streamed even when 'offline' - streaming wakes it."""
    api = AsyncMock(async_get_cameras=AsyncMock())
    camera = _make_camera(hass, {CONF_SERIAL: "SN1", CONF_IS_BATTERY: True}, api=api)
    hass.http = SimpleNamespace(server_port=8123)

    url = await camera.stream_source()
    assert "/api/ezviz_stream/SN1?token=" in url
    api.async_get_cameras.assert_not_called()  # no lookup: never gate a battery wake


async def test_removal_stops_orphaned_ha_stream(hass: HomeAssistant) -> None:
    """Removing a camera tears down its HA Stream so it stops retrying the dead URL."""
    camera = _make_camera(hass, {CONF_SERIAL: "SN1"})
    camera._broadcast = AsyncMock()
    stream = SimpleNamespace(stop=AsyncMock())
    camera.stream = stream

    with patch("custom_components.ezviz_stream.camera.unregister_stream") as unreg:
        await camera.async_will_remove_from_hass()

    unreg.assert_called_once()
    stream.stop.assert_awaited_once()
    assert camera.stream is None
