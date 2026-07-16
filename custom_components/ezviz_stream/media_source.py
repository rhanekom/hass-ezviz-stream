"""
Media source: browse and play a camera's cloud-stored recordings.

Exposes each configured camera's EZVIZ cloud clips in Home Assistant's media
browser: root -> camera -> recordings. Listing is plain HTTPS (no camera wake).
Selecting a clip resolves to the token-guarded replay view
(:class:`~.stream_view.EzvizReplayView`), which streams the decrypted clip as
fragmented H.264 MP4.

The official ``ezviz`` integration has no recordings/playback, so this is net-add.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Import from the defining module: media_player re-exports these without an
# explicit __all__ entry, which mypy's no_implicit_reexport rejects.
from homeassistant.components.media_player.const import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.config_entries import ConfigEntryState

from .const import (
    CAMERA_SUBENTRY_TYPE,
    CONF_ENABLE_RECORDINGS,
    CONF_RECORDINGS_MODE,
    CONF_SERIAL,
    DEFAULT_ENABLE_RECORDINGS,
    DEFAULT_RECORDINGS_MODE,
    DOMAIN,
    RECORDINGS_MODE_DEFAULT,
    RECORDINGS_MODE_ON,
)
from .stream_view import replay_token

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import EzvizStreamConfigEntry
    from .api import EzvizCloudApi

_MIME_TYPE = "video/mp4"


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Set up the EZVIZ Stream recordings media source."""
    return EzvizRecordingsMediaSource(hass)


class EzvizRecordingsMediaSource(MediaSource):
    """Browse and resolve EZVIZ cloud recordings for playback."""

    name = "EZVIZ Recordings"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the media source."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a ``serial/seq_id`` clip to the token-guarded replay URL."""
        serial, _, seq_id = item.identifier.partition("/")
        if not serial or not seq_id:
            msg = f"Invalid EZVIZ recording id: {item.identifier!r}"
            raise Unresolvable(msg)
        token = replay_token(self.hass, serial)
        if token is None:
            msg = f"Camera {serial} is not available for playback"
            raise Unresolvable(msg)
        url = f"/api/ezviz_stream/{serial}/replay/{seq_id}?token={token}"
        return PlayMedia(url, _MIME_TYPE)

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse cameras (root) or one camera's recordings."""
        if not item.identifier:
            return self._browse_root()
        serial, _, seq_id = item.identifier.partition("/")
        if seq_id:  # a clip is a leaf, not browsable
            msg = f"{item.identifier!r} is a recording, not a folder"
            raise Unresolvable(msg)
        return await self._browse_camera(serial)

    def _browse_root(self) -> BrowseMediaSource:
        """List every configured, loaded camera as a folder."""
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=serial,
                media_class=MediaClass.DIRECTORY,
                media_content_type=MediaType.VIDEO,
                title=title,
                can_play=False,
                can_expand=True,
                children_media_class=MediaClass.VIDEO,
            )
            for _entry, serial, title in self._cameras()
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title="EZVIZ Recordings",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )

    async def _browse_camera(self, serial: str) -> BrowseMediaSource:
        """List one camera's cloud recordings, newest first."""
        match = next((c for c in self._cameras() if c[1] == serial), None)
        if match is None:
            msg = f"Unknown camera {serial}"
            raise Unresolvable(msg)
        entry, _serial, title = match
        api: EzvizCloudApi = entry.runtime_data.api
        camera = next(
            (c for c in await api.async_get_cameras() if c.serial == serial), None
        )
        channel = camera.channel if camera is not None else 1
        recordings = await api.async_get_cloud_videos(serial, channel)
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{serial}/{rec.seq_id}",
                media_class=MediaClass.VIDEO,
                media_content_type=MediaType.VIDEO,
                title=rec.start_time or rec.begin_cas or rec.seq_id,
                can_play=True,
                can_expand=False,
            )
            for rec in recordings
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=serial,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=title,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.VIDEO,
            children=children,
        )

    def _cameras(self) -> list[tuple[EzvizStreamConfigEntry, str, str]]:
        """Return (entry, serial, title) for every camera of every loaded account."""
        out: list[tuple[EzvizStreamConfigEntry, str, str]] = []
        entries: list[EzvizStreamConfigEntry] = self.hass.config_entries.async_entries(
            DOMAIN
        )
        for entry in entries:
            if entry.state is not ConfigEntryState.LOADED:
                continue
            # Recordings in the media library are opt-in (off by default, for privacy).
            # The account setting is the default; each camera can override it.
            account_on = entry.options.get(
                CONF_ENABLE_RECORDINGS, DEFAULT_ENABLE_RECORDINGS
            )
            for subentry in entry.subentries.values():
                if subentry.subentry_type != CAMERA_SUBENTRY_TYPE:
                    continue
                serial = subentry.data.get(CONF_SERIAL)
                if not serial:
                    continue
                mode = subentry.data.get(CONF_RECORDINGS_MODE, DEFAULT_RECORDINGS_MODE)
                enabled = (
                    account_on
                    if mode == RECORDINGS_MODE_DEFAULT
                    else mode == RECORDINGS_MODE_ON
                )
                if enabled:
                    out.append((entry, serial, subentry.title or serial))
        return out
