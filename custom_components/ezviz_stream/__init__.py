"""The EZVIZ Stream integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CannotConnect, EzvizCloudApi, InvalidAuth, MfaRequired
from .const import CONF_REGION

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [Platform.CAMERA]

# EZVIZ rejects a burst of concurrent VTDU sessions (result 5405/5452), which a
# dashboard of camera cards would trigger. Serialise streaming account-wide.
MAX_CONCURRENT_STREAMS = 1


@dataclass
class EzvizStreamData:
    """Runtime data stored on the config entry."""

    api: EzvizCloudApi
    stream_semaphore: asyncio.Semaphore


type EzvizStreamConfigEntry = ConfigEntry[EzvizStreamData]


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

    entry.runtime_data = EzvizStreamData(
        api=api, stream_semaphore=asyncio.Semaphore(MAX_CONCURRENT_STREAMS)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Reload when the entry changes so cameras added as subentries after setup get
    # their entities created (adding a subentry updates the entry -> fires this).
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: EzvizStreamConfigEntry
) -> None:
    """Reload the account entry when it (or its subentries) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: EzvizStreamConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
