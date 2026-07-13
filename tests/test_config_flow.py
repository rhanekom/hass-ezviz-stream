"""Tests for the EZVIZ Stream config flow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from custom_components.ezviz_stream.api import (
    CannotConnect,
    EzvizCamera,
    InvalidAuth,
    MfaRequired,
)
from custom_components.ezviz_stream.const import (
    CONF_CAMERAS,
    CONF_REGION,
    CONF_VERIFICATION_CODE,
    DOMAIN,
)

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


async def test_full_flow(hass: HomeAssistant) -> None:
    """Account step then camera step creates an entry with the chosen data."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with _patch_api():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _ACCOUNT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cameras"

    with patch(
        "custom_components.ezviz_stream.async_setup_entry", return_value=True
    ) as mock_setup:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CAMERAS: ["SN1"], CONF_VERIFICATION_CODE: "ABCDEF"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "user@example.com"
    assert result["data"] == {
        **_ACCOUNT,
        CONF_CAMERAS: ["SN1"],
        CONF_VERIFICATION_CODE: "ABCDEF",
    }
    assert len(mock_setup.mock_calls) == 1


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
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}


async def test_no_streamable_cameras_aborts(hass: HomeAssistant) -> None:
    """An account with no streamable cameras aborts cleanly."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _patch_api(cameras=[]):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _ACCOUNT
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_cameras"


async def test_already_configured_aborts(hass: HomeAssistant) -> None:
    """A second entry for the same account (by email) is rejected."""
    MockConfigEntry(
        domain=DOMAIN, unique_id="user@example.com", data=_ACCOUNT
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _patch_api():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _ACCOUNT
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
