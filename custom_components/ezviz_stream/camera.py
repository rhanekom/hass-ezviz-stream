"""Camera platform for EZVIZ Stream: one entity per camera subentry."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.helpers.device_registry import DeviceInfo

from .broadcast import CameraBroadcast, mpegts_source
from .const import (
    CAMERA_SUBENTRY_TYPE,
    CONF_SERIAL,
    CONF_SLOW_THUMBNAILS,
    CONF_STREAM,
    CONF_VERIFICATION_CODE,
    DEFAULT_STREAM,
    MANUFACTURER,
    OFFICIAL_EZVIZ_DOMAIN,
)
from .stream import grab_jpeg
from .stream_view import register_stream, unregister_stream

_LOGGER = logging.getLogger(__name__)

# A single-frame grab drives a brief live session; keep it short so HA's image
# fetch does not hang. Efficient live view arrives with go2rtc (Milestone C).
_SNAPSHOT_TIMEOUT = 30.0
_SNAPSHOT_MAX_SESSIONS = 3  # limit reconnect churn per image request
# Serve a cached frame for this long so HA's image polling does not re-stream on
# every poll (each grab is a full cloud session). Battery cams (and any camera the
# user flags) use the slower cadence - they wake slowly and streaming drains them.
_SNAPSHOT_CACHE_TTL = 30.0
_SNAPSHOT_CACHE_TTL_SLOW = 300.0
# The last good frame is persisted here so a cold start (the in-memory cache is empty
# after a restart) falls back to it instead of a blank tile until a fresh grab
# succeeds. It is camera imagery at rest: kept in the HA config dir, owner-only
# (0600), one small JPEG per camera; removed when the camera is removed.
_SNAPSHOT_DIR = "ezviz_stream"
_SNAPSHOT_FILE_MODE = 0o600

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import EzvizStreamConfigEntry


def _write_snapshot(path: Path, data: bytes) -> None:
    """Persist the latest frame, owner-only (executor job)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _SNAPSHOT_FILE_MODE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _read_snapshot(path: Path) -> bytes | None:
    """Return the persisted frame, or None if there is none (executor job)."""
    try:
        return path.read_bytes()
    except OSError:
        return None


def _remove_snapshot(path: Path) -> None:
    """Delete the persisted frame if present (executor job)."""
    path.unlink(missing_ok=True)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 - platform setup signature fixed by HA
    entry: EzvizStreamConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a camera entity for each camera subentry of the account."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type != CAMERA_SUBENTRY_TYPE:
            continue
        async_add_entities(
            [EzvizStreamCamera(entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class EzvizStreamCamera(Camera):
    """A cloud-streamed EZVIZ camera (one per subentry)."""

    _attr_has_entity_name = True
    _attr_name = None  # the camera is its own device; use the device name
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, entry: EzvizStreamConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialise the camera from its account entry and camera subentry."""
        super().__init__()
        self._entry = entry
        self._serial: str = subentry.data[CONF_SERIAL]
        self._verification_code: str = subentry.data.get(CONF_VERIFICATION_CODE, "")
        self._slow_thumbnails: bool = subentry.data.get(CONF_SLOW_THUMBNAILS, False)
        self._stream_index: int = subentry.data.get(CONF_STREAM, DEFAULT_STREAM)
        self._attr_unique_id = self._serial
        self._image: bytes | None = None  # last decoded frame (snapshot cache)
        self._image_at = 0.0
        self._image_lock = asyncio.Lock()  # dedupe concurrent grabs for this camera
        # Live serving: a random per-camera token guards the HTTP media endpoint, and
        # one on-demand broadcaster fans a single cloud session out to all consumers.
        self._token = secrets.token_urlsafe(32)
        self._broadcast = CameraBroadcast(self._make_source)
        # Reuse the official `ezviz` device identifier so we land on the same device
        # card when that integration is installed, and stand alone otherwise (§6.3).
        self._attr_device_info = DeviceInfo(
            identifiers={(OFFICIAL_EZVIZ_DOMAIN, self._serial)},
            name=subentry.title,
            manufacturer=MANUFACTURER,
            serial_number=self._serial,
        )

    def _make_source(self) -> AsyncIterator[bytes]:
        """Build the upstream MPEG-TS source (called when the broadcaster starts)."""
        api = self._entry.runtime_data.api
        return mpegts_source(
            api,
            self._serial,
            get_ffmpeg_manager(self.hass).binary,
            stream=self._stream_index,
            verification_code=self._verification_code,
        )

    @property
    def _snapshot_path(self) -> Path:
        """Path to this camera's persisted last-good frame."""
        return Path(self.hass.config.path(_SNAPSHOT_DIR)) / f"{self._serial}.jpg"

    @property
    def _cache_ttl(self) -> float:
        """How long a grabbed frame stays fresh (longer for slow/battery cams)."""
        return (
            _SNAPSHOT_CACHE_TTL_SLOW if self._slow_thumbnails else _SNAPSHOT_CACHE_TTL
        )

    async def async_added_to_hass(self) -> None:
        """Register the broadcaster and restore the last frame as a failure fallback."""
        register_stream(self.hass, self._serial, self._token, self._broadcast)
        # Restore for failure-fallback only: leave _image_at at 0 so the frame is
        # treated as stale and the next request still attempts a fresh grab, falling
        # back to the restored frame only if that grab fails (no cold-start blank).
        restored = await self.hass.async_add_executor_job(
            _read_snapshot, self._snapshot_path
        )
        if restored is not None and self._image is None:
            self._image = restored

    async def async_will_remove_from_hass(self) -> None:
        """Deregister, stop the broadcaster, and delete the persisted frame."""
        unregister_stream(self.hass, self._serial)
        await self._broadcast.async_stop()
        await self.hass.async_add_executor_job(_remove_snapshot, self._snapshot_path)

    async def stream_source(self) -> str:
        """
        Return the local HTTP MPEG-TS URL that go2rtc and the stream component read.

        go2rtc rejects ``exec:`` sources via its API, and HA's ``stream`` component can
        only ffmpeg-open a URL, so we serve MPEG-TS from our own token-guarded view
        (:mod:`stream_view`). The broadcaster behind it fans one cloud session out to
        all consumers (WebRTC + HLS + snapshots), so only one stream runs per camera.
        """
        port = self.hass.http.server_port
        return (
            f"http://127.0.0.1:{port}/api/ezviz_stream/{self._serial}"
            f"?token={self._token}"
        )

    async def async_camera_image(
        self,
        width: int | None = None,  # noqa: ARG002 - HA image API; we return native res
        height: int | None = None,  # noqa: ARG002
    ) -> bytes | None:
        """
        Return the cached frame immediately, refreshing in the background if stale.

        Stale-while-revalidate: a grab drives login -> handshake -> media -> decode and
        can take up to ``_SNAPSHOT_TIMEOUT`` (~30 s), but HA aborts an image request
        after ``CAMERA_IMAGE_TIMEOUT`` (10 s). Blocking a request on a grab therefore
        blanks the tile even though we hold a good (just-stale) frame. So we always
        return the cached frame at once - fresh or stale, including one restored from
        disk across a restart - and only kick a background refresh when it is stale.
        The very first grab (no frame yet) is the only one we block on.
        """
        if self._image is not None:
            if time.monotonic() - self._image_at >= self._cache_ttl:
                self._schedule_snapshot_refresh()
            return self._image

        # Cold: nothing cached yet, so block on the first grab.
        async with self._image_lock:
            if self._image is None:
                await self._async_grab_into_cache()
        return self._image

    def _schedule_snapshot_refresh(self) -> None:
        """Kick a background snapshot grab unless one is already running."""
        if self._image_lock.locked():
            return
        # eager_start=False so the grab runs entirely off the request path - the
        # caller gets the cached frame back before any of the refresh runs.
        self.hass.async_create_background_task(
            self._async_background_refresh(),
            f"ezviz_stream snapshot refresh {self._serial}",
            eager_start=False,
        )

    async def _async_background_refresh(self) -> None:
        """Refresh the snapshot cache off the request path (never raises)."""
        async with self._image_lock:
            if (
                self._image is not None
                and time.monotonic() - self._image_at < self._cache_ttl
            ):
                return  # another refresh beat us to it
            try:
                await self._async_grab_into_cache()
            except Exception:  # noqa: BLE001 - a background task must not leak
                _LOGGER.debug(
                    "Background snapshot refresh failed for %s",
                    self._serial,
                    exc_info=True,
                )

    async def _async_grab_into_cache(self) -> None:
        """Grab one frame and update the in-memory + on-disk cache (holds the lock)."""
        api = self._entry.runtime_data.api
        camera = next(
            (
                cam
                for cam in await api.async_get_cameras()
                if cam.serial == self._serial
            ),
            None,
        )
        if camera is None:
            _LOGGER.warning("Camera %s not found on the account", self._serial)
            return

        async with self._entry.runtime_data.snapshot_semaphore:
            jpeg = await grab_jpeg(
                camera,
                api.async_get_vtdu_token,
                get_ffmpeg_manager(self.hass).binary,
                stream=self._stream_index,
                verification_code=self._verification_code,
                duration=_SNAPSHOT_TIMEOUT,
                max_sessions=_SNAPSHOT_MAX_SESSIONS,
            )
        if jpeg is not None:
            self._image = jpeg
            self._image_at = time.monotonic()
            await self.hass.async_add_executor_job(
                _write_snapshot, self._snapshot_path, jpeg
            )
