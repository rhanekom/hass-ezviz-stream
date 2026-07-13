"""Constants for the EZVIZ Stream integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "ezviz_stream"

# Config-entry / config-flow keys (username/password come from homeassistant.const).
CONF_REGION: Final = "region"
CONF_CAMERAS: Final = "cameras"  # list[str] of selected camera serials
CONF_VERIFICATION_CODE: Final = "verification_code"  # shared Image-Encryption code

DEFAULT_REGION: Final = "Europe"

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
