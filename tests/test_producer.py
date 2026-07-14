"""Tests for the go2rtc producer wiring.

The socket streaming itself is verified live (CI can't reach the cloud); here we
cover the producer's camera selection and error path with mocks.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.ezviz_stream import producer
from custom_components.ezviz_stream.api import EzvizCamera

if TYPE_CHECKING:
    from pathlib import Path


def _mock_session() -> MagicMock:
    """A fake aiohttp.ClientSession usable as an async context manager."""
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm


async def test_run_streams_matching_camera() -> None:
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(
        return_value=[
            EzvizCamera("OTHER", "x", "IPC", 1, 1, streamable=True),
            EzvizCamera("SN1", "Cam", "BatteryCamera", 1, 1, streamable=True),
        ]
    )
    creds = {
        "username": "u",
        "password": "p",
        "region": "Europe",
        "serial": "SN1",
        "stream": 2,
    }
    with (
        patch(
            "custom_components.ezviz_stream.producer.aiohttp.ClientSession",
            return_value=_mock_session(),
        ),
        patch(
            "custom_components.ezviz_stream.producer.EzvizCloudApi", return_value=api
        ),
        patch(
            "custom_components.ezviz_stream.producer.stream_annexb", AsyncMock()
        ) as stream,
    ):
        result = await producer._run(creds)

    assert result == 0
    assert stream.await_args.args[0].serial == "SN1"  # the matching camera
    assert stream.await_args.kwargs["stream"] == 2


async def test_run_missing_camera_returns_error() -> None:
    api = AsyncMock()
    api.async_get_cameras = AsyncMock(return_value=[])
    with (
        patch(
            "custom_components.ezviz_stream.producer.aiohttp.ClientSession",
            return_value=_mock_session(),
        ),
        patch(
            "custom_components.ezviz_stream.producer.EzvizCloudApi", return_value=api
        ),
        patch("custom_components.ezviz_stream.producer.stream_annexb", AsyncMock()),
    ):
        result = await producer._run(
            {"username": "u", "password": "p", "region": "Europe", "serial": "NOPE"}
        )

    assert result == 1


def test_main_reads_creds_file_and_runs(tmp_path: Path) -> None:
    """main() parses --creds-file, loads the JSON, and runs the async producer."""
    creds = {"username": "u", "password": "p", "region": "Europe", "serial": "SN1"}
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(json.dumps(creds))

    with (
        patch("sys.argv", ["producer", "--creds-file", str(creds_file)]),
        patch.object(producer, "_run", MagicMock(return_value="COROUTINE")) as run,
        patch.object(producer.asyncio, "run", MagicMock(return_value=0)) as asyncio_run,
    ):
        assert producer.main() == 0

    assert run.call_args.args[0] == creds  # the parsed creds were passed to _run
    asyncio_run.assert_called_once_with("COROUTINE")


def test_main_handles_keyboard_interrupt(tmp_path: Path) -> None:
    """Ctrl-C during streaming is a clean exit (status 0)."""
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(json.dumps({"serial": "SN1"}))

    with (
        patch("sys.argv", ["producer", "--creds-file", str(creds_file)]),
        patch.object(producer, "_run", MagicMock(return_value="C")),
        patch.object(producer.asyncio, "run", MagicMock(side_effect=KeyboardInterrupt)),
    ):
        assert producer.main() == 0
