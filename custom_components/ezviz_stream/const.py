"""Constants for the EZVIZ Stream integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "ezviz_stream"

# Account config-entry keys (username/password come from homeassistant.const).
CONF_REGION: Final = "region"
# Account-level tuning (options flow): how many camera thumbnails may refresh from
# the cloud at once. A burst of grabs (a dashboard of camera cards cold-loading) can
# overwhelm EZVIZ's signalling with churn (result 5405), so 1 - serialise them - is
# the safest default; the last-motion-thumbnail option cuts battery-cam load further.
# Raise cautiously if multi-camera thumbnail fill is too slow; back off if the hard
# concurrency codes (5504/5546) appear. Gates only snapshot grabs - live streams are
# one cloud session per camera and ungated.
CONF_MAX_SNAPSHOTS: Final = "max_concurrent_snapshots"
DEFAULT_MAX_SNAPSHOTS: Final = 1
MAX_MAX_SNAPSHOTS: Final = 5

# Camera subentries: the account is the config entry; each camera is a subentry
# carrying its own serial and (optional) Image-Encryption verification code.
CAMERA_SUBENTRY_TYPE: Final = "camera"
CONF_SERIAL: Final = "serial"
CONF_VERIFICATION_CODE: Final = "verification_code"
# Cached at add time: is this a battery-powered camera? Exposed read-only on the
# entity and in the config flow. Stored so it needs no repeated cloud lookup; the
# entity self-resolves it once for cameras added before this was recorded.
CONF_IS_BATTERY: Final = "is_battery"
# Deprecated boolean cadence flag (kept only so subentries created before the
# explicit interval landed still map to a sensible TTL on read). Superseded by
# CONF_SNAPSHOT_INTERVAL; no longer written by the config flow.
CONF_SLOW_THUMBNAILS: Final = "slow_thumbnails"
# How often a *viewed* thumbnail refreshes (cache TTL, seconds). Each refresh of a
# non-motion thumbnail is a full cloud session that wakes a battery camera, so the
# battery default is deliberately long; mains cameras can afford a short interval.
# User-overridable per camera in the subentry flow's Advanced section.
CONF_SNAPSHOT_INTERVAL: Final = "snapshot_interval"
DEFAULT_SNAPSHOT_INTERVAL: Final = 30
DEFAULT_SNAPSHOT_INTERVAL_BATTERY: Final = 600
MIN_SNAPSHOT_INTERVAL: Final = 15
MAX_SNAPSHOT_INTERVAL: Final = 21600  # 6 h - for battery cams woken very rarely
# Use the last cloud motion/alarm image as the thumbnail instead of a live grab, so a
# battery camera is never woken just to fill a tile (reference A.8.1). Defaults on for
# battery cameras. The still-image payload may be encrypted (reference B.10.2).
CONF_MOTION_THUMBNAIL: Final = "motion_thumbnail"
# Marker prefix of an encrypted still image (alarm snapshot); see reference B.10.2.
HIK_ENCRYPTION_HEADER: Final = b"hikencodepicture"
# Which track to stream/snapshot: main (1, HD) or sub (2, lower-res, less bandwidth).
CONF_STREAM: Final = "stream"
MAIN_STREAM: Final = 1
SUB_STREAM: Final = 2
DEFAULT_STREAM: Final = MAIN_STREAM

DEFAULT_REGION: Final = "Europe"

# EZVIZ ``deviceCategory`` for battery-powered cameras (matches pyezvizapi's
# DeviceCatagories.BATTERY_CAMERA_DEVICE_CATEGORY).
BATTERY_CAMERA_CATEGORY: Final = "BatteryCamera"

# Domain of the official Home Assistant `ezviz` integration. We reuse its device
# identifier so our camera lands on the same device card when it is installed, and
# stand alone (our own device) when it isn't (spec §6.3).
OFFICIAL_EZVIZ_DOMAIN: Final = "ezviz"
MANUFACTURER: Final = "EZVIZ"

# EZVIZ account region -> API host code (`api<code>.ezvizlife.com`). South Africa
# routes through the Europe node (see doc/reference.md A.1).
REGION_API_CODES: Final[dict[str, str]] = {
    "Europe": "ieu",
    "Africa": "ieu",
    "Asia": "isgp",
    "Singapore": "isgp",
    "India": "iindia",
    "NorthAmerica": "ius",
    "Oceania": "ius",
    "SouthAmerica": "isa",
}
