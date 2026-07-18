"""Tests for the EZVIZ Stream config + camera-subentry flows."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntryState,
    ConfigSubentryData,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_stream.api import (
    CannotConnect,
    EzvizCamera,
    InvalidAuth,
    InvalidRegion,
    MfaRequired,
)
from custom_components.ezviz_stream.camera import _snapshot_path_for
from custom_components.ezviz_stream.config_flow import (
    CameraSubentryFlowHandler,
    _code_hint,
    _stored_interval,
)
from custom_components.ezviz_stream.const import (
    CAMERA_SUBENTRY_TYPE,
    CONF_FORCE_H264,
    CONF_IS_BATTERY,
    CONF_IS_ENCRYPTED,
    CONF_MAX_SNAPSHOTS,
    CONF_RECORDINGS_MODE,
    CONF_REGION,
    CONF_SERIAL,
    CONF_SLOW_THUMBNAILS,
    CONF_SNAPSHOT_INTERVAL,
    CONF_STATIC_ANCHOR,
    CONF_STREAM,
    CONF_THUMBNAIL_MODE,
    CONF_VERIFICATION_CODE,
    DEFAULT_SNAPSHOT_INTERVAL,
    DEFAULT_SNAPSHOT_INTERVAL_BATTERY,
    DOMAIN,
    RECORDINGS_MODE_DEFAULT,
    THUMBNAIL_INTERVAL,
    THUMBNAIL_MOTION,
    THUMBNAIL_STATIC_MOTION,
)
from custom_components.ezviz_stream.decrypt_image import password_hash

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


def _patch_api_raising(exc: type[Exception]) -> Any:
    """Patch EzvizCloudApi so login works but camera discovery raises ``exc``."""
    api = AsyncMock()
    api.async_login = AsyncMock(return_value=None)
    api.async_get_cameras = AsyncMock(side_effect=exc)
    return patch(
        "custom_components.ezviz_stream.config_flow.EzvizCloudApi", return_value=api
    )


def _sn1_subentry() -> ConfigSubentryData:
    """A minimal reconfigurable subentry for the mains camera SN1."""
    return ConfigSubentryData(
        data={
            CONF_SERIAL: "SN1",
            CONF_VERIFICATION_CODE: "OLD",
            CONF_THUMBNAIL_MODE: THUMBNAIL_INTERVAL,
            CONF_SNAPSHOT_INTERVAL: 30,
            CONF_STREAM: 1,
            CONF_IS_BATTERY: False,
            CONF_IS_ENCRYPTED: False,
        },
        subentry_type=CAMERA_SUBENTRY_TYPE,
        title="Front door",
        unique_id="SN1",
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
    """Adding a (non-battery) camera: pick it, set its code, motion thumbnail off."""
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
    # The confirmed frame is persisted so the new camera's tile shows immediately.
    snap = _snapshot_path_for(hass, "SN1")
    assert snap.exists()
    assert await hass.async_add_executor_job(snap.read_bytes) == b"jpeg-bytes"
    assert result["data"] == {
        CONF_SERIAL: "SN1",
        CONF_VERIFICATION_CODE: "ABCDEF",
        CONF_THUMBNAIL_MODE: THUMBNAIL_INTERVAL,  # SN1 is an IPC (mains) cam
        CONF_SNAPSHOT_INTERVAL: DEFAULT_SNAPSHOT_INTERVAL,  # mains default
        CONF_STREAM: 1,  # main stream by default
        CONF_FORCE_H264: False,  # native HEVC copy by default (go2rtc transcodes)
        CONF_RECORDINGS_MODE: RECORDINGS_MODE_DEFAULT,  # follow the account setting
        CONF_IS_BATTERY: False,
        # CONF_IS_ENCRYPTED omitted: encryption status is unknown for this test cam
    }


async def test_add_battery_camera_defaults_to_static_motion(
    hass: HomeAssistant,
) -> None:
    """A battery camera defaults to static-then-motion (no-wake alarm-image refresh)."""
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
    assert result["data"][CONF_THUMBNAIL_MODE] == THUMBNAIL_STATIC_MOTION
    assert result["data"][CONF_STATIC_ANCHOR] > 0  # anchored to "now" on save
    assert result["data"][CONF_SNAPSHOT_INTERVAL] == DEFAULT_SNAPSHOT_INTERVAL_BATTERY
    assert result["data"][CONF_STREAM] == 2  # battery cams default to the sub stream
    assert result["data"][CONF_IS_BATTERY] is True  # recorded at add time


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
    """Reconfiguring a camera updates its code, thumbnail settings and stream."""
    entry = _account_entry(
        subentries=[
            ConfigSubentryData(
                data={
                    CONF_SERIAL: "SN1",
                    CONF_VERIFICATION_CODE: "OLD",
                    CONF_THUMBNAIL_MODE: THUMBNAIL_INTERVAL,
                    CONF_SNAPSHOT_INTERVAL: 30,
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
                "advanced": {
                    CONF_THUMBNAIL_MODE: THUMBNAIL_MOTION,
                    CONF_SNAPSHOT_INTERVAL: 900,
                    CONF_STREAM: "2",
                },
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].data == {
        CONF_SERIAL: "SN1",  # unchanged
        CONF_VERIFICATION_CODE: "NEW",
        CONF_THUMBNAIL_MODE: THUMBNAIL_MOTION,
        CONF_SNAPSHOT_INTERVAL: 900,
        CONF_STREAM: 2,  # switched to sub stream
        CONF_FORCE_H264: False,  # not enabled in this reconfigure
        CONF_RECORDINGS_MODE: RECORDINGS_MODE_DEFAULT,  # unchanged (account default)
        CONF_IS_BATTERY: False,  # resolved from the account (SN1 is IPC)
        # CONF_IS_ENCRYPTED omitted: _CAMERAS leaves encryption status unknown
    }


async def test_reconfigure_enables_h264_transcode(hass: HomeAssistant) -> None:
    """Turning on the H.264 option in the advanced section persists it."""
    entry = _account_entry(
        subentries=[
            ConfigSubentryData(
                data={
                    CONF_SERIAL: "SN1",
                    CONF_VERIFICATION_CODE: "",
                    CONF_THUMBNAIL_MODE: THUMBNAIL_INTERVAL,
                    CONF_SNAPSHOT_INTERVAL: 30,
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
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_VERIFICATION_CODE: "",
                "advanced": {
                    CONF_THUMBNAIL_MODE: THUMBNAIL_INTERVAL,
                    CONF_SNAPSHOT_INTERVAL: 30,
                    CONF_STREAM: "1",
                    CONF_FORCE_H264: True,
                },
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert entry.subentries[subentry_id].data[CONF_FORCE_H264] is True


async def test_encrypted_camera_requires_and_validates_code(
    hass: HomeAssistant,
) -> None:
    """An encrypted camera requires a code; a wrong one is caught before any grab."""
    entry = _account_entry()
    entry.add_to_hass(hass)
    enc = EzvizCamera(
        "SNE",
        "Enc cam",
        "IPC",
        1,
        1,
        streamable=True,
        is_encrypted=True,
        encrypt_pwd_hash=password_hash("ABCDEF"),
    )
    with _patch_api(cameras=[enc]), _patch_frame_grab(ok=True):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, CAMERA_SUBENTRY_TYPE), context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_SERIAL: "SNE"}
        )
        assert result["step_id"] == "options"

        # Blank code -> required error (no grab).
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "", "advanced": {}}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {CONF_VERIFICATION_CODE: "code_required"}

        # Wrong code -> caught by the password hash, still no grab.
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "WRONG1", "advanced": {}}
        )
        assert result["errors"] == {CONF_VERIFICATION_CODE: "invalid_code"}

        # Correct code -> validates and saves.
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "ABCDEF", "advanced": {}}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_IS_ENCRYPTED] is True
    assert result["data"][CONF_VERIFICATION_CODE] == "ABCDEF"


async def test_static_motion_mode_records_an_anchor(hass: HomeAssistant) -> None:
    """Selecting 'static, then newer motion' stores a re-anchor timestamp on save."""
    entry = _account_entry()
    entry.add_to_hass(hass)
    with _patch_api(), _patch_frame_grab(ok=True):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, CAMERA_SUBENTRY_TYPE), context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_SERIAL: "SN1"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_VERIFICATION_CODE: "",
                "advanced": {CONF_THUMBNAIL_MODE: THUMBNAIL_STATIC_MOTION},
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_THUMBNAIL_MODE] == THUMBNAIL_STATIC_MOTION
    assert result["data"][CONF_STATIC_ANCHOR] > 0  # anchored to "now"


async def test_unencrypted_camera_shows_optional_verification_code(
    hass: HomeAssistant,
) -> None:
    """A clear camera still shows the code field, optionally, and saves without one."""
    entry = _account_entry()
    entry.add_to_hass(hass)
    cam = EzvizCamera(
        "SNU", "Clear cam", "IPC", 1, 1, streamable=True, is_encrypted=False
    )
    with _patch_api(cameras=[cam]), _patch_frame_grab(ok=True):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, CAMERA_SUBENTRY_TYPE), context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_SERIAL: "SNU"}
        )
        assert result["step_id"] == "options"
        markers = {marker.schema: marker for marker in result["data_schema"].schema}
        assert CONF_VERIFICATION_CODE in markers  # shown even for a clear camera
        assert isinstance(markers[CONF_VERIFICATION_CODE], vol.Optional)  # but optional

        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"advanced": {}}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_IS_ENCRYPTED] is False
    assert result["data"][CONF_VERIFICATION_CODE] == ""


async def test_unencrypted_camera_accepts_optional_code_for_old_recordings(
    hass: HomeAssistant,
) -> None:
    """A clear camera may still be given a code (to decrypt older recordings)."""
    entry = _account_entry()
    entry.add_to_hass(hass)
    cam = EzvizCamera(
        "SNU", "Clear cam", "IPC", 1, 1, streamable=True, is_encrypted=False
    )
    with _patch_api(cameras=[cam]), _patch_frame_grab(ok=True):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, CAMERA_SUBENTRY_TYPE), context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_SERIAL: "SNU"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "OLDKEY", "advanced": {}}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_IS_ENCRYPTED] is False
    assert result["data"][CONF_VERIFICATION_CODE] == "OLDKEY"


def test_code_hint_prompts_for_old_recordings_when_unencrypted() -> None:
    """The unencrypted hint is non-empty and mentions the old-recordings case."""
    hint = _code_hint(is_encrypted=False)
    assert hint  # field is now always shown, so the hint must not be empty
    assert "optional" in hint.lower()


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


# --- account/reauth extra error branches ------------------------------------ #
async def test_account_step_invalid_region(hass: HomeAssistant) -> None:
    """An unknown region reports against the region field, not base."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _patch_api(login=AsyncMock(side_effect=InvalidRegion)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _ACCOUNT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_REGION: "invalid_region"}


async def test_account_step_unknown_error(hass: HomeAssistant) -> None:
    """An unexpected exception during login surfaces as the generic 'unknown' error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _patch_api(login=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _ACCOUNT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MfaRequired, "mfa_not_supported"),
        (CannotConnect, "cannot_connect"),
        (RuntimeError, "unknown"),
    ],
)
async def test_reauth_step_errors(
    hass: HomeAssistant, error: type[Exception], expected: str
) -> None:
    """Reauth surfaces MFA/connectivity/unexpected failures as form errors."""
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with _patch_api(login=AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "wrong"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


# --- subentry add: account errors abort ------------------------------------- #
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (CannotConnect, "cannot_connect"),
        (InvalidAuth, "invalid_auth"),
        (MfaRequired, "invalid_auth"),
    ],
)
async def test_add_camera_aborts_on_account_error(
    hass: HomeAssistant, error: type[Exception], reason: str
) -> None:
    """If the account can't be reached to list cameras, adding aborts cleanly."""
    entry = _account_entry()
    entry.add_to_hass(hass)
    with _patch_api_raising(error):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, CAMERA_SUBENTRY_TYPE), context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


# --- reconfigure branches --------------------------------------------------- #
async def test_reconfigure_encrypted_requires_code(hass: HomeAssistant) -> None:
    """Reconfiguring an encrypted camera with a blank code re-shows the form."""
    entry = _account_entry(
        subentries=[
            ConfigSubentryData(
                data={
                    CONF_SERIAL: "SNE",
                    CONF_VERIFICATION_CODE: "OLD",
                    CONF_THUMBNAIL_MODE: THUMBNAIL_INTERVAL,
                    CONF_SNAPSHOT_INTERVAL: 30,
                    CONF_STREAM: 1,
                },
                subentry_type=CAMERA_SUBENTRY_TYPE,
                title="Enc cam",
                unique_id="SNE",
            )
        ]
    )
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))
    enc = EzvizCamera(
        "SNE",
        "Enc cam",
        "IPC",
        1,
        1,
        streamable=True,
        is_encrypted=True,
        encrypt_pwd_hash=password_hash("ABCDEF"),
    )
    with _patch_api(cameras=[enc]), _patch_frame_grab(ok=True):
        result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_VERIFICATION_CODE: "",
                "advanced": {
                    CONF_THUMBNAIL_MODE: THUMBNAIL_INTERVAL,
                    CONF_SNAPSHOT_INTERVAL: 30,
                    CONF_STREAM: "1",
                },
            },
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_VERIFICATION_CODE: "code_required"}


async def test_reconfigure_frame_check_fails_then_save_anyway(
    hass: HomeAssistant,
) -> None:
    """A failed reconfigure frame check saves the edited subentry via save-anyway."""
    entry = _account_entry(subentries=[_sn1_subentry()])
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))

    with _patch_api(), _patch_frame_grab(ok=False):
        result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_VERIFICATION_CODE: "NEW",
                "advanced": {
                    CONF_THUMBNAIL_MODE: THUMBNAIL_MOTION,
                    CONF_SNAPSHOT_INTERVAL: 900,
                    CONF_STREAM: "2",
                },
            },
        )
        assert result["type"] is FlowResultType.MENU
        assert result["step_id"] == "verify_failed"

        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"next_step_id": "save_anyway"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].data[CONF_VERIFICATION_CODE] == "NEW"


async def test_reconfigure_frame_check_retry_reopens_form(hass: HomeAssistant) -> None:
    """Choosing retry after a failed reconfigure check re-opens the reconfigure form."""
    entry = _account_entry(subentries=[_sn1_subentry()])
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))

    with _patch_api(), _patch_frame_grab(ok=False):
        result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_VERIFICATION_CODE: "NEW",
                "advanced": {
                    CONF_THUMBNAIL_MODE: THUMBNAIL_MOTION,
                    CONF_SNAPSHOT_INTERVAL: 900,
                    CONF_STREAM: "2",
                },
            },
        )
        assert result["step_id"] == "verify_failed"
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"next_step_id": "retry"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"


async def test_reconfigure_falls_back_when_account_unreachable(
    hass: HomeAssistant,
) -> None:
    """If the account is unreachable, reconfigure still opens using stored data."""
    entry = _account_entry(subentries=[_sn1_subentry()])
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))

    with _patch_api_raising(CannotConnect):
        result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"


# --- _stored_interval, _async_frame_ok, _async_api (unit) ------------------- #
def test_stored_interval_prefers_explicit_then_legacy_then_default() -> None:
    assert _stored_interval({CONF_SNAPSHOT_INTERVAL: 45}) == 45
    assert (
        _stored_interval({CONF_SLOW_THUMBNAILS: True})
        == DEFAULT_SNAPSHOT_INTERVAL_BATTERY
    )
    assert _stored_interval({}) == DEFAULT_SNAPSHOT_INTERVAL


async def test_frame_ok_false_when_camera_missing() -> None:
    """The frame check fails softly when the camera is not on the account."""
    handler = CameraSubentryFlowHandler()
    handler._get_entry = MagicMock(return_value=MagicMock())
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(return_value=[])
    with patch.object(handler, "_async_api", AsyncMock(return_value=api)):
        ok = await handler._async_frame_ok(
            {CONF_SERIAL: "SN1", CONF_STREAM: 1, CONF_VERIFICATION_CODE: ""}
        )
    assert ok is False


async def test_frame_ok_false_on_account_error() -> None:
    """The frame check fails softly when the account cannot be reached."""
    handler = CameraSubentryFlowHandler()
    handler._get_entry = MagicMock(return_value=MagicMock())
    with patch.object(handler, "_async_api", AsyncMock(side_effect=CannotConnect)):
        ok = await handler._async_frame_ok(
            {CONF_SERIAL: "SN1", CONF_STREAM: 1, CONF_VERIFICATION_CODE: ""}
        )
    assert ok is False


async def test_async_api_reuses_loaded_runtime_data() -> None:
    """A loaded entry hands back its live API client instead of logging in again."""
    handler = CameraSubentryFlowHandler()
    sentinel = object()
    entry = MagicMock()
    entry.state = ConfigEntryState.LOADED
    entry.runtime_data = SimpleNamespace(api=sentinel)
    assert await handler._async_api(entry) is sentinel
