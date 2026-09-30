"""Fixed ZIP coverage; street addresses remain caller supplied for technician review."""

import json
import re
from pathlib import Path

from app.config import Settings

SERVICE_ZIPS = json.loads(
    Path(__file__).with_name("service_zipcodes.json").read_text()
)["zip_codes"]


def postal_code_from_address(address: str | None) -> str | None:
    """Accept a ZIP only at the end of a volunteered address, never a street number."""
    match = re.search(
        r"\b(?:[A-Z]{2}|New York)[,\s]+([0-9]{5})(?:-[0-9]{4})?\s*$",
        address or "",
        re.IGNORECASE,
    )
    return match[1] if match else None


def check_service_zip(postal_code: str | None, settings: Settings) -> dict:
    """Exact allowlist membership is authoritative; no geocoding or numeric ranges."""
    if not postal_code:
        return {"zip_status": "missing", "in_service_area": None}
    location = SERVICE_ZIPS.get(postal_code)
    eligible = bool(
        location
        and settings.service_state == "NY"
        and location["county"].lower() in settings.counties
    )
    result = {"zip_status": "checked", "in_service_area": eligible}
    if location:
        result.update(
            state="NY",
            county=location["county"],
            zip_city=location["city"],
            latitude=location["latitude"],
            longitude=location["longitude"],
            coordinates_basis="zip_centroid",
        )
    return result
