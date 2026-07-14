"""The EZVIZ Stream integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CannotConnect, EzvizCloudApi, InvalidAuth, MfaRequired
from .const import (
    CAMERA_SUBENTRY_TYPE,
    CONF_MAX_SNAPSHOTS,
    CONF_REGION,
    CONF_SERIAL,
    DEFAULT_MAX_SNAPSHOTS,
    DOMAIN,
    OFFICIAL_EZVIZ_DOMAIN,
)
from .stream_view import EzvizStreamMediaView

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

PLATFORMS: list[Platform] = [Platform.CAMERA]

_VIEW_REGISTERED = "view_registered"


@dataclass
class EzvizStreamData:
    """Runtime data stored on the config entry."""

    api: EzvizCloudApi
    snapshot_semaphore: asyncio.Semaphore
    # Snapshot of each camera subentry's data at the last (re)load. Lets the update
    # listener tell an added/reconfigured camera (which needs a reload to (re)create
    # its entity) from a pure removal (which does not - reloading would needlessly
    # re-login and, if EZVIZ throttles it, briefly drop every other camera).
    camera_subentries: dict[str, dict[str, Any]] = field(default_factory=dict)


type EzvizStreamConfigEntry = ConfigEntry[EzvizStreamData]


def _camera_subentries(entry: EzvizStreamConfigEntry) -> dict[str, dict[str, Any]]:
    """Return a copy of each camera subentry's data, keyed by subentry id."""
    return {
        subentry_id: dict(subentry.data)
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == CAMERA_SUBENTRY_TYPE
    }


async def async_setup_entry(hass: HomeAssistant, entry: EzvizStreamConfigEntry) -> bool:
    """Set up EZVIZ Stream from a config entry."""
    api = EzvizCloudApi(async_get_clientsession(hass))
    try:
        await api.async_login(
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            entry.data[CONF_REGION],
        )
    except (InvalidAuth, MfaRequired) as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except CannotConnect as err:
        raise ConfigEntryNotReady(str(err)) from err

    max_snapshots = int(entry.options.get(CONF_MAX_SNAPSHOTS, DEFAULT_MAX_SNAPSHOTS))
    entry.runtime_data = EzvizStreamData(
        api=api,
        snapshot_semaphore=asyncio.Semaphore(max_snapshots),
        camera_subentries=_camera_subentries(entry),
    )
    # Register the media view once per HA instance (serves every camera's stream).
    domain_data = hass.data.setdefault(DOMAIN, {})
    if "http" in hass.config.components and not domain_data.get(_VIEW_REGISTERED):
        hass.http.register_view(EzvizStreamMediaView(hass))
        domain_data[_VIEW_REGISTERED] = True
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Reload when a camera is added or reconfigured so its entity is (re)created.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: EzvizStreamConfigEntry
) -> None:
    """
    Reload only when a camera was added or reconfigured, not on a pure removal.

    A reload re-logs-in to the EZVIZ cloud; doing that on every subentry change means
    deleting one camera would re-login and, if the cloud throttles it, drop every
    other camera. Home Assistant already removes a deleted subentry's entity/device,
    so a removal needs no reload here.
    """
    data = entry.runtime_data
    if data is None:  # not fully loaded yet; nothing to reconcile
        return
    current = _camera_subentries(entry)
    known = data.camera_subentries
    added_or_changed = any(current[sid] != known.get(sid) for sid in current)
    data.camera_subentries = current  # refresh the snapshot either way
    if added_or_changed:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: EzvizStreamConfigEntry,
    device_entry: DeviceEntry,
) -> bool:
    """
    Allow deleting a camera from its device page by removing its subentry.

    Without this, Home Assistant offers no per-camera delete (``supports_remove_device``
    stays false), leaving only the drastic "delete the whole account" action. We match
    the device to its camera subentry by serial and remove just that subentry.
    """
    serials = {
        identifier[1]
        for identifier in device_entry.identifiers
        if identifier[0] == OFFICIAL_EZVIZ_DOMAIN
    }
    for subentry_id, subentry in list(config_entry.subentries.items()):
        if (
            subentry.subentry_type == CAMERA_SUBENTRY_TYPE
            and subentry.data.get(CONF_SERIAL) in serials
        ):
            hass.config_entries.async_remove_subentry(config_entry, subentry_id)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: EzvizStreamConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
