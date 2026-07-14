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
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
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
    CONF_SLOW_THUMBNAILS,
    CONF_STREAM,
    CONF_VERIFICATION_CODE,
    DEFAULT_REGION,
    DEFAULT_STREAM,
    DOMAIN,
    MAIN_STREAM,
    REGION_API_CODES,
    SUB_STREAM,
)
from .stream import grab_jpeg

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry, ConfigSubentry

    from . import EzvizStreamConfigEntry

_LOGGER = logging.getLogger(__name__)

# Collapsible section key for the non-essential camera settings.
_ADVANCED = "advanced"
# A config-time frame grab confirms the verification code + stream selection. It
# drives a full cloud session (slow, and prone to transient cloud/battery hiccups),
# so a failed grab is a soft block: the user can retry or save anyway.
_VERIFY_TIMEOUT = 30.0
_VERIFY_MAX_SESSIONS = 3


def _camera_options_schema(
    *, verification_code: str, slow_thumbnails: bool, stream: int
) -> vol.Schema:
    """
    Build the schema for a camera's editable settings (add + reconfigure).

    Only the verification code is shown up front; the cadence and stream (both with
    sensible defaults) live in a collapsed 'advanced' section.
    """
    return vol.Schema(
        {
            vol.Optional(
                CONF_VERIFICATION_CODE, default=verification_code
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Required(_ADVANCED): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_SLOW_THUMBNAILS, default=slow_thumbnails
                        ): BooleanSelector(),
                        vol.Required(CONF_STREAM, default=str(stream)): SelectSelector(
                            SelectSelectorConfig(
                                options=[
                                    SelectOptionDict(
                                        value=str(MAIN_STREAM), label="Main (HD)"
                                    ),
                                    SelectOptionDict(
                                        value=str(SUB_STREAM), label="Sub (lower-res)"
                                    ),
                                ],
                                mode=SelectSelectorMode.DROPDOWN,
                            )
                        ),
                    }
                ),
                {"collapsed": True},
            ),
        }
    )


def _flatten_options(user_input: dict[str, Any]) -> dict[str, Any]:
    """Merge the collapsed 'advanced' section back into a flat dict."""
    flat = {key: value for key, value in user_input.items() if key != _ADVANCED}
    flat.update(user_input.get(_ADVANCED, {}))
    return flat


def _battery_note(*, is_battery: bool) -> str:
    """Return a battery-drain warning to append to the form description, if apt."""
    if not is_battery:
        return ""
    return (
        " This is a battery-powered camera: live viewing streams from the cloud and "
        "drains the battery, so watch it only when needed (the stream runs only while "
        "someone is watching)."
    )


def _camera_subentry_data(serial: str, user_input: dict[str, Any]) -> dict[str, Any]:
    """Build the subentry data dict from a flattened options/reconfigure form."""
    return {
        CONF_SERIAL: serial,
        CONF_VERIFICATION_CODE: user_input.get(CONF_VERIFICATION_CODE, ""),
        CONF_SLOW_THUMBNAILS: user_input.get(CONF_SLOW_THUMBNAILS, False),
        CONF_STREAM: int(user_input[CONF_STREAM]),
    }


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

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],  # noqa: ARG002 - HA reauth signature
    ) -> ConfigFlowResult:
        """Start reauth when the stored credentials stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-validate the account with a new password, keeping user/region."""
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api = EzvizCloudApi(async_get_clientsession(self.hass))
            try:
                await api.async_login(
                    reauth_entry.data[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    reauth_entry.data[CONF_REGION],
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except MfaRequired:
                errors["base"] = "mfa_not_supported"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during EZVIZ reauth")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
            description_placeholders={CONF_USERNAME: reauth_entry.data[CONF_USERNAME]},
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: ConfigEntry,  # noqa: ARG003 - signature fixed by Home Assistant
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentry flows this integration supports."""
        return {CAMERA_SUBENTRY_TYPE: CameraSubentryFlowHandler}


class CameraSubentryFlowHandler(ConfigSubentryFlow):
    """Add a camera (subentry) to an EZVIZ account entry."""

    def __init__(self) -> None:
        """Initialise the flow; picked camera + pending save carry between steps."""
        super().__init__()
        self._camera: EzvizCamera | None = None
        # Set when a frame check fails, so the verify-failed menu can save anyway.
        self._pending_data: dict[str, Any] | None = None
        self._pending_subentry: ConfigSubentry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick a camera from the account, then configure it."""
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
            self._camera = next(
                cam for cam in available if cam.serial == user_input[CONF_SERIAL]
            )
            return await self.async_step_options()

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
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Supply the verification code, thumbnail cadence, and stream for a new cam."""
        camera = self._camera
        assert camera is not None  # noqa: S101 - set by async_step_user before we get here

        if user_input is not None:
            data = _camera_subentry_data(camera.serial, _flatten_options(user_input))
            if await self._async_frame_ok(data):
                return self.async_create_entry(
                    title=camera.label, unique_id=camera.serial, data=data
                )
            self._pending_data, self._pending_subentry = data, None
            return await self.async_step_verify_failed()

        # Battery cams default to the slower cadence; the user can override it here.
        return self.async_show_form(
            step_id="options",
            data_schema=_camera_options_schema(
                verification_code="",
                slow_thumbnails=camera.is_battery,
                stream=DEFAULT_STREAM,
            ),
            description_placeholders={
                "camera": camera.label,
                "battery_note": _battery_note(is_battery=camera.is_battery),
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an already-added camera's verification code, cadence, and stream."""
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            data = _camera_subentry_data(
                subentry.data[CONF_SERIAL], _flatten_options(user_input)
            )
            if await self._async_frame_ok(data):
                return self.async_update_and_abort(
                    self._get_entry(), subentry, data=data
                )
            self._pending_data, self._pending_subentry = data, subentry
            return await self.async_step_verify_failed()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_camera_options_schema(
                verification_code=subentry.data.get(CONF_VERIFICATION_CODE, ""),
                slow_thumbnails=subentry.data.get(CONF_SLOW_THUMBNAILS, False),
                stream=subentry.data.get(CONF_STREAM, DEFAULT_STREAM),
            ),
            description_placeholders={"camera": subentry.title},
        )

    async def async_step_verify_failed(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 - menu step signature
    ) -> SubentryFlowResult:
        """Offer retry or save-anyway after a failed frame check."""
        return self.async_show_menu(
            step_id="verify_failed", menu_options=["retry", "save_anyway"]
        )

    async def async_step_retry(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 - menu choice signature
    ) -> SubentryFlowResult:
        """Re-show the options/reconfigure form so the check runs again on submit."""
        if self._pending_subentry is not None:
            return await self.async_step_reconfigure()
        return await self.async_step_options()

    async def async_step_save_anyway(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 - menu choice signature
    ) -> SubentryFlowResult:
        """Save the camera despite the failed frame check."""
        assert self._pending_data is not None  # noqa: S101 - set before the menu
        if self._pending_subentry is not None:
            return self.async_update_and_abort(
                self._get_entry(), self._pending_subentry, data=self._pending_data
            )
        assert self._camera is not None  # noqa: S101 - add path always has a camera
        return self.async_create_entry(
            title=self._camera.label,
            unique_id=self._camera.serial,
            data=self._pending_data,
        )

    async def _async_frame_ok(self, data: dict[str, Any]) -> bool:
        """
        Grab one frame to confirm the verification code + stream (soft check).

        Returns True on a decoded frame. A False result is ambiguous (wrong code, or
        a sleeping cam / transient cloud error), so callers treat it as a soft block.
        """
        entry: EzvizStreamConfigEntry = self._get_entry()
        try:
            api = await self._async_api(entry)
            camera = next(
                (
                    cam
                    for cam in await api.async_get_cameras()
                    if cam.serial == data[CONF_SERIAL]
                ),
                None,
            )
            if camera is None:
                return False
            jpeg = await grab_jpeg(
                camera,
                api.async_get_vtdu_token,
                get_ffmpeg_manager(self.hass).binary,
                stream=data[CONF_STREAM],
                verification_code=data[CONF_VERIFICATION_CODE],
                duration=_VERIFY_TIMEOUT,
                max_sessions=_VERIFY_MAX_SESSIONS,
            )
        except CannotConnect, InvalidAuth, MfaRequired:
            return False
        return jpeg is not None

    async def _async_api(self, entry: EzvizStreamConfigEntry) -> EzvizCloudApi:
        """Return the account's API client, reusing a loaded session if any."""
        if entry.state is ConfigEntryState.LOADED and entry.runtime_data:
            return entry.runtime_data.api
        api = EzvizCloudApi(async_get_clientsession(self.hass))
        await api.async_login(
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            entry.data[CONF_REGION],
        )
        return api

    async def _async_account_cameras(
        self, entry: EzvizStreamConfigEntry
    ) -> list[EzvizCamera]:
        """Return the account's streamable cameras, reusing a live session if any."""
        api = await self._async_api(entry)
        return await api.async_get_cameras()
