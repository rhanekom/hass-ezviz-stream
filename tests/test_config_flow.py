"""Tests for the EZVIZ Stream config + camera-subentry flows."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
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
    CONF_MAX_SNAPSHOTS,
    CONF_REGION,
    CONF_SERIAL,
    CONF_SLOW_THUMBNAILS,
    CONF_STREAM,
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


@contextlib.contextmanager
def _patch_frame_grab(*, ok: bool) -> Any:
    """Patch the config-flow frame check: grab_jpeg returns a frame (ok) or None."""
    with (
        patch(
            "custom_components.ezviz_stream.config_flow.grab_jpeg",
            AsyncMock(return_value=b"jpeg-bytes" if ok else None),
        ),
        patch(
            "custom_components.ezviz_stream.config_flow.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="ffmpeg"),
        ),
    ):
        yield


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
    """Adding a (non-battery) camera: pick it, set its code, slow refresh off."""
    entry = _account_entry()
    entry.add_to_hass(hass)

    with _patch_api(), _patch_frame_grab(ok=True):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, CAMERA_SUBENTRY_TYPE), context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_SERIAL: "SN1"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "options"

        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "ABCDEF", "advanced": {}}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Front door"
    assert result["data"] == {
        CONF_SERIAL: "SN1",
        CONF_VERIFICATION_CODE: "ABCDEF",
        CONF_SLOW_THUMBNAILS: False,  # SN1 is an IPC (mains) cam
        CONF_STREAM: 1,  # main stream by default
    }


async def test_add_battery_camera_defaults_to_slow_thumbnails(
    hass: HomeAssistant,
) -> None:
    """A battery camera defaults the slow-thumbnail refresh on in the options step."""
    entry = _account_entry()
    entry.add_to_hass(hass)

    with _patch_api(), _patch_frame_grab(ok=True):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, CAMERA_SUBENTRY_TYPE), context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {CONF_SERIAL: "SN2"},  # the BatteryCamera
        )
        assert result["step_id"] == "options"
        # Submit the options step accepting the defaults (advanced section untouched).
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"advanced": {}}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SLOW_THUMBNAILS] is True
    assert result["data"][CONF_STREAM] == 2  # battery cams default to the sub stream


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


async def test_reconfigure_camera_subentry(hass: HomeAssistant) -> None:
    """Reconfiguring a camera updates its code, thumbnail cadence and stream."""
    entry = _account_entry(
        subentries=[
            ConfigSubentryData(
                data={
                    CONF_SERIAL: "SN1",
                    CONF_VERIFICATION_CODE: "OLD",
                    CONF_SLOW_THUMBNAILS: False,
                    CONF_STREAM: 1,
                },
                subentry_type=CAMERA_SUBENTRY_TYPE,
                title="Front door",
                unique_id="SN1",
            )
        ]
    )
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))

    with _patch_api(), _patch_frame_grab(ok=True):
        result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reconfigure"

        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_VERIFICATION_CODE: "NEW",
                "advanced": {CONF_SLOW_THUMBNAILS: True, CONF_STREAM: "2"},
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].data == {
        CONF_SERIAL: "SN1",  # unchanged
        CONF_VERIFICATION_CODE: "NEW",
        CONF_SLOW_THUMBNAILS: True,
        CONF_STREAM: 2,  # switched to sub stream
    }


async def test_add_camera_frame_check_fails_then_save_anyway(
    hass: HomeAssistant,
) -> None:
    """A failed frame check offers retry/save-anyway; save-anyway creates the camera."""
    entry = _account_entry()
    entry.add_to_hass(hass)

    with _patch_api(), _patch_frame_grab(ok=False):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, CAMERA_SUBENTRY_TYPE), context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_SERIAL: "SN1"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "BADCODE", "advanced": {}}
        )
        # Soft block: the frame check failed, so we land on the verify-failed menu.
        assert result["type"] is FlowResultType.MENU
        assert result["step_id"] == "verify_failed"

        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"next_step_id": "save_anyway"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERIAL] == "SN1"
    assert result["data"][CONF_VERIFICATION_CODE] == "BADCODE"


async def test_add_camera_frame_check_retry_reopens_form(hass: HomeAssistant) -> None:
    """Choosing retry on the verify-failed menu re-opens the options form."""
    entry = _account_entry()
    entry.add_to_hass(hass)

    with _patch_api(), _patch_frame_grab(ok=False):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, CAMERA_SUBENTRY_TYPE), context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_SERIAL: "SN1"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "BADCODE", "advanced": {}}
        )
        assert result["step_id"] == "verify_failed"

        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"next_step_id": "retry"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "options"


async def test_reauth_flow_updates_password(hass: HomeAssistant) -> None:
    """Reauth re-validates and stores a new password for the same account."""
    entry = _account_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with (
        _patch_api(),
        patch("custom_components.ezviz_stream.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"


async def test_reauth_flow_surfaces_invalid_auth(hass: HomeAssistant) -> None:
    """A bad password during reauth shows an error and keeps the form open."""
    entry = _account_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with _patch_api(login=AsyncMock(side_effect=InvalidAuth)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "wrong"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


# --- options flow ----------------------------------------------------------- #
async def test_options_flow_sets_max_snapshots(hass: HomeAssistant) -> None:
    """The account options flow stores the snapshot-concurrency limit as an int."""
    entry = _account_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    with patch("custom_components.ezviz_stream.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_MAX_SNAPSHOTS: 3}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MAX_SNAPSHOTS] == 3
    assert isinstance(entry.options[CONF_MAX_SNAPSHOTS], int)
