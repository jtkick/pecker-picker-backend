"""
Handmade demo users, so there's something to browse while real members are
few. Only tester accounts ever see them (`demo_users tester add`), and each
can be switched on and off. They never fade from discover while on.

Define them in a folder holding users.json (a list) and each user's photos in
photos/<handle>/ (api/demo by default, or --dir; deploy/demo-profiles.sh syncs
a private folder to production). Every field except handle and display_name is optional:

    [
      {
        "handle": "river",
        "display_name": "River",
        "age": 29,
        "bio": "Coffee, climbing, bad puns.",
        "pronouns": "they/them",
        "height": 175,
        "length": null,
        "looking_for": ["Dates", "Friends"],
        "position": "vers",
        "power": null,
        "side": false,
        "kinks": [],
        "interests": ["Climbing"],
        "can_host": false,
        "can_travel": true,
        "location": [39.11, -84.51],
        "location_sharing": "general",
        "active_minutes_ago": 20,
        "photos": [{"file": "1.jpg", "tags": ["face"]}]
      }
    ]

  handle            stable id (lowercase letters, digits, _); renaming it makes a new user
  height            cm; length in tenths of an inch (0–120)
  looking_for       any of: Friends, Chill, Dates, Hookups, FWB, Companionship,
                    Long-term, Open relationship, Marriage-minded
  position          bottom | vers_bottom | vers | vers_top | top   (or "side": true)
  power             sub | sub_switch | switch | dom_switch | dom
  location_sharing  off | general | exact
  photo tags        face | torso | explicit | other
  kinks, interests  names exactly as in the app's lists (seed_kinks / seed_interests)

Usage:
    manage.py demo_users sync                    create/update everyone in the file
    manage.py demo_users sync --check            only report problems; change nothing
    manage.py demo_users list
    manage.py demo_users on  [HANDLE ...]        switch on (everyone if none given)
    manage.py demo_users off [HANDLE ...]        switch off
    manage.py demo_users remove HANDLE ...       delete users and their photos ("synthetic_*" for all synthetic)
    manage.py demo_users tester add|remove USER  USER is a user id or phone number
    manage.py demo_users synthetic N [--near LAT,LNG]   N random demo users (no photos)
"""
import json
import random
import re
import time
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from api.discover import FRESHNESS
from api.geocoding import reverse_geocode
from api.media import strip_image_metadata
from api.models import Interest, Kink, Persona, PersonaPhoto, ProfileImage, User

DEMO_DIR = Path(__file__).resolve().parents[2] / 'demo'

GEOCODE_INTERVAL = 1.1   # seconds; Nominatim allows 1 request/second
_last_geocode = 0.0


def _neighborhood(lat: float, lng: float) -> str:
    """reverse_geocode, at most once a second (Nominatim's usage policy)."""
    global _last_geocode
    time.sleep(max(0.0, _last_geocode + GEOCODE_INTERVAL - time.monotonic()))
    _last_geocode = time.monotonic()
    return reverse_geocode(lat, lng)

POSITIONS = {'bottom': 0, 'vers_bottom': 1, 'vers': 2, 'vers_top': 3, 'top': 4}
POWERS = {'sub': 0, 'sub_switch': 1, 'switch': 2, 'dom_switch': 3, 'dom': 4}
SHARING = {'off': 0, 'general': 1, 'exact': 2}
TAGS = {'face', 'torso', 'explicit', 'other'}
FIELDS = {
    'handle', 'display_name', 'age', 'bio', 'pronouns', 'height', 'length', 'looking_for', 'position',
    'power', 'side', 'kinks', 'interests', 'can_host', 'can_travel', 'location', 'location_sharing',
    'active_minutes_ago', 'photos',
}
HANDLE = re.compile(r'^[a-z0-9_]{1,40}$')
# Storage appends `_` + 7 random characters when a name is taken.
STORAGE_SUFFIX = re.compile(r'_[A-Za-z0-9]{7}(?=\.[^.]+$)')


def _check(entry: dict) -> list[str]:
    """Problems with one entry of users.json, as readable messages."""
    handle = entry.get('handle', '?')
    problems = [f'{handle}: unknown field "{k}"' for k in entry if k not in FIELDS]
    if not HANDLE.match(str(entry.get('handle', ''))):
        problems.append(f'{handle}: handle must be lowercase letters, digits or _')
    if not entry.get('display_name'):
        problems.append(f'{handle}: display_name is required')
    for key, allowed in (('position', POSITIONS), ('power', POWERS), ('location_sharing', SHARING)):
        if entry.get(key) is not None and entry[key] not in allowed:
            problems.append(f'{handle}: {key} must be one of {", ".join(allowed)}')
    unknown = set(entry.get('looking_for') or ()) - set(FRESHNESS)
    if unknown:
        problems.append(f'{handle}: unknown looking_for {sorted(unknown)}')
    location = entry.get('location')
    if location is not None and not (isinstance(location, list) and len(location) == 2):
        problems.append(f'{handle}: location must be [lat, lng]')
    for photo in entry.get('photos') or ():
        if set(photo.get('tags') or ()) - TAGS:
            problems.append(f'{handle}: photo {photo.get("file")} has unknown tags')
    return problems


def _image_name(handle: str, file: str) -> str:
    """Stored name, with the extension strip_image_metadata re-encodes to (PNG/WebP kept, else JPEG)."""
    path = Path(file)
    ext = path.suffix.lower() if path.suffix.lower() in ('.png', '.webp') else '.jpg'
    return f'demo_{handle}_{path.stem}{ext}'


def _unknown_names(entries: list[dict], field: str, model) -> list[str]:
    known = set(model.objects.values_list('name', flat=True))
    return [
        f'{e.get("handle", "?")}: unknown {field[:-1]} "{name}"'
        for e in entries for name in e.get(field) or () if name not in known
    ]


def _readable(path: Path) -> bool:
    try:
        with Image.open(path) as img:
            img.verify()
        return True
    except (UnidentifiedImageError, OSError):
        return False


class Command(BaseCommand):
    help = 'Handmade demo users that only tester accounts see. Run with no arguments for help.'

    def add_arguments(self, parser):
        parser.add_argument('action', nargs='?', choices=['sync', 'list', 'on', 'off', 'remove', 'tester', 'synthetic'])
        parser.add_argument('args', nargs='*', metavar='NAME')
        parser.add_argument('--dir', default=str(DEMO_DIR), help='Folder holding users.json and photos/.')
        parser.add_argument('--check', action='store_true', help='With sync: report problems, change nothing.')
        parser.add_argument('--near', default='39.1,-84.5', help='Center for synthetic users as LAT,LNG.')

    def handle(self, *args, action=None, **options):
        if action is None:
            self.stdout.write(__doc__)
            return
        names = list(args)   # Django passes the `args` positionals separately
        match action:
            case 'sync':
                self._sync(Path(options['dir']), check_only=options['check'])
            case 'list':
                self._list()
            case 'on' | 'off':
                self._switch(names, action == 'on')
            case 'remove':
                if not names:
                    raise CommandError('Name the handles to remove.')
                self._remove(names)
            case 'tester':
                if len(names) != 2 or names[0] not in ('add', 'remove'):
                    raise CommandError('Usage: demo_users tester add|remove USER')
                self._tester(names[1], names[0] == 'add')
            case 'synthetic':
                if len(names) != 1 or not names[0].isdigit():
                    raise CommandError('Usage: demo_users synthetic N [--near LAT,LNG]')
                lat, lng = (float(v) for v in options['near'].split(','))
                self._synthetic(int(names[0]), lat, lng)

    # ── sync ─────────────────────────────────────────────────────────────────

    def _sync(self, folder: Path, check_only: bool = False):
        path = folder / 'users.json'
        if not path.exists():
            raise CommandError(f'{path} not found.')
        try:
            entries = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise CommandError(f'{path}: {e}')
        if not isinstance(entries, list):
            raise CommandError(f'{path} must hold a list of users.')
        problems = [p for entry in entries for p in _check(entry)]
        handles = [e.get('handle') for e in entries]
        problems += [f'{h}: handle used twice' for h in {h for h in handles if handles.count(h) > 1}]
        problems += [
            f'{e["handle"]}: photo {p["file"]} not found'
            for e in entries for p in e.get('photos') or ()
            if not (folder / 'photos' / e['handle'] / p['file']).exists()
        ]
        problems += _unknown_names(entries, 'kinks', Kink) + _unknown_names(entries, 'interests', Interest)
        problems += [
            f'{e["handle"]}: photo {p["file"]} is not a readable image (use JPEG, PNG or WebP)'
            for e in entries for p in e.get('photos') or ()
            if (folder / 'photos' / e['handle'] / p['file']).exists()
            and not _readable(folder / 'photos' / e['handle'] / p['file'])
        ]
        if problems:
            raise CommandError('users.json has problems:\n  ' + '\n  '.join(problems))
        if check_only:
            photos = sum(len(e.get('photos') or ()) for e in entries)
            self.stdout.write(self.style.SUCCESS(f'✓ {len(entries)} demo users and {photos} photos look good.'))
            return
        for entry in entries:
            self._sync_one(entry, folder / 'photos' / entry['handle'])
        self.stdout.write(self.style.SUCCESS(f'✓ {len(entries)} demo users synced.'))

    @transaction.atomic
    def _sync_one(self, e: dict, photo_dir: Path):
        handle = e['handle']
        user = User.objects.filter(demo_handle=handle).first()
        created = user is None
        if created:
            user = User.objects.create_user(phone_number=None, demo_handle=handle)
        location = tuple(e['location']) if e.get('location') else (None, None)
        if location != (user.latitude, user.longitude):
            user.latitude, user.longitude = location
            user.location_updated_at = timezone.now()
            user.neighborhood = _neighborhood(*location) if None not in location else ''
        user.pronouns = e.get('pronouns') or ''
        user.height, user.length = e.get('height'), e.get('length')
        user.save()

        persona = user.get_active_persona()
        persona.display_name = e['display_name']
        persona.age = e.get('age')
        persona.bio = e.get('bio') or ''
        persona.relationship_types = e.get('looking_for') or []
        persona.intent = 1 if persona.relationship_types else 0
        persona.is_side = bool(e.get('side'))
        persona.position = POSITIONS.get(e.get('position'))
        persona.power_position = POWERS.get(e.get('power'))
        persona.kinks, persona.interests = e.get('kinks') or [], e.get('interests') or []
        persona.can_host, persona.can_travel = bool(e.get('can_host')), bool(e.get('can_travel'))
        persona.location_sharing = SHARING[e.get('location_sharing') or 'general']
        persona.last_active_at = timezone.now() - timedelta(minutes=e.get('active_minutes_ago', 30))
        persona.save()

        self._sync_photos(user, persona, e.get('photos') or [], photo_dir)
        state = 'created' if created else 'updated'
        self.stdout.write(f'  {handle}: {persona.display_name} — {state}{"" if user.is_active else " (off)"}')

    def _sync_photos(self, user, persona, photos: list[dict], photo_dir: Path):
        """Re-upload only when the photo files changed; tags are updated in place."""
        handle = user.demo_handle
        wanted = [_image_name(handle, p['file']) for p in photos]
        current = list(user.gallery_images.order_by('order'))
        stored = [STORAGE_SUFFIX.sub('', Path(img.image.name).name) for img in current]
        if stored != [STORAGE_SUFFIX.sub('', name) for name in wanted]:
            for img in current:
                img.image.delete(save=False)
                img.delete()
            current = []
            for i, photo in enumerate(photos):
                with open(photo_dir / photo['file'], 'rb') as f:
                    content = strip_image_metadata(f)
                if content is None:
                    raise CommandError(f'{handle}: {photo["file"]} is not a readable image.')
                img = ProfileImage(user=user, order=i)
                img.image.save(wanted[i], content, save=False)
                current.append(img)
        for img, photo in zip(current, photos):
            tags = set(photo.get('tags') or ())
            for tag in TAGS:
                setattr(img, f'tag_{tag}', tag in tags)
            img.save()
        persona.photos.all().delete()
        PersonaPhoto.objects.bulk_create(PersonaPhoto(persona=persona, image=img, order=i) for i, img in enumerate(current[:6]))

    # ── switching & housekeeping ─────────────────────────────────────────────

    def _demo_users(self, handles):
        """Demo users by handle; a trailing * matches a prefix (e.g. "synthetic_*")."""
        users = User.objects.filter(demo_handle__isnull=False)
        if handles:
            match = Q()
            for h in handles:
                q = Q(demo_handle__startswith=h[:-1]) if h.endswith('*') else Q(demo_handle=h)
                if not users.filter(q).exists():
                    raise CommandError(f'No demo users named {h}.')
                match |= q
            users = users.filter(match)
        return users

    def _switch(self, handles, on: bool):
        count = self._demo_users(handles).update(is_active=on)
        self.stdout.write(self.style.SUCCESS(f'✓ {count} demo users switched {"on" if on else "off"}.'))

    def _list(self):
        users = User.objects.filter(demo_handle__isnull=False).order_by('demo_handle')
        if not users:
            self.stdout.write('No demo users. Add some to api/demo/users.json and run `demo_users sync`.')
        for user in users:
            persona = user.get_active_persona()
            photos = persona.photos.count()
            state = 'on ' if user.is_active else 'off'
            self.stdout.write(f'  [{state}] {user.demo_handle:<20} {persona.display_name:<20} {photos} photos')
        testers = User.objects.filter(is_tester=True)
        self.stdout.write(f'\nTesters ({testers.count()}): '
                          + (', '.join(f'{u.pk} {u.phone_number or ""}'.strip() for u in testers) or 'none'))

    def _remove(self, handles):
        users = self._demo_users(handles)
        for user in users:
            for img in user.gallery_images.all():
                img.image.delete(save=False)
        count = users.count()
        users.delete()
        self.stdout.write(self.style.SUCCESS(f'✓ Removed {count} demo users.'))

    def _tester(self, ident: str, add: bool):
        user = User.objects.filter(pk=int(ident)).first() if ident.isdigit() and len(ident) < 10 else \
            User.objects.filter(phone_number=ident).first()
        if user is None or user.is_demo:
            raise CommandError(f'No real account matches {ident}.')
        user.is_tester = add
        user.save(update_fields=['is_tester'])
        self.stdout.write(self.style.SUCCESS(f'✓ User {user.pk} {"is now" if add else "is no longer"} a tester.'))

    # ── synthetic ────────────────────────────────────────────────────────────

    def _synthetic(self, count: int, lat: float, lng: float):
        """Random demo users (handles synthetic_NNNN) for exercising ranking at volume."""
        rng = random.Random()
        now = timezone.now()
        names = ['Alex', 'Sam', 'Jordan', 'Riley', 'Casey', 'Morgan', 'Quinn', 'Avery', 'Rowan', 'Jamie']
        interests = ['Hiking', 'Gaming', 'Cooking', 'Music', 'Travel', 'Fitness', 'Art', 'Movies']
        start = User.objects.filter(demo_handle__startswith='synthetic_').count()
        for i in range(start, start + count):
            user = User.objects.create_user(
                phone_number=None,
                demo_handle=f'synthetic_{i:04d}',
                # Within ~30 km of the center.
                latitude=lat + rng.uniform(-0.27, 0.27),
                longitude=lng + rng.uniform(-0.35, 0.35),
                pronouns=rng.choice(['', 'he/him', 'she/her', 'they/them']),
                height=rng.choice([None, rng.randint(155, 200)]),
            )
            persona = user.get_active_persona()
            persona.display_name = f'{rng.choice(names)} {i}'
            persona.age = rng.randint(18, 60)
            persona.relationship_types = rng.sample(list(FRESHNESS), rng.randint(0, 3))
            persona.intent = 1 if persona.relationship_types else 0
            persona.is_side = rng.random() < 0.1
            persona.position = rng.choice([None, 0, 1, 2, 3, 4])
            persona.power_position = rng.choice([None, 0, 1, 2, 3, 4])
            persona.interests = rng.sample(interests, rng.randint(0, 4))
            persona.can_host, persona.can_travel = rng.random() < 0.4, rng.random() < 0.5
            persona.location_sharing = rng.choice([0, 1, 1, 2])
            # Idle time skewed toward recent: minutes to weeks.
            persona.last_active_at = now - timedelta(minutes=rng.expovariate(1 / (60 * 24 * 2)))
            persona.save()
        self.stdout.write(self.style.SUCCESS(f'✓ Created {count} synthetic demo users near {lat},{lng}.'))
