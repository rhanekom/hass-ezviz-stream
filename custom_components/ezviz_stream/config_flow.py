"""Config flow for EZVIZ Stream: account step, then camera selection."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
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
    CONF_CAMERAS,
    CONF_REGION,
    CONF_VERIFICATION_CODE,
    DEFAULT_REGION,
    DOMAIN,
    REGION_API_CODES,
)

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
    """Handle the EZVIZ Stream config flow."""

    def __init__(self) -> None:
        """Initialise per-flow state carried between steps."""
        self._account: dict[str, str] = {}
        self._cameras: list[EzvizCamera] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: authenticate the EZVIZ account and discover its cameras."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api = EzvizCloudApi(async_get_clientsession(self.hass))
            try:
                await api.async_login(
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    user_input[CONF_REGION],
                )
                cameras = await api.async_get_cameras()
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
                if not cameras:
                    return self.async_abort(reason="no_cameras")
                self._account = user_input
                self._cameras = cameras
                return await self.async_step_cameras()

        return self.async_show_form(
            step_id="user", data_schema=_ACCOUNT_SCHEMA, errors=errors
        )

    async def async_step_cameras(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: pick cameras to add and supply the shared verification code."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._account[CONF_USERNAME],
                data={**self._account, **user_input},
            )

        options = [
            SelectOptionDict(value=cam.serial, label=cam.label) for cam in self._cameras
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CAMERAS, default=[cam.serial for cam in self._cameras]
                ): SelectSelector(SelectSelectorConfig(options=options, multiple=True)),
                vol.Optional(CONF_VERIFICATION_CODE, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="cameras", data_schema=schema)
