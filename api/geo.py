"""
Where a persona is, as other people may know it. Exact positions are only
used when the persona shares Exact location; otherwise everything works from
a fuzzed point (General) or a coarse grid cell (Off), so neither distances
nor ordering can be used to pin someone down.
"""
import math
import random

# General-mode jitter (~±450 m) and Off-mode grid cell (~5 km).
FUZZ_DEG = 0.004
OFF_CELL_DEG = 0.045
DISTANCE_BANDS_KM = [1, 2, 3, 5, 10, 20, 50]


def fuzz_coords(lat: float, lng: float, seed: int) -> tuple[float, float]:
    """A deterministically jittered position (~±450 m) for General-mode personas."""
    rng = random.Random(seed)
    return lat + rng.uniform(-FUZZ_DEG, FUZZ_DEG), lng + rng.uniform(-FUZZ_DEG, FUZZ_DEG)


def cell_center(lat: float, lng: float) -> tuple[float, float]:
    """Center of the coarse grid cell containing a point (for Off-mode personas)."""
    return (
        (math.floor(lat / OFF_CELL_DEG) + 0.5) * OFF_CELL_DEG,
        (math.floor(lng / OFF_CELL_DEG) + 0.5) * OFF_CELL_DEG,
    )


def located_user(persona):
    """The account whose location places a persona (a group follows its last active member)."""
    if persona.group_type and persona.last_active_user is not None:
        return persona.last_active_user
    return persona.user


def position(persona, level: int) -> tuple[float, float] | None:
    """Where `persona` is at sharing `level` (0 Off, 1 General, 2 Exact); groups never go past General."""
    user = located_user(persona)
    if user.latitude is None or user.longitude is None:
        return None
    if persona.group_type:
        level = min(level, 1)
    if level >= 2:
        return user.latitude, user.longitude
    if level == 1:
        return fuzz_coords(user.latitude, user.longitude, persona.pk)
    return cell_center(user.latitude, user.longitude)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def distance_label(km: float | None, level: int) -> str | None:
    """What a card may say about distance: exact only at Exact, rounded bands at General, nothing at Off."""
    if km is None or level <= 0:
        return None
    if level >= 2:
        return f'{round(km * 1000, -1):.0f} m' if km < 1 else f'{km:.1f} km'
    if km < DISTANCE_BANDS_KM[0]:
        return f'< {DISTANCE_BANDS_KM[0]} km'
    for band in DISTANCE_BANDS_KM[1:]:
        if km < band:
            return f'{band} km'
    return f'{DISTANCE_BANDS_KM[-1]}+ km'
