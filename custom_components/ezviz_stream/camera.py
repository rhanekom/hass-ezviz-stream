"""Camera platform for EZVIZ Stream: one entity per camera subentry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.camera import Camera
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CAMERA_SUBENTRY_TYPE,
    CONF_SERIAL,
    CONF_VERIFICATION_CODE,
    MANUFACTURER,
    OFFICIAL_EZVIZ_DOMAIN,
)
from .stream import grab_jpeg

_LOGGER = logging.getLogger(__name__)

# A single-frame grab drives a brief live session; keep it short so HA's image
# fetch does not hang. Efficient live view arrives with go2rtc (Milestone C).
_SNAPSHOT_TIMEOUT = 30.0
_MAIN_STREAM = 1

if TYPE_CHECKING:
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

    def __init__(self, entry: EzvizStreamConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialise the camera from its account entry and camera subentry."""
        super().__init__()
        self._entry = entry
        self._serial: str = subentry.data[CONF_SERIAL]
        self._verification_code: str = subentry.data.get(CONF_VERIFICATION_CODE, "")
        self._attr_unique_id = self._serial
        # Reuse the official `ezviz` device identifier so we land on the same device
        # card when that integration is installed, and stand alone otherwise (§6.3).
        self._attr_device_info = DeviceInfo(
            identifiers={(OFFICIAL_EZVIZ_DOMAIN, self._serial)},
            name=subentry.title,
            manufacturer=MANUFACTURER,
            serial_number=self._serial,
        )

    async def async_camera_image(
        self,
        width: int | None = None,  # noqa: ARG002 - HA image API; we return native res
        height: int | None = None,  # noqa: ARG002
    ) -> bytes | None:
        """
        Grab a single decoded frame via a brief cloud live session.

        This drives login -> handshake -> media -> decode; it is inherently slow
        (seconds). Efficient continuous live view arrives with go2rtc (Milestone C).
        """
        api = self._entry.runtime_data.api
        cameras = await api.async_get_cameras()
        camera = next(
            (cam for cam in cameras if cam.serial == self._serial),
            None,
        )
        if camera is None:
            _LOGGER.warning("Camera %s not found on the account", self._serial)
            return None
        return await grab_jpeg(
            camera,
            api.async_get_vtdu_token,
            get_ffmpeg_manager(self.hass).binary,
            stream=_MAIN_STREAM,
            verification_code=self._verification_code,
            duration=_SNAPSHOT_TIMEOUT,
        )
