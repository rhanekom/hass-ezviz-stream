"""Constants for the EZVIZ Stream integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "ezviz_stream"

# Account config-entry keys (username/password come from homeassistant.const).
CONF_REGION: Final = "region"

# Camera subentries: the account is the config entry; each camera is a subentry
# carrying its own serial and (optional) Image-Encryption verification code.
CAMERA_SUBENTRY_TYPE: Final = "camera"
CONF_SERIAL: Final = "serial"
CONF_VERIFICATION_CODE: Final = "verification_code"
# Poll this camera's thumbnail less often. Defaults on for battery cameras (slow to
# wake, and each grab is a full cloud session), user-overridable in the subentry flow.
CONF_SLOW_THUMBNAILS: Final = "slow_thumbnails"
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
