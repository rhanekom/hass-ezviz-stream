"""Camera platform for EZVIZ Stream: one entity per camera subentry."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import TYPE_CHECKING

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.helpers.device_registry import DeviceInfo

from .broadcast import CameraBroadcast, mpegts_source
from .const import (
    CAMERA_SUBENTRY_TYPE,
    CONF_SERIAL,
    CONF_VERIFICATION_CODE,
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
_MAIN_STREAM = 1
# Serve a cached frame for this long so HA's image polling does not re-stream on
# every poll (each grab is a full cloud session).
_SNAPSHOT_CACHE_TTL = 30.0

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import EzvizStreamConfigEntry


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
        self._stream_index: int = _MAIN_STREAM
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

    async def async_added_to_hass(self) -> None:
        """Register this camera's broadcaster so the media view can serve it."""
        register_stream(self.hass, self._serial, self._token, self._broadcast)

    async def async_will_remove_from_hass(self) -> None:
        """Deregister and stop this camera's broadcaster."""
        unregister_stream(self.hass, self._serial)
        await self._broadcast.async_stop()

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
        Return a recent frame, grabbing one via a brief cloud session if stale.

        Grabbing drives login -> handshake -> media -> decode (seconds), so results
        are cached for ``_SNAPSHOT_CACHE_TTL`` and grabs are serialised account-wide
        (a dashboard of cameras must not open concurrent VTDU sessions). On failure
        the last known frame is returned. Continuous live view arrives with go2rtc.
        """
        if (
            self._image is not None
            and time.monotonic() - self._image_at < _SNAPSHOT_CACHE_TTL
        ):
            return self._image

        async with self._image_lock:
            # Another waiter may have just refreshed while we waited for the lock.
            if (
                self._image is not None
                and time.monotonic() - self._image_at < _SNAPSHOT_CACHE_TTL
            ):
                return self._image

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
                return self._image

            async with self._entry.runtime_data.stream_semaphore:
                jpeg = await grab_jpeg(
                    camera,
                    api.async_get_vtdu_token,
                    get_ffmpeg_manager(self.hass).binary,
                    stream=_MAIN_STREAM,
                    verification_code=self._verification_code,
                    duration=_SNAPSHOT_TIMEOUT,
                    max_sessions=_SNAPSHOT_MAX_SESSIONS,
                )
            if jpeg is not None:
                self._image = jpeg
                self._image_at = time.monotonic()
            return self._image
