"""Camera platform for EZVIZ Stream: one entity per camera subentry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.camera import Camera
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CAMERA_SUBENTRY_TYPE,
    CONF_SERIAL,
    CONF_VERIFICATION_CODE,
    MANUFACTURER,
    OFFICIAL_EZVIZ_DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import EzvizStreamConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 — platform setup signature fixed by HA
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
        width: int | None = None,  # noqa: ARG002 — placeholder until the producer lands
        height: int | None = None,  # noqa: ARG002
    ) -> bytes | None:
        """Return a still image. Placeholder until the streaming producer (B.2/B.3)."""
        return None
