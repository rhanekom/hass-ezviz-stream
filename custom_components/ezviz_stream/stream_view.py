"""
HTTP view serving a camera's live MPEG-TS stream to go2rtc and the stream component.

go2rtc refuses ``exec:`` sources via its API (only ``rtsp``/``http``-style sources
are accepted), and HA's ``stream`` component can only ffmpeg-open a URL - so
``stream_source()`` points here. This view runs the camera's on-demand
:class:`~.broadcast.CameraBroadcast` and streams MPEG-TS to whoever connects
(go2rtc, the stream component, or ffmpeg for a snapshot); the broadcaster ensures
they all share one cloud session.

The endpoint carries a per-camera random token in the query (checked in constant
time) rather than HA auth, so a local, credential-less consumer like go2rtc can read
it; account credentials never touch this path.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from aiohttp import web

# Import from the defining module: homeassistant.components.http re-exports this
# without an explicit __all__ entry, which mypy's no_implicit_reexport rejects.
from homeassistant.helpers.http import HomeAssistantView

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .broadcast import CameraBroadcast

_LOGGER = logging.getLogger(__name__)

DATA_STREAMS = "media_streams"  # hass.data[DOMAIN][DATA_STREAMS]: serial -> _Stream


@dataclass
class _Stream:
    """A registered camera stream: its access token and broadcaster."""

    token: str
    broadcast: CameraBroadcast


def _registry(hass: HomeAssistant) -> dict[str, _Stream]:
    """Return (creating if needed) the serial -> stream registry."""
    return cast(
        "dict[str, _Stream]",
        hass.data.setdefault(DOMAIN, {}).setdefault(DATA_STREAMS, {}),
    )


def register_stream(
    hass: HomeAssistant, serial: str, token: str, broadcast: CameraBroadcast
) -> None:
    """Register a camera's broadcaster so the view can serve it."""
    _registry(hass)[serial] = _Stream(token, broadcast)


def unregister_stream(hass: HomeAssistant, serial: str) -> None:
    """Remove a camera's broadcaster (on entity removal)."""
    _registry(hass).pop(serial, None)


class EzvizStreamMediaView(HomeAssistantView):
    """Serve on-demand MPEG-TS for a camera, guarded by a per-camera token."""

    url = "/api/ezviz_stream/{serial}"
    name = "api:ezviz_stream"
    requires_auth = False  # a per-camera token in the query guards it instead

    def __init__(self, hass: HomeAssistant) -> None:
        """Store the hass reference used to look up registered streams."""
        self.hass = hass

    async def get(self, request: web.Request, serial: str) -> web.StreamResponse:
        """Stream MPEG-TS for ``serial`` while the client stays connected."""
        entry = _registry(self.hass).get(serial)
        token = request.query.get("token", "")
        # One response for both failure modes so a caller can't probe valid serials.
        if entry is None or not hmac.compare_digest(token, entry.token):
            raise web.HTTPNotFound

        response = web.StreamResponse()
        response.content_type = "video/mp2t"
        await response.prepare(request)
        try:
            async for chunk in entry.broadcast.subscribe():
                await response.write(chunk)
        except ConnectionError:  # covers ConnectionResetError
            pass  # client (go2rtc/ffmpeg) disconnected; unsubscribe happens in finally
        return response
