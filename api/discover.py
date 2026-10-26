"""
The discover feed: who a viewer sees, and in what order.

1. Candidates — the caller's discoverable queryset, narrowed in the database
   to people near the viewer who were active recently enough.
2. Visibility — each persona stays listed for a while after its last activity,
   longer the more patient what it's looking for is (hookups fade in hours,
   long-term in weeks).
3. Score — a weighted sum of compatibility, distance, freshness and profile
   quality, each 0–1, plus a little per-day jitter so the order isn't frozen.
4. Snapshot — the ranked ids are cached so later pages continue the same list.

Everything tunable is in the constants below.
"""
import math
import random
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import ExpressionWrapper, F, FloatField, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from . import geo
from .models import LocationShareGrant

HOUR, DAY = timedelta(hours=1), timedelta(days=1)

# (freshness half-life, hidden after this long idle) by what a persona is looking for.
FRESHNESS = {
    'Hookups': (HOUR, 3 * HOUR),
    'FWB': (4 * HOUR, 12 * HOUR),
    'Chill': (4 * HOUR, 12 * HOUR),
    'Friends': (12 * HOUR, 2 * DAY),
    'Dates': (DAY, 3 * DAY),
    'Open relationship': (DAY, 3 * DAY),
    'Companionship': (3 * DAY, 21 * DAY),
    'Long-term': (3 * DAY, 21 * DAY),
    'Marriage-minded': (3 * DAY, 21 * DAY),
}
BROWSING_FRESHNESS = (12 * HOUR, 2 * DAY)   # nothing selected
LONGEST_WINDOW = max(window for _, window in [*FRESHNESS.values(), BROWSING_FRESHNESS])
ONLINE_WINDOW = timedelta(minutes=5)

INTENT_GROUPS = {
    'Hookups': 'casual', 'FWB': 'casual', 'Chill': 'casual',
    'Friends': 'social',
    'Dates': 'dating', 'Open relationship': 'dating',
    'Companionship': 'committed', 'Long-term': 'committed', 'Marriage-minded': 'committed',
}
NEIGHBOUR_GROUPS = {frozenset({'casual', 'dating'}), frozenset({'dating', 'committed'})}
SAME_TYPE, SAME_GROUP, NEIGHBOUR, BROWSING = 1.0, 0.7, 0.3, 0.5

WEIGHTS = {'compat': 0.40, 'distance': 0.25, 'freshness': 0.25, 'quality': 0.10}
COMPAT_WEIGHTS = {'intent': 0.5, 'position': 0.2, 'power': 0.1, 'kinks': 0.1, 'interests': 0.1}
SIDE_WITH_OTHER = 0.3
HOST_TRAVEL_BONUS = 0.05
DISTANCE_SCALE_KM = 5      # distance score halves at this distance
JITTER = 0.03

DEFAULT_RADIUS_KM, MAX_RADIUS_KM = 50, 200
# The database prefilters on true coordinates, so it's widened by more than the
# fuzz/cell size; the real cut is made on the position the viewer may know.
PREFILTER_MARGIN_KM = 8
PREFILTER_MARGIN_DEG = 0.08
MAX_CANDIDATES = 2000
PAGE_SIZE = 30
SNAPSHOT_TTL = 15 * 60
MAP_PIN_LIMIT = 200

SORTS = ('recommended', 'nearby', 'active', 'new')

POSITIONS = ['bottom', 'vers_bottom', 'vers', 'vers_top', 'top']
# Filters anyone may use, and ones that need Plus (ignored for free accounts).
FREE_FILTERS = {'max_distance_km', 'age_min', 'age_max', 'looking_for'}
PLUS_FILTERS = {'positions', 'kinks', 'online_now', 'verified_only', 'can_host', 'can_travel', 'groups'}


# ── Visibility & freshness ───────────────────────────────────────────────────

def freshness_profile(relationship_types) -> tuple[timedelta, timedelta]:
    """(half-life, window) for a persona; with several types, the most patient applies."""
    known = [FRESHNESS[t] for t in relationship_types or () if t in FRESHNESS]
    return max(known, key=lambda hw: hw[1]) if known else BROWSING_FRESHNESS


def idle(persona, now) -> timedelta:
    return now - (persona.last_active_at or persona.created_at)


def _is_demo(persona) -> bool:
    return getattr(getattr(persona, 'user', None), 'demo_handle', None) is not None


def is_visible(persona, now) -> bool:
    # Demo profiles stay listed while switched on, however long since they were "active".
    return _is_demo(persona) or idle(persona, now) <= freshness_profile(persona.relationship_types)[1]


def is_online(persona, now) -> bool:
    return idle(persona, now) <= ONLINE_WINDOW


def freshness(persona, now) -> float:
    if is_online(persona, now):
        return 1.0
    half_life = freshness_profile(persona.relationship_types)[0]
    return 0.5 ** (idle(persona, now) / half_life)


# ── Compatibility ────────────────────────────────────────────────────────────

def _type_pair(a: str, b: str) -> float:
    if a == b:
        return SAME_TYPE
    ga, gb = INTENT_GROUPS.get(a), INTENT_GROUPS.get(b)
    if ga is None or gb is None:
        return 0.0
    if ga == gb:
        return SAME_GROUP
    if 'social' in (ga, gb) or frozenset({ga, gb}) in NEIGHBOUR_GROUPS:
        return NEIGHBOUR
    return 0.0


def intent_compat(a_types, b_types) -> float:
    """How well two personas' "looking for" lists line up; Browsing is neutral."""
    if not a_types or not b_types:
        return BROWSING
    return max(_type_pair(a, b) for a in a_types for b in b_types)


def complement(a: int, b: int) -> float:
    """1 for opposite ends of a 0–4 scale (Top+Bottom, Dom+Sub), 0 for the same end."""
    return 1 - abs(a + b - 4) / 4


def position_compat(a, b) -> float | None:
    if a.is_side or b.is_side:
        return 1.0 if a.is_side and b.is_side else SIDE_WITH_OTHER
    if a.position is None or b.position is None:
        return None
    return complement(a.position, b.position)


def power_compat(a, b) -> float | None:
    if a.power_position is None or b.power_position is None:
        return None
    return complement(a.power_position, b.power_position)


def jaccard(a, b) -> float | None:
    a, b = set(a or ()), set(b or ())
    if not a or not b:
        return None
    return len(a & b) / len(a | b)


def compat(viewer, candidate) -> float:
    """Weighted average over the signals both sides have, plus a small host/travel bonus for casual."""
    signals = {
        'intent': intent_compat(viewer.relationship_types, candidate.relationship_types),
        'position': position_compat(viewer, candidate),
        'power': power_compat(viewer, candidate),
        'kinks': jaccard(viewer.kinks, candidate.kinks),
        'interests': jaccard(viewer.interests, candidate.interests),
    }
    present = {k: v for k, v in signals.items() if v is not None}
    score = sum(COMPAT_WEIGHTS[k] * v for k, v in present.items()) / sum(COMPAT_WEIGHTS[k] for k in present)
    casual = any(INTENT_GROUPS.get(t) == 'casual' for t in candidate.relationship_types or ())
    if casual and ((viewer.can_host and candidate.can_travel) or (viewer.can_travel and candidate.can_host)):
        score += HOST_TRAVEL_BONUS
    return min(score, 1.0)


# ── Other signals ────────────────────────────────────────────────────────────

def distance_score(km: float | None) -> float:
    return 0.0 if km is None else 1 / (1 + km / DISTANCE_SCALE_KM)


def quality(candidate, verified: bool) -> float:
    has_photo = bool(getattr(candidate, '_prefetched_objects_cache', {}).get('photos', candidate.photos.all()))
    return 0.5 * has_photo + 0.2 * bool(candidate.bio.strip()) + 0.3 * verified


def jitter(viewer_id: int, candidate_id: int, day) -> float:
    """Stable for a viewer/candidate pair within a day; changes the next day."""
    return random.Random(f'{viewer_id}:{candidate_id}:{day.isoformat()}').uniform(-JITTER, JITTER)


# ── Filters ──────────────────────────────────────────────────────────────────

def clean_filters(raw) -> dict:
    """Validated filters to store; drops unknown keys and malformed values."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in ('max_distance_km', 'age_min', 'age_max'):
        if isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool):
            out[key] = raw[key]
    if 'max_distance_km' in out:
        out['max_distance_km'] = max(1, min(out['max_distance_km'], MAX_RADIUS_KM))
    for key, allowed in (('looking_for', set(FRESHNESS)), ('positions', set(POSITIONS) | {'side'})):
        if isinstance(raw.get(key), list):
            out[key] = [v for v in raw[key] if v in allowed]
    if isinstance(raw.get('kinks'), list):
        out['kinks'] = [k for k in raw['kinks'] if isinstance(k, str)][:50]
    for key in ('online_now', 'verified_only', 'can_host', 'can_travel'):
        if raw.get(key) is True:
            out[key] = True
    if raw.get('groups') in ('only', 'hide'):
        out['groups'] = raw['groups']
    return out


def effective_filters(persona, is_plus: bool) -> dict:
    """The persona's saved filters that apply now; Plus-only ones stop applying without Plus."""
    filters = clean_filters(persona.discover_filters)
    allowed = FREE_FILTERS | (PLUS_FILTERS if is_plus else set())
    return {k: v for k, v in filters.items() if k in allowed}


def shown_age(persona):
    """The account's age where the persona shows it; groups have none."""
    return persona.user.age if persona.show_age and not persona.group_type else None


def _passes(candidate, filters, now, verified: bool) -> bool:
    if not candidate.group_type:
        age = shown_age(candidate)
        if 'age_min' in filters and (age is None or age < filters['age_min']):
            return False
        if 'age_max' in filters and (age is None or age > filters['age_max']):
            return False
    if filters.get('looking_for') and not set(filters['looking_for']) & set(candidate.relationship_types or ()):
        return False
    if filters.get('positions'):
        mine = 'side' if candidate.is_side else (
            POSITIONS[candidate.position] if candidate.position is not None else None)
        if mine not in filters['positions']:
            return False
    if filters.get('kinks') and not set(filters['kinks']) & set(candidate.kinks or ()):
        return False
    if filters.get('online_now') and not is_online(candidate, now):
        return False
    if filters.get('verified_only') and not verified:
        return False
    if filters.get('can_host') and not candidate.can_host:
        return False
    if filters.get('can_travel') and not candidate.can_travel:
        return False
    if filters.get('groups') == 'only' and not candidate.group_type:
        return False
    if filters.get('groups') == 'hide' and candidate.group_type:
        return False
    return True


# ── Ranking ──────────────────────────────────────────────────────────────────

def _bbox_q(center: tuple[float, float], radius_km: float) -> Q:
    """Database prefilter on true coordinates; a group follows its last active member."""
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(math.cos(math.radians(center[0])), 0.01))
    return Q(
        lat__range=(center[0] - dlat, center[0] + dlat),
        lng__range=(center[1] - dlng, center[1] + dlng),
    )


def with_coords(qs):
    return qs.annotate(
        lat=Coalesce('last_active_user__latitude', 'user__latitude'),
        lng=Coalesce('last_active_user__longitude', 'user__longitude'),
    )


def recent_q(now) -> Q:
    """Cheap database prefilter: active within the longest visibility window."""
    since = now - LONGEST_WINDOW
    return (
        Q(last_active_at__gte=since) | Q(last_active_at__isnull=True, created_at__gte=since)
        | Q(user__demo_handle__isnull=False)
    )


def _grant_levels(viewer_persona, now) -> dict[int, int]:
    """Location levels other personas have granted the viewer, by grantor id."""
    rows = LocationShareGrant.objects.filter(grantee=viewer_persona).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
    ).values_list('grantor_id', 'level')
    levels = {}
    for grantor_id, level in rows:
        levels[grantor_id] = max(level, levels.get(grantor_id, 0))
    return levels


class Ranked:
    """A scored candidate. `km`/`level` describe what the viewer may know about its distance."""
    __slots__ = ('persona', 'score', 'km', 'level')

    def __init__(self, persona, score, km, level):
        self.persona, self.score, self.km, self.level = persona, score, km, level

    @property
    def distance(self) -> str | None:
        return geo.distance_label(self.km, self.level)


def rank(qs, viewer_user, viewer_persona, *, sort='recommended', filters=None, radius_km=None,
         bbox=None, now=None, verified=lambda persona: False) -> list[Ranked]:
    """
    Visible personas from `qs` (already excluding hidden ones), best first.
    The viewer's stored location is the center; `bbox` (min_lat, min_lng, max_lat, max_lng)
    restricts to an area instead of a radius. `verified(persona)` decides the verified signal.
    """
    now = now or timezone.now()
    filters = filters or {}
    origin = (viewer_user.latitude, viewer_user.longitude)
    has_origin = None not in origin
    radius_km = filters.get('max_distance_km') or radius_km or DEFAULT_RADIUS_KM

    qs = with_coords(qs).filter(recent_q(now))
    if bbox is not None:
        m = PREFILTER_MARGIN_DEG
        qs = qs.filter(lat__range=(bbox[0] - m, bbox[2] + m), lng__range=(bbox[1] - m, bbox[3] + m))
    elif has_origin:
        nearby = _bbox_q(origin, radius_km + PREFILTER_MARGIN_KM)
        qs = qs.filter(nearby | Q(user__demo_handle__isnull=False) if settings.DEMO_PROFILES_FOR_EVERYONE else nearby)
    if has_origin:
        # Keep the nearest when there are more than MAX_CANDIDATES.
        d2 = (F('lat') - origin[0]) * (F('lat') - origin[0]) + (F('lng') - origin[1]) * (F('lng') - origin[1])
        qs = qs.annotate(d2=ExpressionWrapper(d2, output_field=FloatField())).order_by('d2')

    grants = _grant_levels(viewer_persona, now)
    today = now.date()
    ranked = []
    for persona in qs[:MAX_CANDIDATES]:
        if not is_visible(persona, now):
            continue
        is_verified = verified(persona)
        if not _passes(persona, filters, now, is_verified):
            continue
        level = max(persona.location_sharing, grants.get(persona.pk, 0))
        where = geo.position(persona, level)
        if bbox is not None and (where is None or not (
            bbox[0] <= where[0] <= bbox[2] and bbox[1] <= where[1] <= bbox[3]
        )):
            continue
        km = geo.haversine_km(origin, where) if has_origin and where is not None else None
        if bbox is None and km is not None and km > radius_km and not (
            settings.DEMO_PROFILES_FOR_EVERYONE and _is_demo(persona)
        ):
            continue
        score = (
            WEIGHTS['compat'] * compat(viewer_persona, persona)
            + WEIGHTS['distance'] * distance_score(km)
            + WEIGHTS['freshness'] * freshness(persona, now)
            + WEIGHTS['quality'] * quality(persona, is_verified)
            + jitter(viewer_persona.pk, persona.pk, today)
        )
        ranked.append(Ranked(persona, score, km, level))

    if sort == 'nearby':
        # By band, not exact distance, so the order reveals no more than the labels do.
        ranked.sort(key=lambda r: (r.km is None, _band_km(r.km) if r.km is not None else 0, -r.score))
    elif sort == 'active':
        ranked.sort(key=lambda r: (idle(r.persona, now), -r.score))
    elif sort == 'new':
        ranked.sort(key=lambda r: r.persona.created_at, reverse=True)
    else:
        ranked.sort(key=lambda r: -r.score)
    return ranked


def _band_km(km: float) -> int:
    return next((b for b in geo.DISTANCE_BANDS_KM if km < b), geo.DISTANCE_BANDS_KM[-1] + 1)


# ── Snapshots ────────────────────────────────────────────────────────────────

def _key(viewer_persona_id: int, token: str) -> str:
    return f'discover:{viewer_persona_id}:{token}'


def save_snapshot(viewer_persona_id: int, ids: list[int]) -> str:
    token = secrets.token_urlsafe(8)
    cache.set(_key(viewer_persona_id, token), ids, SNAPSHOT_TTL)
    return token


def load_snapshot(viewer_persona_id: int, cursor: str) -> tuple[str, list[int], int] | None:
    """(token, ids, offset) for a cursor `<token>:<offset>`, or None if it's malformed or expired."""
    token, _, offset = (cursor or '').partition(':')
    if not token or not offset.isdigit():
        return None
    ids = cache.get(_key(viewer_persona_id, token))
    return None if ids is None else (token, ids, int(offset))
