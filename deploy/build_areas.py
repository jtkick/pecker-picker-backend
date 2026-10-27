#!/usr/bin/env python3
"""
Builds the area lookup file (api/areas.py) from a Who's On First admin
database, e.g. whosonfirst-data-admin-us-latest.db from
https://data.geocode.earth/wof/dist/sqlite/ (CC-BY, credit "Who's On First").

    python3 deploy/build_areas.py whosonfirst-data-admin-us-latest.db data/areas.sqlite3

Keeps neighbourhoods (and macrohoods), localities and counties. Shapes are
simplified to ~20 m. For privacy, anything smaller than 1 km² is kept only
as a point, and points of one kind are thinned to at least 1 km apart, so
no area places someone more precisely than General location sharing does.
Standard library only; runs anywhere with Python 3.10+.
"""
import json
import math
import sqlite3
import sys
import time
import zlib
from array import array
from collections import defaultdict

MIN_AREA_KM2 = 1.0
MIN_POINT_SPACING_KM = 1.0
SIMPLIFY_DEG = 0.0002   # ~20 m
SCALE = 1e5             # stored coordinates are int(degrees × 1e5), ~1 m

KINDS = {'neighbourhood': 'neighbourhood', 'macrohood': 'neighbourhood',
         'locality': 'locality', 'county': 'county'}

LIVE = ("s.is_current != 0 AND s.is_deprecated != 1 AND s.is_superseded != 1 "
        "AND s.is_ceased != 1 AND s.country = 'US'")


def simplify(ring: list) -> list:
    """Douglas–Peucker on a closed ring of [lng, lat]."""
    if len(ring) < 5:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = ring[a]
        bx, by = ring[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, best_i = 0.0, -1
        for i in range(a + 1, b):
            px, py = ring[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / norm if norm else math.hypot(px - ax, py - ay)
            if d > best:
                best, best_i = d, i
        if best > SIMPLIFY_DEG:
            keep[best_i] = True
            stack.append((a, best_i))
            stack.append((best_i, b))
    return [p for p, k in zip(ring, keep) if k]


def encode(geometry: dict) -> tuple[bytes, tuple] | None:
    """[polygons][rings][points] as int32s: counts first, then lng/lat pairs."""
    polys = geometry['coordinates'] if geometry['type'] == 'MultiPolygon' else [geometry['coordinates']]
    out = array('i', [0])
    min_lat = min_lng = math.inf
    max_lat = max_lng = -math.inf
    n_polys = 0
    for poly in polys:
        rings = [r for r in (simplify(ring) for ring in poly) if len(r) >= 4]
        if not rings:
            continue
        n_polys += 1
        out.append(len(rings))
        for ring in rings:
            out.append(len(ring))
            for lng, lat in ring:
                out.append(round(lng * SCALE))
                out.append(round(lat * SCALE))
                min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
                min_lng, max_lng = min(min_lng, lng), max(max_lng, lng)
    if not n_polys:
        return None
    out[0] = n_polys
    return zlib.compress(out.tobytes(), 6), (min_lat, max_lat, min_lng, max_lng)


def km_between(a, b) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def thin(points: list[dict]) -> list[dict]:
    """Greedy: keeps the most populous first, dropping any within MIN_POINT_SPACING_KM of a kept one."""
    cell = MIN_POINT_SPACING_KM / 111
    grid = defaultdict(list)
    kept = []
    for p in sorted(points, key=lambda p: (-p['population'], p['id'])):
        cx, cy = int(p['lat'] // cell), int(p['lng'] // cell)
        near = (q for dx in (-1, 0, 1) for dy in (-1, 0, 1) for q in grid[(cx + dx, cy + dy)])
        if any(km_between((p['lat'], p['lng']), (q['lat'], q['lng'])) < MIN_POINT_SPACING_KM for q in near):
            continue
        grid[(cx, cy)].append(p)
        kept.append(p)
    return kept


def county_name(name: str, local: str | None) -> str:
    local = (local or 'County').title()
    return name if name.lower().endswith(local.lower()) else f'{name} {local}'


def main(src: str, dest: str):
    started = time.time()
    wof = sqlite3.connect(src)
    regions = {}
    for (body,) in wof.execute(f"""
            SELECT g.body FROM spr s JOIN geojson g ON g.id = s.id AND g.is_alt = 0
            WHERE {LIVE} AND s.placetype = 'region'"""):
        p = json.loads(body)['properties']
        regions[p['wof:id']] = p.get('wof:shortcode') or ''

    shaped, points = [], defaultdict(list)
    query = f"""
        SELECT s.id, s.placetype, s.latitude, s.longitude, g.body
        FROM spr s JOIN geojson g ON g.id = s.id AND g.is_alt = 0
        WHERE {LIVE}
          AND s.placetype IN ({','.join('?' * len(KINDS))})"""
    for n, (wid, placetype, lat, lng, body) in enumerate(wof.execute(query, list(KINDS))):
        if n % 20000 == 0:
            print(f'{n} places, {time.time() - started:.0f}s', flush=True)
        doc = json.loads(body)
        p, geom = doc['properties'], doc['geometry']
        hierarchy = (p.get('wof:hierarchy') or [{}])[0]
        kind = KINDS[placetype]
        name = p.get('wof:name') or ''
        if not name:
            continue
        area = {
            'id': wid,
            'kind': kind,
            'name': county_name(name, p.get('wof:placetype_local')) if kind == 'county' else name,
            'region': regions.get(hierarchy.get('region_id'), ''),
            'lat': p.get('lbl:latitude') or p.get('mps:latitude') or p.get('geom:latitude') or lat,
            'lng': p.get('lbl:longitude') or p.get('mps:longitude') or p.get('geom:longitude') or lng,
            'locality': hierarchy.get('locality_id') if kind == 'neighbourhood' else None,
            'km2': (p.get('geom:area_square_m') or 0) / 1e6,
            'population': p.get('wof:population') or 0,
        }
        if geom['type'] in ('Polygon', 'MultiPolygon') and area['km2'] >= MIN_AREA_KM2:
            encoded = encode(geom)
            if encoded:
                area['shape'], area['bbox'] = encoded
                shaped.append(area)
                continue
        points[kind].append(area)

    out = sqlite3.connect(dest)
    out.executescript("""
        DROP TABLE IF EXISTS areas; DROP TABLE IF EXISTS shapes; DROP TABLE IF EXISTS areas_index;
        DROP TABLE IF EXISTS meta;
        CREATE TABLE areas (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, region TEXT NOT NULL,
            lat REAL NOT NULL, lng REAL NOT NULL, locality INTEGER, km2 REAL NOT NULL, shaped INTEGER NOT NULL);
        CREATE INDEX areas_points ON areas (kind, shaped);
        CREATE TABLE shapes (id INTEGER PRIMARY KEY, data BLOB NOT NULL);
        CREATE VIRTUAL TABLE areas_index USING rtree (id, min_lat, max_lat, min_lng, max_lng);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    rows = shaped + [p for kind in points.values() for p in thin(kind)]
    for a in rows:
        is_shaped = 'shape' in a
        out.execute('INSERT INTO areas VALUES (?,?,?,?,?,?,?,?,?)',
                    (a['id'], a['kind'], a['name'], a['region'], a['lat'], a['lng'],
                     a['locality'], a['km2'], int(is_shaped)))
        bbox = a['bbox'] if is_shaped else (a['lat'], a['lat'], a['lng'], a['lng'])
        out.execute('INSERT INTO areas_index VALUES (?,?,?,?,?)', (a['id'], *bbox))
        if is_shaped:
            out.execute('INSERT INTO shapes VALUES (?,?)', (a['id'], a['shape']))
    out.execute("INSERT INTO meta VALUES ('built_at', ?)", (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),))
    out.execute("INSERT INTO meta VALUES ('source', ?)", (src.rsplit('/', 1)[-1],))
    out.commit()
    out.execute('VACUUM')

    counts = defaultdict(lambda: [0, 0])
    for a in rows:
        counts[a['kind']][0 if 'shape' in a else 1] += 1
    for kind, (s, pts) in sorted(counts.items()):
        print(f'{kind:14} {s:6} shapes {pts:6} points')
    print(f'done in {time.time() - started:.0f}s')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
