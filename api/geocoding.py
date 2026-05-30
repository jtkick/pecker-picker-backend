"""
Reverse geocoding via Nominatim (OpenStreetMap).
Returns a human-readable neighborhood/area string for a coordinate.
"""

import logging
import requests

logger = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_HEADERS = {"User-Agent": "PeckerPicker/1.0 (jaredkick@gmail.com)"}
_TIMEOUT = 5


def nearby_label(lat: float, lng: float) -> str | None:
    """
    Return the name of a nearby business/amenity, or a street address, for
    exact coordinates.  Returns None when only a general area can be found
    (Nominatim zoom=18 returned just a road or suburb).
    """
    try:
        resp = requests.get(
            _NOMINATIM_URL,
            params={"lat": lat, "lon": lng, "format": "json", "zoom": 18},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Nominatim nearby_label failed for (%s, %s): %s", lat, lng, exc)
        return None

    addr = data.get("address", {})

    # Named place (the address key holds the place's name at zoom 18)
    place = (
        addr.get("amenity")
        or addr.get("shop")
        or addr.get("tourism")
        or addr.get("leisure")
        or addr.get("office")
        or addr.get("building")
    )
    if place and place.lower() not in ("yes", "no", "residential", "commercial"):
        return place

    # Street address
    house = addr.get("house_number", "")
    road = addr.get("road") or addr.get("pedestrian") or addr.get("footway")
    if house and road:
        return f"{house} {road}"

    return None


def reverse_geocode(lat: float, lng: float) -> str:
    """
    Return a short location label for the given coordinates.
    Examples: "Over-the-Rhine, Cincinnati", "Mayfair, London", "Downtown Dubai"
    Returns an empty string if the request fails.
    """
    try:
        resp = requests.get(
            _NOMINATIM_URL,
            params={"lat": lat, "lon": lng, "format": "json", "zoom": 14},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Nominatim reverse geocode failed for (%s, %s): %s", lat, lng, exc)
        return ""

    addr = data.get("address", {})

    # Most-specific area name, in priority order.
    area = (
        addr.get("neighbourhood")
        or addr.get("quarter")
        or addr.get("suburb")
        or addr.get("city_district")
        or addr.get("borough")
    )

    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("municipality")
    )

    if area and city:
        return f"{area}, {city}"
    if area:
        return area
    if city:
        state = addr.get("state") or addr.get("country")
        return f"{city}, {state}" if state else city
    return data.get("display_name", "").split(",")[0]
