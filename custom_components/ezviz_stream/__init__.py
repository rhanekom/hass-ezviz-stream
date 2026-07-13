"""The EZVIZ Stream integration."""

from __future__ import annotations

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

# No entity platforms yet — the camera platform lands with the streaming module.
PLATFORMS: list[Platform] = []


@dataclass
class EzvizStreamData:
    """Runtime data stored on the config entry."""

    api: EzvizCloudApi


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

    entry.runtime_data = EzvizStreamData(api=api)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: EzvizStreamConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
