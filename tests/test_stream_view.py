"""Tests for the token-guarded MPEG-TS media view."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.setup import async_setup_component

from custom_components.ezviz_stream.stream_view import (
    EzvizStreamMediaView,
    register_stream,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.core import HomeAssistant


class _FakeBroadcast:
    """A broadcaster that yields a fixed pair of MPEG-TS chunks."""

    async def subscribe(self) -> AsyncIterator[bytes]:
        yield b"AB"
        yield b"CD"


async def _client(hass: HomeAssistant, hass_client_no_auth):  # noqa: ANN001, ANN202
    """Register the view (before the server starts) and return a test client."""
    assert await async_setup_component(hass, "http", {})
    hass.http.register_view(EzvizStreamMediaView(hass))
    return await hass_client_no_auth()


async def test_get_streams_for_valid_token(hass, hass_client_no_auth) -> None:  # noqa: ANN001
    """A correct token streams the broadcaster's MPEG-TS chunks."""
    register_stream(hass, "SN1", "good-token", _FakeBroadcast())
    client = await _client(hass, hass_client_no_auth)

    resp = await client.get("/api/ezviz_stream/SN1?token=good-token")
    assert resp.status == 200
    assert resp.content_type == "video/mp2t"
    assert await resp.read() == b"ABCD"


async def test_bad_token_is_not_found(hass, hass_client_no_auth) -> None:  # noqa: ANN001
    """A wrong token is rejected as 404 (indistinguishable from an unknown serial)."""
    register_stream(hass, "SN1", "good-token", _FakeBroadcast())
    client = await _client(hass, hass_client_no_auth)

    resp = await client.get("/api/ezviz_stream/SN1?token=wrong")
    assert resp.status == 404


async def test_unknown_serial_is_not_found(hass, hass_client_no_auth) -> None:  # noqa: ANN001
    """An unregistered serial is 404 even with a non-empty token."""
    client = await _client(hass, hass_client_no_auth)

    resp = await client.get("/api/ezviz_stream/NOPE?token=whatever")
    assert resp.status == 404
