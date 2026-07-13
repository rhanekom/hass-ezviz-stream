"""
Config flow for EZVIZ Stream.

The account is the config entry (a hub); each camera is a config subentry carrying
its own serial and optional Image-Encryption verification code, added from the
account's "Add camera" action.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    CannotConnect,
    EzvizCamera,
    EzvizCloudApi,
    InvalidAuth,
    InvalidRegion,
    MfaRequired,
)
from .const import (
    CAMERA_SUBENTRY_TYPE,
    CONF_REGION,
    CONF_SERIAL,
    CONF_VERIFICATION_CODE,
    DEFAULT_REGION,
    DOMAIN,
    REGION_API_CODES,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from . import EzvizStreamConfigEntry

_LOGGER = logging.getLogger(__name__)

_ACCOUNT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_REGION, default=DEFAULT_REGION): SelectSelector(
            SelectSelectorConfig(
                options=sorted(REGION_API_CODES),
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
    }
)


class EzvizStreamConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the EZVIZ Stream account config flow."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Authenticate the EZVIZ account and create the account entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api = EzvizCloudApi(async_get_clientsession(self.hass))
            try:
                await api.async_login(
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    user_input[CONF_REGION],
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except MfaRequired:
                errors["base"] = "mfa_not_supported"
            except InvalidRegion:
                errors[CONF_REGION] = "invalid_region"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during EZVIZ login")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME], data=user_input
                )

        return self.async_show_form(
            step_id="user", data_schema=_ACCOUNT_SCHEMA, errors=errors
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: ConfigEntry,  # noqa: ARG003 — signature fixed by Home Assistant
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentry flows this integration supports."""
        return {CAMERA_SUBENTRY_TYPE: CameraSubentryFlowHandler}


class CameraSubentryFlowHandler(ConfigSubentryFlow):
    """Add a camera (subentry) to an EZVIZ account entry."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick a camera from the account and supply its verification code."""
        entry: EzvizStreamConfigEntry = self._get_entry()

        try:
            cameras = await self._async_account_cameras(entry)
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")
        except InvalidAuth, MfaRequired:
            return self.async_abort(reason="invalid_auth")

        already_added = {
            subentry.data[CONF_SERIAL] for subentry in entry.subentries.values()
        }
        available = [cam for cam in cameras if cam.serial not in already_added]
        if not available:
            return self.async_abort(reason="no_cameras")

        if user_input is not None:
            serial = user_input[CONF_SERIAL]
            camera = next(cam for cam in available if cam.serial == serial)
            return self.async_create_entry(
                title=camera.label,
                unique_id=serial,
                data={
                    CONF_SERIAL: serial,
                    CONF_VERIFICATION_CODE: user_input.get(CONF_VERIFICATION_CODE, ""),
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_SERIAL): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=cam.serial, label=cam.label)
                            for cam in available
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_VERIFICATION_CODE, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def _async_account_cameras(
        self, entry: EzvizStreamConfigEntry
    ) -> list[EzvizCamera]:
        """Return the account's streamable cameras, reusing a live session if any."""
        if entry.state is ConfigEntryState.LOADED and entry.runtime_data:
            api = entry.runtime_data.api
        else:
            api = EzvizCloudApi(async_get_clientsession(self.hass))
            await api.async_login(
                entry.data[CONF_USERNAME],
                entry.data[CONF_PASSWORD],
                entry.data[CONF_REGION],
            )
        return await api.async_get_cameras()
