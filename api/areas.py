"""
Named areas (neighbourhood → locality → county) for a coordinate, from the
read-only lookup file built by deploy/build_areas.py (Who's On First data).

Each account is placed in up to three areas when its location changes. The
map groups General-location profiles under one of them depending on zoom, and
the neighbourhood text on profiles comes from the same lookup.
"""
import functools
import math
import sqlite3
import threading
import zlib
from array import array
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

SCALE = 1e5
# How far the nearest point may be when no shape contains the coordinate.
POINT_RADIUS_KM = {'neighbourhood': 2.0, 'locality': 3.0, 'county': 100.0}
KINDS = ('neighbourhood', 'locality', 'county')

_local = threading.local()


@dataclass(frozen=True)
class Area:
    id: int
    kind: str
    name: str
    region: str
    lat: float
    lng: float


@dataclass(frozen=True)
class Placement:
    neighbourhood: Area | None
    locality: Area | None
    county: Area | None

    @property
    def label(self) -> str:
        """e.g. "Over-the-Rhine, Cincinnati", "Mason, OH", "Hamilton County, OH"."""
        if self.neighbourhood and self.locality:
            return f'{self.neighbourhood.name}, {self.locality.name}'
        best = self.neighbourhood or self.locality or self.county
        if best is None:
            return ''
        return f'{best.name}, {best.region}' if best.region else best.name


def _path() -> Path:
    return Path(settings.AREAS_DB)


def available() -> bool:
    return _path().is_file()


def _db() -> sqlite3.Connection:
    """One read-only connection per thread (and per file, so tests can swap it)."""
    path = _path()
    if getattr(_local, 'path', None) != path:
        _local.conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, check_same_thread=False)
        _local.path = path
        get.cache_clear()
    return _local.conn


def _area(row) -> Area:
    return Area(id=row[0], kind=row[1], name=row[2], region=row[3], lat=row[4], lng=row[5])


_COLUMNS = 'a.id, a.kind, a.name, a.region, a.lat, a.lng'


@functools.lru_cache(maxsize=4096)
def get(area_id: int) -> Area | None:
    row = _db().execute(f'SELECT {_COLUMNS} FROM areas a WHERE a.id = ?', (area_id,)).fetchone()
    return _area(row) if row else None


def _decode(blob: bytes) -> list[list[list[tuple[float, float]]]]:
    ints = array('i')
    ints.frombytes(zlib.decompress(blob))
    polys, i = [], 1
    for _ in range(ints[0]):
        rings = []
        n_rings = ints[i]
        i += 1
        for _ in range(n_rings):
            n = ints[i]
            i += 1
            rings.append([(ints[j] / SCALE, ints[j + 1] / SCALE) for j in range(i, i + 2 * n, 2)])
            i += 2 * n
        polys.append(rings)
    return polys


def _in_ring(lng: float, lat: float, ring) -> bool:
    inside = False
    x1, y1 = ring[-1]
    for x2, y2 in ring:
        if (y1 > lat) != (y2 > lat) and lng < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
        x1, y1 = x2, y2
    return inside


def _contains(polys, lat: float, lng: float) -> bool:
    # Inside an outer ring and none of its holes.
    return any(_in_ring(lng, lat, rings[0]) and not any(_in_ring(lng, lat, h) for h in rings[1:])
               for rings in polys)


def _containing(kind: str, lat: float, lng: float) -> Area | None:
    """The smallest shape of `kind` containing the point (shapes can overlap)."""
    # CROSS JOIN keeps SQLite driving from the R-tree; otherwise it scans by kind.
    rows = _db().execute(f"""
        SELECT {_COLUMNS}, s.data FROM areas_index i
        CROSS JOIN areas a ON a.id = i.id JOIN shapes s ON s.id = i.id
        WHERE i.min_lat <= ? AND i.max_lat >= ? AND i.min_lng <= ? AND i.max_lng >= ?
          AND a.kind = ? AND a.shaped = 1
        ORDER BY a.km2""", (lat, lat, lng, lng, kind))
    for row in rows:
        if _contains(_decode(row[6]), lat, lng):
            return _area(row)
    return None


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _nearest(kind: str, lat: float, lng: float, locality: int | None = None) -> Area | None:
    """The closest point of `kind` within its radius (for neighbourhoods, in the same locality)."""
    radius = POINT_RADIUS_KM[kind]
    dlat = radius / 111
    dlng = radius / (111 * max(math.cos(math.radians(lat)), 0.01))
    sql = f"""
        SELECT {_COLUMNS} FROM areas_index i CROSS JOIN areas a ON a.id = i.id
        WHERE i.min_lat >= ? AND i.max_lat <= ? AND i.min_lng >= ? AND i.max_lng <= ?
          AND a.kind = ? AND a.shaped = 0"""
    params = [lat - dlat, lat + dlat, lng - dlng, lng + dlng, kind]
    if locality is not None:
        sql += ' AND a.locality = ?'
        params.append(locality)
    best, best_km = None, radius
    for row in _db().execute(sql, params):
        km = _km((lat, lng), (row[4], row[5]))
        if km <= best_km:
            best, best_km = _area(row), km
    return best


def place(lat: float, lng: float) -> Placement:
    """The areas containing (or nearest to) a coordinate; any level may be missing."""
    county = _containing('county', lat, lng) or _nearest('county', lat, lng)
    locality = _containing('locality', lat, lng) or _nearest('locality', lat, lng)
    neighbourhood = _containing('neighbourhood', lat, lng) or (
        _nearest('neighbourhood', lat, lng, locality.id) if locality else None)
    return Placement(neighbourhood, locality, county)


def assign(user) -> None:
    """Sets the user's areas and neighbourhood label from their stored location (does not save)."""
    if user.latitude is None or user.longitude is None:
        user.area_neighbourhood = user.area_locality = user.area_county = None
        user.neighborhood = ''
        return
    placement = place(user.latitude, user.longitude)
    user.area_neighbourhood = placement.neighbourhood.id if placement.neighbourhood else None
    user.area_locality = placement.locality.id if placement.locality else None
    user.area_county = placement.county.id if placement.county else None
    user.neighborhood = placement.label[:100]


AREA_FIELDS = ['area_neighbourhood', 'area_locality', 'area_county', 'neighborhood']


def area_for(user, kind: str) -> Area | None:
    """The user's area at `kind`, or the nearest coarser one they have."""
    ids = [user.area_neighbourhood, user.area_locality, user.area_county]
    for area_id in ids[KINDS.index(kind):]:
        if area_id is not None and (area := get(area_id)) is not None:
            return area
    return None
