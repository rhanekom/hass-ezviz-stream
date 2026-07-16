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
from homeassistant.components.ffmpeg import get_ffmpeg_manager

# Import from the defining module: homeassistant.components.http re-exports this
# without an explicit __all__ entry, which mypy's no_implicit_reexport rejects.
from homeassistant.helpers.http import HomeAssistantView

from .api import EzvizStreamApiError
from .broadcast import mp4_replay_source
from .cloud_replay import iter_cloud_replay_ps
from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.core import HomeAssistant

    from .api import EzvizCloudApi
    from .broadcast import CameraBroadcast

_LOGGER = logging.getLogger(__name__)

DATA_STREAMS = "media_streams"  # hass.data[DOMAIN][DATA_STREAMS]: serial -> _Stream


@dataclass
class _Stream:
    """
    A registered camera stream: token, live broadcaster, and replay inputs.

    ``api`` and ``verification_code`` let the replay view fetch a clip descriptor +
    playback ticket and decrypt the cloud-stored recording on demand.
    """

    token: str
    broadcast: CameraBroadcast
    api: EzvizCloudApi
    verification_code: str


def _registry(hass: HomeAssistant) -> dict[str, _Stream]:
    """Return (creating if needed) the serial -> stream registry."""
    return cast(
        "dict[str, _Stream]",
        hass.data.setdefault(DOMAIN, {}).setdefault(DATA_STREAMS, {}),
    )


def register_stream(  # noqa: PLR0913 - live + replay both keyed off the same camera
    hass: HomeAssistant,
    serial: str,
    token: str,
    broadcast: CameraBroadcast,
    api: EzvizCloudApi,
    verification_code: str,
) -> None:
    """Register a camera's broadcaster + replay inputs so the views can serve it."""
    _registry(hass)[serial] = _Stream(token, broadcast, api, verification_code)


def unregister_stream(hass: HomeAssistant, serial: str) -> None:
    """Remove a camera's broadcaster (on entity removal)."""
    _registry(hass).pop(serial, None)


def replay_token(hass: HomeAssistant, serial: str) -> str | None:
    """Return a registered camera's access token (for building a replay URL)."""
    entry = _registry(hass).get(serial)
    return entry.token if entry else None


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


class EzvizReplayView(HomeAssistantView):
    """
    Serve a cloud-stored recording as fragmented H.264 MP4 (token-guarded).

    Resolved from the media_source platform. On GET it fetches the clip descriptor
    and a fresh playback ticket, streams the encrypted cloud-replay clip through the
    decryptor, and transcodes it to a browser-playable fragmented MP4.
    """

    url = "/api/ezviz_stream/{serial}/replay/{seq_id}"
    name = "api:ezviz_stream:replay"
    requires_auth = False  # the per-camera token in the query guards it instead

    def __init__(self, hass: HomeAssistant) -> None:
        """Store the hass reference used to look up registered streams."""
        self.hass = hass

    async def get(
        self, request: web.Request, serial: str, seq_id: str
    ) -> web.StreamResponse:
        """Stream ``seq_id`` of ``serial`` as fragmented MP4 while the client stays."""
        entry = _registry(self.hass).get(serial)
        token = request.query.get("token", "")
        # One response for every failure mode so a caller can't probe valid ids.
        if entry is None or not hmac.compare_digest(token, entry.token):
            raise web.HTTPNotFound

        try:
            source = await self._replay_source(entry, serial, seq_id)
        except EzvizStreamApiError as err:
            _LOGGER.debug("replay %s/%s setup failed: %s", serial, seq_id, err)
            raise web.HTTPNotFound from err
        if source is None:
            raise web.HTTPNotFound

        response = web.StreamResponse()
        response.content_type = "video/mp4"
        await response.prepare(request)
        try:
            async for chunk in source:
                await response.write(chunk)
        except ConnectionError:  # client closed the tab / seeked away
            pass
        return response

    async def _replay_source(
        self, entry: _Stream, serial: str, seq_id: str
    ) -> AsyncIterator[bytes] | None:
        """Resolve the clip + ticket and return the transcoded MP4 byte stream."""
        cameras = await entry.api.async_get_cameras()
        camera = next((c for c in cameras if c.serial == serial), None)
        if camera is None:
            return None
        videos = await entry.api.async_get_cloud_videos(serial, camera.channel)
        rec = next(
            (
                v
                for v in videos
                if str(v.seq_id) == seq_id
                and v.stream_url
                and v.begin_cas
                and v.end_cas
            ),
            None,
        )
        if rec is None or rec.stream_url is None or rec.begin_cas is None:
            return None
        if rec.end_cas is None:
            return None
        ticket = await entry.api.async_get_camera_ticket(serial, camera.channel)
        ps_source = iter_cloud_replay_ps(
            stream_url=rec.stream_url,
            ticket=ticket,
            serial=serial,
            channel=camera.channel,
            seq_id=rec.seq_id,
            begin_cas=rec.begin_cas,
            end_cas=rec.end_cas,
            storage_version=rec.storage_version,
            verification_code=entry.verification_code if rec.crypt else "",
            file_size=rec.file_size,
        )
        ffmpeg_bin = get_ffmpeg_manager(self.hass).binary
        return mp4_replay_source(ffmpeg_bin, ps_source)
