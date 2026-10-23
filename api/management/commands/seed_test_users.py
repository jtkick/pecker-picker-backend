"""
Management command to create and destroy test users.

Usage:
    python manage.py seed_test_users          # create / refresh
    python manage.py seed_test_users --clear  # delete all test users

Custom images:
    Place image files in api/management/commands/seed_images/ and reference
    them by filename in the TEST_USERS data:
        "avatar_image": "dapper_downy.jpg"          # overrides avatar_color
        "gallery_image_files": ["a.jpg", "b.jpg"]   # overrides gallery_colors
    Missing files fall back to the color-generated placeholders.
"""

import io
from pathlib import Path
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone
from PIL import Image, ImageDraw

from api.geocoding import reverse_geocode
from api.models import Persona, PersonaPhoto, ProfileImage, User
from api.tagging import auto_tag

SEED_IMAGES_DIR = Path(__file__).parent / "seed_images"

# Phone numbers in the reserved 555-0100–555-0199 range (safe for testing)
TEST_USERS = [
    {
        "phone_number": "+15550100001",
        "display_name": "Alejo Ospina",
        "bio": "🔞 • If you came to stalk me, you owe me your cum • 🖤 NO RTxRT",
        "pronouns": "he/him",
        "age": 37,
        "notifications_enabled": True,
        "intent": 0,  # Browsing
        "looking_for_chat": False,
        "looking_for_cuddle": False,
        "looking_for_fool_around": False,
        "looking_for_sex": False,
        "location_sharing": 0,  # Off
        "share_images": True,
        "share_face": True,
        "share_torso": True,
        "share_explicit": True,
        "share_other": False,
        "can_host": False,
        "can_travel": False,
        "latitude": 39.1098,   # OTR — Washington Park
        "longitude": -84.5155,
        "gallery_colors": ["#EF9A9A", "#FFCDD2"],
        "gallery_image_files": [f"alejo_ospina/{i}.jpg" for i in range(1, 8)],
        "gallery_image_tags": [["face", "torso"], ["face", "torso"], ["torso", "explicit"], ["torso"], ["torso", "explicit"], ["face", "torso"], ["face", "torso"]],
    },
    {
        "phone_number": "+15550100002",
        "display_name": "Andrei Iosivas",
        "bio": "@bengals WR\n@princeton Ftbl & Track\nOahu, Hawaii 🇵🇭🇷🇴\nBusiness Inquiries: Joe@sparx-marketing.com",
        "pronouns": "he/him",
        "age": 27,
        "notifications_enabled": True,
        "intent": 1,  # Looking
        "looking_for_chat": True,
        "looking_for_cuddle": True,
        "looking_for_fool_around": False,
        "looking_for_sex": False,
        "location_sharing": 1,  # General
        "share_images": True,
        "share_face": True,
        "share_torso": True,
        "share_explicit": True,
        "share_other": False,
        "can_host": True,
        "can_travel": False,
        "latitude": 39.1132,   # OTR — Findlay Market
        "longitude": -84.5180,
        "gallery_colors": ["#90CAF9", "#BBDEFB", "#E3F2FD"],
        "gallery_image_files": [f"andrei_iosivas/{i}.jpg" for i in range(1, 6)],
        "gallery_image_tags": [["face"], ["face"], ["face", "torso"], ["face","torso"], ["face"]],
    },
    {
        "phone_number": "+15550100003",
        "display_name": "Ethan O'Pry",
        "bio": "He/him\nLife is nice 🥳\nLink in my Insta Bio!\nIG: @ethanopryreal",
        "pronouns": "they/them",
        "age": 31,
        "notifications_enabled": False,
        "intent": 1,  # Looking
        "looking_for_chat": True,
        "looking_for_cuddle": True,
        "looking_for_fool_around": True,
        "looking_for_sex": True,
        "location_sharing": 2,  # Exact
        "share_images": True,
        "share_face": True,
        "share_torso": True,
        "share_explicit": False,
        "share_other": True,
        "can_host": True,
        "can_travel": True,
        "latitude": 39.1073,   # OTR — Main & 14th
        "longitude": -84.5127,
        "gallery_colors": ["#A5D6A7", "#C8E6C9"],
        "gallery_image_files": [f"ethan_opry/{i}.jpg" for i in range(1, 6)],
        "gallery_image_tags": [["face", "torso", "explicit"], ["face"], ["face","torso"], ["face"], ["face", "torso"]],
    },
    {
        "phone_number": "+15550100004",
        "display_name": "Josh Ivory",
        "bio": " a talented Men Main model represented by Milk Management",
        "pronouns": "she/her",
        "age": 32,
        "notifications_enabled": True,
        "intent": 1,  # Looking
        "looking_for_chat": False,
        "looking_for_cuddle": False,
        "looking_for_fool_around": True,
        "looking_for_sex": True,
        "location_sharing": 1,  # General
        "share_images": True,
        "share_face": True,
        "share_torso": False,
        "share_explicit": True,
        "share_other": False,
        "can_host": False,
        "can_travel": True,
        "latitude": 39.1060,   # OTR — Vine & 13th
        "longitude": -84.5168,
        "gallery_colors": ["#FFCC80", "#FFE0B2", "#FFF3E0"],
        "gallery_image_files": [f"josh_ivory/{i}.jpg" for i in range(1, 6)],
        "gallery_image_tags": [["face", "torso"], ["face"], ["face", "torso"], ["face", "torso"], ["face", "torso"]],
    },
    {
        "phone_number": "+15550100005",
        "display_name": "Kyle Kuznik",
        "bio": "Dxb\nSc kylekuznikk\nkylekuznikk@gmail.com\n@dfyne.official @getbasicsupps code \"kuz\"\n@fitia.app.en\n1 ON 1 FITNESS COACHING ⬇️",
        "pronouns": "he/they",
        "age": 24,
        "notifications_enabled": True,
        "intent": 0,  # Browsing
        "looking_for_chat": False,
        "looking_for_cuddle": False,
        "looking_for_fool_around": False,
        "looking_for_sex": False,
        "location_sharing": 0,  # Off
        "share_images": True,
        "share_face": True,
        "share_torso": False,
        "share_explicit": False,
        "share_other": True,
        "can_host": False,
        "can_travel": True,
        "latitude": 25.2048,   # Dubai (per bio)
        "longitude": 55.2708,
        "gallery_colors": ["#CE93D8"],
        "gallery_image_files": [f"kyle_kuznik/{i}.jpg" for i in range(1, 6)],
        "gallery_image_tags": [["face", "torso"], ["face"], ["face", "torso"], ["face"], ["face", "torso"]],
    },
    {
        "phone_number": "+15550100006",
        "display_name": "Noah Lindsey",
        "bio": "20 | 🇩🇴\n🔌@buckedup \"NJLWOLF4\" for 20% off\nOnline coaching",
        "pronouns": "he/him",
        "age": 41,
        "notifications_enabled": True,
        "intent": 1,  # Looking
        "looking_for_chat": False,
        "looking_for_cuddle": False,
        "looking_for_fool_around": True,
        "looking_for_sex": False,
        "location_sharing": 2,  # Exact
        "share_images": True,
        "share_face": False,
        "share_torso": True,
        "share_explicit": False,
        "share_other": False,
        "can_host": True,
        "can_travel": True,
        "latitude": 39.1103,   # OTR — Liberty & Race
        "longitude": -84.5200,
        "gallery_colors": ["#80CBC4", "#B2DFDB", "#E0F2F1"],
        "gallery_image_files": [f"noah_lindsey/{i}.jpg" for i in range(1, 5)],
        "gallery_image_tags": [["face", "torso"], ["face", "torso"], ["face"], ["torso"]],
    },
    {
        "phone_number": "+15550100007",
        "display_name": "Peachy Boy",
        "bio": "British 🇬🇧",
        "pronouns": "she/her",
        "age": 36,
        "notifications_enabled": False,
        "intent": 1,  # Looking
        "looking_for_chat": True,
        "looking_for_cuddle": True,
        "looking_for_fool_around": False,
        "looking_for_sex": False,
        "location_sharing": 1,  # General
        "share_images": True,
        "share_face": True,
        "share_torso": False,
        "share_explicit": False,
        "share_other": True,
        "can_host": True,
        "can_travel": False,
        "latitude": 51.5014,   # London (British per bio)
        "longitude": -0.1419,
        "gallery_colors": ["#BCAAA4", "#D7CCC8"],
        "gallery_image_files": [f"peachy_boy/{i}.jpg" for i in range(1, 6)],
        "gallery_image_tags": [["face", "torso"], ["face"], ["face"], ["face", "torso", "explicit"], ["face", "explicit"]],
    },
    {
        "phone_number": "+15550100008",
        "display_name": "Reno Gold",
        "bio": "🥇 Gold standard 🏆",
        "pronouns": "xe/xem",
        "age": 36,
        "notifications_enabled": True,
        "intent": 1,  # Looking
        "looking_for_chat": False,
        "looking_for_cuddle": True,
        "looking_for_fool_around": False,
        "looking_for_sex": True,
        "location_sharing": 0,  # Off
        "share_images": True,
        "share_face": False,
        "share_torso": True,
        "share_explicit": True,
        "share_other": False,
        "can_host": False,
        "can_travel": True,
        "latitude": 39.1043,   # OTR — Central Pkwy
        "longitude": -84.5143,
        "gallery_colors": ["#FFAB91", "#FFCCBC"],
        "gallery_image_files": [f"reno_gold/{i}.jpg" for i in range(1, 7)],
        "gallery_image_tags": [["face", "torso"], ["face"], ["face", "torso"], ["face", "torso"], ["face", "torso", "explicit"], ["face", "torso", "explicit"]],
    },
]


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)



def _make_gallery_image(color: str, size: int = 600) -> ContentFile:
    """Generate a solid-color rectangle gallery image."""
    r, g, b = _hex_to_rgb(color)
    img = Image.new("RGB", (size, int(size * 4 / 3)))
    draw = ImageDraw.Draw(img)
    for y in range(img.height):
        shade = tuple(max(0, min(255, c + int((y / img.height - 0.5) * 60))) for c in (r, g, b))
        draw.line([(0, y), (size, y)], fill=shade)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return ContentFile(buf.getvalue(), name="gallery.jpg")


def _load_image_file(filename: str) -> ContentFile | None:
    """Load an image from seed_images/. Returns None if the file doesn't exist."""
    path = SEED_IMAGES_DIR / filename
    if not path.exists():
        return None
    return ContentFile(path.read_bytes(), name=filename)


TEST_PHONES = {u["phone_number"] for u in TEST_USERS}


class Command(BaseCommand):
    help = "Seed test users with full profile data, or clear them with --clear."

    def add_arguments(self, parser):
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete all test users and their uploaded images.",
        )
        parser.add_argument(
            "--tags-only",
            action="store_true",
            help="Re-apply gallery_image_tags from seed data without re-uploading images.",
        )

    def handle(self, *args, **options):
        if options["clear"]:
            self._clear()
        elif options["tags_only"]:
            self._retag()
        else:
            self._seed()

    # ── Seed ──────────────────────────────────────────────────────────────────

    def _seed(self):
        self.stdout.write("Seeding test users…\n")
        for data in TEST_USERS:
            gallery_colors = data.pop("gallery_colors")
            gallery_image_files = data.pop("gallery_image_files")
            gallery_image_tags = data.pop("gallery_image_tags", [])

            user, created = User.objects.get_or_create(phone_number=data["phone_number"])
            user.set_unusable_password()

            # Pre-persona seed data: share_* toggles decide which images are public.
            share = {t: data.get(f"share_{t}", False) for t in ("face", "torso", "explicit", "other")}
            share_images = data.get("share_images", False)

            for field in ("notifications_enabled", "latitude", "longitude"):
                setattr(user, field, data[field])
            for field in ("pronouns", "height", "length"):
                if field in data:
                    setattr(user, field, data[field])
            user.save()

            persona = user.get_active_persona()
            for field in Persona.PROFILE_FIELDS:
                if field in data:
                    setattr(persona, field, data[field])
            persona.save()
            persona.photos.all().delete()

            if user.latitude is not None and user.longitude is not None:
                neighborhood = reverse_geocode(user.latitude, user.longitude)
                User.objects.filter(pk=user.pk).update(
                    location_updated_at=timezone.now(),
                    neighborhood=neighborhood,
                )
                self.stdout.write(f"    ↳ {neighborhood or '(no neighborhood)'}")

            # Replace gallery images — prefer files, fall back to generated color placeholders
            for img in user.gallery_images.all():
                img.image.delete(save=False)
                img.delete()

            gallery_contents: list[tuple[str, ContentFile]] = []
            for filename in gallery_image_files:
                cf = _load_image_file(filename)
                if cf is None:
                    self.stdout.write(f"    ⚠ gallery file '{filename}' not found, skipping")
                    continue
                gallery_contents.append((filename, cf))
            for i in range(len(gallery_contents), len(gallery_colors)):
                gallery_contents.append((f"gallery_{user.phone_number}_{i}.jpg", _make_gallery_image(gallery_colors[i])))

            for i, (fname, content) in enumerate(gallery_contents):
                manual = gallery_image_tags[i] if i < len(gallery_image_tags) else []

                # Try Rekognition; fall back to manually-specified tags.
                image_path = SEED_IMAGES_DIR / fname
                reko = auto_tag(image_path.read_bytes()) if image_path.exists() else {}

                if reko:
                    tag_kw = reko
                    source = 'rekognition'
                else:
                    tag_kw = {
                        'tag_face':     'face' in manual,
                        'tag_torso':    'torso' in manual,
                        'tag_explicit': 'explicit' in manual,
                        'tag_other':    'other' in manual,
                    }
                    source = 'manual'

                obj = ProfileImage(user=user, order=i, **tag_kw)
                obj.image.save(fname, content, save=False)
                obj.save()

                img_tags = [t for t in share if tag_kw.get(f'tag_{t}')]
                if share_images and all(share[t] for t in img_tags) and persona.photos.count() < 6:
                    PersonaPhoto.objects.create(persona=persona, image=obj, order=persona.photos.count())

                active = [t for t in ('face', 'torso', 'explicit', 'other') if tag_kw.get(f'tag_{t}')]
                self.stdout.write(f"      [{source}] {fname}: {', '.join(active) or 'other'}")

            status = "created" if created else "updated"
            self.stdout.write(f"  {persona.display_name} ({user.phone_number}) — {status}")

        self.stdout.write(self.style.SUCCESS(f"\n✓ {len(TEST_USERS)} test users ready."))

    # ── Retag ─────────────────────────────────────────────────────────────────

    def _retag(self):
        self.stdout.write("Re-tagging seeded images…\n")
        updated = 0
        for data in TEST_USERS:
            phone = data["phone_number"]
            tags_by_order = data.get("gallery_image_tags", [])
            try:
                user = User.objects.get(phone_number=phone)
            except User.DoesNotExist:
                self.stdout.write(f"  ⚠ {phone} not found — run seed first")
                continue

            for order, manual in enumerate(tags_by_order):
                try:
                    img = user.gallery_images.get(order=order)
                except ProfileImage.DoesNotExist:
                    self.stdout.write(f"    ⚠ {phone} image order={order} not found")
                    continue

                img.tag_face     = 'face'     in manual
                img.tag_torso    = 'torso'    in manual
                img.tag_explicit = 'explicit' in manual
                img.tag_other    = 'other'    in manual
                img.save(update_fields=['tag_face', 'tag_torso', 'tag_explicit', 'tag_other'])

                active = [t for t in ('face', 'torso', 'explicit', 'other') if getattr(img, f'tag_{t}')]
                self.stdout.write(f"      {phone} [{order}]: {', '.join(active) or '(none)'}")
                updated += 1

        self.stdout.write(self.style.SUCCESS(f"\n✓ {updated} images re-tagged."))

    # ── Clear ─────────────────────────────────────────────────────────────────

    def _clear(self):
        users = User.objects.filter(phone_number__in=TEST_PHONES)
        count = users.count()
        if count == 0:
            self.stdout.write("No test users found.")
            return

        for user in users:
            for img in user.gallery_images.all():
                img.image.delete(save=False)
                img.delete()
            user.delete()
            self.stdout.write(f"  Deleted {user.phone_number}")

        self.stdout.write(self.style.SUCCESS(f"\n✓ Removed {count} test users."))
