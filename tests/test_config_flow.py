"""Tests for the EZVIZ Stream config + camera-subentry flows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_stream.api import (
    CannotConnect,
    EzvizCamera,
    InvalidAuth,
    MfaRequired,
)
from custom_components.ezviz_stream.const import (
    CAMERA_SUBENTRY_TYPE,
    CONF_REGION,
    CONF_SERIAL,
    CONF_VERIFICATION_CODE,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_ACCOUNT = {
    CONF_USERNAME: "user@example.com",
    CONF_PASSWORD: "hunter2",
    CONF_REGION: "Europe",
}
_CAMERAS = [
    EzvizCamera("SN1", "Front door", "IPC", 1, 1, streamable=True),
    EzvizCamera("SN2", "", "BatteryCamera", 1, 1, streamable=True),
]


def _patch_api(
    *, login: AsyncMock | None = None, cameras: list[EzvizCamera] | None = None
) -> Any:
    """Patch EzvizCloudApi in the config flow with mocked async methods."""
    api = AsyncMock()
    api.async_login = login or AsyncMock(return_value=None)
    api.async_get_cameras = AsyncMock(
        return_value=_CAMERAS if cameras is None else cameras
    )
    return patch(
        "custom_components.ezviz_stream.config_flow.EzvizCloudApi", return_value=api
    )


def _account_entry(
    *, subentries: list[ConfigSubentryData] | None = None
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data=_ACCOUNT,
        subentries_data=subentries or [],
    )


# --- account (main) flow ---------------------------------------------------- #
async def test_account_flow_creates_entry(hass: HomeAssistant) -> None:
    """The account step validates the login and creates the account entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with (
        _patch_api(),
        patch("custom_components.ezviz_stream.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _ACCOUNT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "user@example.com"
    assert result["data"] == _ACCOUNT


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (InvalidAuth, "invalid_auth"),
        (MfaRequired, "mfa_not_supported"),
        (CannotConnect, "cannot_connect"),
    ],
)
async def test_account_step_errors(
    hass: HomeAssistant, error: type[Exception], expected: str
) -> None:
    """Login failures surface as form errors and let the user retry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _patch_api(login=AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _ACCOUNT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


async def test_account_already_configured(hass: HomeAssistant) -> None:
    """A second entry for the same account (by email) is rejected."""
    _account_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _patch_api():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _ACCOUNT
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- camera subentry flow --------------------------------------------------- #
async def test_add_camera_subentry(hass: HomeAssistant) -> None:
    """Adding a camera creates a subentry with its serial and verification code."""
    entry = _account_entry()
    entry.add_to_hass(hass)

    with (
        _patch_api(),
        patch("custom_components.ezviz_stream.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, CAMERA_SUBENTRY_TYPE), context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM

        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {CONF_SERIAL: "SN1", CONF_VERIFICATION_CODE: "ABCDEF"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Front door"
    assert result["data"] == {CONF_SERIAL: "SN1", CONF_VERIFICATION_CODE: "ABCDEF"}


async def test_add_camera_aborts_when_all_added(hass: HomeAssistant) -> None:
    """When every streamable camera is already a subentry, adding aborts."""
    entry = _account_entry(
        subentries=[
            ConfigSubentryData(
                data={CONF_SERIAL: cam.serial, CONF_VERIFICATION_CODE: ""},
                subentry_type=CAMERA_SUBENTRY_TYPE,
                title=cam.label,
                unique_id=cam.serial,
            )
            for cam in _CAMERAS
        ]
    )
    entry.add_to_hass(hass)

    with _patch_api():
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, CAMERA_SUBENTRY_TYPE), context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_cameras"
