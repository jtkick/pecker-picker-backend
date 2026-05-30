from django.core.management.base import BaseCommand
from api.models import Interest

# (name, parent_name or None)
INTERESTS = [
    # ── Arts & Creativity ─────────────────────────────────────────────────────
    ("Arts & Creativity",           None),
    ("Painting",                    "Arts & Creativity"),
    ("Drawing",                     "Arts & Creativity"),
    ("Illustration",                "Arts & Creativity"),
    ("Photography",                 "Arts & Creativity"),
    ("Filmmaking",                  "Arts & Creativity"),
    ("Writing",                     "Arts & Creativity"),
    ("Poetry",                      "Arts & Creativity"),
    ("Blogging",                    "Arts & Creativity"),
    ("Music",                       "Arts & Creativity"),
    ("Playing guitar",              "Arts & Creativity"),
    ("Playing piano",               "Arts & Creativity"),
    ("Playing drums",               "Arts & Creativity"),
    ("Singing",                     "Arts & Creativity"),
    ("DJing",                       "Arts & Creativity"),
    ("Music production",            "Arts & Creativity"),
    ("Vinyl collecting",            "Arts & Creativity"),
    ("Acting",                      "Arts & Creativity"),
    ("Theater",                     "Arts & Creativity"),
    ("Improv comedy",               "Arts & Creativity"),
    ("Pottery",                     "Arts & Creativity"),
    ("Sculpture",                   "Arts & Creativity"),
    ("Woodworking",                 "Arts & Creativity"),
    ("Graphic design",              "Arts & Creativity"),
    ("Animation",                   "Arts & Creativity"),
    ("Knitting",                    "Arts & Creativity"),
    ("Crocheting",                  "Arts & Creativity"),
    ("Embroidery",                  "Arts & Creativity"),
    ("Jewelry making",              "Arts & Creativity"),
    ("Calligraphy",                 "Arts & Creativity"),
    ("Tattoo art",                  "Arts & Creativity"),
    ("Printmaking",                 "Arts & Creativity"),
    ("Street art",                  "Arts & Creativity"),
    ("Digital art",                 "Arts & Creativity"),

    # ── Food & Drink ─────────────────────────────────────────────────────────
    ("Food & Drink",                None),
    ("Cooking",                     "Food & Drink"),
    ("Baking",                      "Food & Drink"),
    ("BBQ & grilling",              "Food & Drink"),
    ("Meal prep",                   "Food & Drink"),
    ("Wine tasting",                "Food & Drink"),
    ("Craft beer",                  "Food & Drink"),
    ("Cocktail making",             "Food & Drink"),
    ("Coffee",                      "Food & Drink"),
    ("Tea",                         "Food & Drink"),
    ("Farmers markets",             "Food & Drink"),
    ("Fine dining",                 "Food & Drink"),
    ("Street food",                 "Food & Drink"),
    ("Food blogging",               "Food & Drink"),
    ("Cheese making",               "Food & Drink"),
    ("Fermentation",                "Food & Drink"),
    ("Sushi",                       "Food & Drink"),
    ("Vegan cooking",               "Food & Drink"),
    ("Home brewing",                "Food & Drink"),

    # ── Fitness & Sports ─────────────────────────────────────────────────────
    ("Fitness & Sports",            None),
    ("Gym / weightlifting",         "Fitness & Sports"),
    ("Running",                     "Fitness & Sports"),
    ("Cycling",                     "Fitness & Sports"),
    ("Swimming",                    "Fitness & Sports"),
    ("Yoga",                        "Fitness & Sports"),
    ("Pilates",                     "Fitness & Sports"),
    ("Boxing",                      "Fitness & Sports"),
    ("Martial arts",                "Fitness & Sports"),
    ("Rock climbing",               "Fitness & Sports"),
    ("CrossFit",                    "Fitness & Sports"),
    ("Tennis",                      "Fitness & Sports"),
    ("Soccer",                      "Fitness & Sports"),
    ("Basketball",                  "Fitness & Sports"),
    ("Baseball",                    "Fitness & Sports"),
    ("Volleyball",                  "Fitness & Sports"),
    ("Rugby",                       "Fitness & Sports"),
    ("Wrestling",                   "Fitness & Sports"),
    ("Skateboarding",               "Fitness & Sports"),
    ("Surfing",                     "Fitness & Sports"),
    ("Skiing",                      "Fitness & Sports"),
    ("Snowboarding",                "Fitness & Sports"),
    ("Golf",                        "Fitness & Sports"),
    ("Archery",                     "Fitness & Sports"),
    ("Rowing",                      "Fitness & Sports"),
    ("Parkour",                     "Fitness & Sports"),
    ("Pole fitness",                "Fitness & Sports"),
    ("Dance",                       "Fitness & Sports"),
    ("Cheerleading",                "Fitness & Sports"),
    ("Ice skating",                 "Fitness & Sports"),
    ("Triathlon",                   "Fitness & Sports"),
    ("Powerlifting",                "Fitness & Sports"),

    # ── Outdoors & Nature ────────────────────────────────────────────────────
    ("Outdoors & Nature",           None),
    ("Hiking",                      "Outdoors & Nature"),
    ("Camping",                     "Outdoors & Nature"),
    ("Backpacking",                 "Outdoors & Nature"),
    ("Fishing",                     "Outdoors & Nature"),
    ("Hunting",                     "Outdoors & Nature"),
    ("Birdwatching",                "Outdoors & Nature"),
    ("Gardening",                   "Outdoors & Nature"),
    ("Foraging",                    "Outdoors & Nature"),
    ("Stargazing",                  "Outdoors & Nature"),
    ("Kayaking",                    "Outdoors & Nature"),
    ("Canoeing",                    "Outdoors & Nature"),
    ("Scuba diving",                "Outdoors & Nature"),
    ("Snorkeling",                  "Outdoors & Nature"),
    ("Mountain biking",             "Outdoors & Nature"),
    ("Trail running",               "Outdoors & Nature"),
    ("National parks",              "Outdoors & Nature"),
    ("Beach",                       "Outdoors & Nature"),
    ("Wildflower hunting",          "Outdoors & Nature"),

    # ── Travel & Adventure ───────────────────────────────────────────────────
    ("Travel & Adventure",          None),
    ("Road trips",                  "Travel & Adventure"),
    ("International travel",        "Travel & Adventure"),
    ("Solo travel",                 "Travel & Adventure"),
    ("Backpacking travel",          "Travel & Adventure"),
    ("Luxury travel",               "Travel & Adventure"),
    ("Cruises",                     "Travel & Adventure"),
    ("Glamping",                    "Travel & Adventure"),
    ("Van life",                    "Travel & Adventure"),
    ("City breaks",                 "Travel & Adventure"),
    ("Festival travel",             "Travel & Adventure"),
    ("Adventure travel",            "Travel & Adventure"),
    ("Voluntourism",                "Travel & Adventure"),
    ("Cultural immersion",          "Travel & Adventure"),

    # ── Technology & Gaming ──────────────────────────────────────────────────
    ("Technology & Gaming",         None),
    ("Video games",                 "Technology & Gaming"),
    ("PC gaming",                   "Technology & Gaming"),
    ("Console gaming",              "Technology & Gaming"),
    ("Mobile gaming",               "Technology & Gaming"),
    ("VR / AR",                     "Technology & Gaming"),
    ("Coding / programming",        "Technology & Gaming"),
    ("Tech gadgets",                "Technology & Gaming"),
    ("Esports",                     "Technology & Gaming"),
    ("Tabletop RPGs",               "Technology & Gaming"),
    ("Board games",                 "Technology & Gaming"),
    ("Card games",                  "Technology & Gaming"),
    ("Retro gaming",                "Technology & Gaming"),
    ("3D printing",                 "Technology & Gaming"),
    ("AI & machine learning",       "Technology & Gaming"),
    ("Robotics",                    "Technology & Gaming"),
    ("CTF / hacking",               "Technology & Gaming"),
    ("Crypto & Web3",               "Technology & Gaming"),
    ("Dungeons & Dragons",          "Technology & Gaming"),

    # ── Books & Learning ─────────────────────────────────────────────────────
    ("Books & Learning",            None),
    ("Reading",                     "Books & Learning"),
    ("Sci-fi",                      "Books & Learning"),
    ("Fantasy",                     "Books & Learning"),
    ("Mystery",                     "Books & Learning"),
    ("Non-fiction",                 "Books & Learning"),
    ("Self-help",                   "Books & Learning"),
    ("History",                     "Books & Learning"),
    ("Philosophy",                  "Books & Learning"),
    ("Psychology",                  "Books & Learning"),
    ("Language learning",           "Books & Learning"),
    ("Online courses",              "Books & Learning"),
    ("Podcasts",                    "Books & Learning"),
    ("Audiobooks",                  "Books & Learning"),
    ("Comics",                      "Books & Learning"),
    ("Manga",                       "Books & Learning"),
    ("Graphic novels",              "Books & Learning"),
    ("True crime",                  "Books & Learning"),

    # ── Music & Entertainment ────────────────────────────────────────────────
    ("Music & Entertainment",       None),
    ("Concerts",                    "Music & Entertainment"),
    ("Music festivals",             "Music & Entertainment"),
    ("Movies",                      "Music & Entertainment"),
    ("TV series",                   "Music & Entertainment"),
    ("Stand-up comedy",             "Music & Entertainment"),
    ("Opera",                       "Music & Entertainment"),
    ("Classical music",             "Music & Entertainment"),
    ("Jazz",                        "Music & Entertainment"),
    ("Electronic music",            "Music & Entertainment"),
    ("Hip-hop",                     "Music & Entertainment"),
    ("Karaoke",                     "Music & Entertainment"),
    ("Anime",                       "Music & Entertainment"),
    ("Documentaries",               "Music & Entertainment"),
    ("Escape rooms",                "Music & Entertainment"),

    # ── Health & Wellness ────────────────────────────────────────────────────
    ("Health & Wellness",           None),
    ("Meditation",                  "Health & Wellness"),
    ("Mindfulness",                 "Health & Wellness"),
    ("Journaling",                  "Health & Wellness"),
    ("Therapy",                     "Health & Wellness"),
    ("Nutrition",                   "Health & Wellness"),
    ("Herbalism",                   "Health & Wellness"),
    ("Breathwork",                  "Health & Wellness"),
    ("Cold plunging",               "Health & Wellness"),
    ("Sauna",                       "Health & Wellness"),
    ("Spa",                         "Health & Wellness"),
    ("Intermittent fasting",        "Health & Wellness"),
    ("Sleep optimization",          "Health & Wellness"),
    ("Sobriety",                    "Health & Wellness"),

    # ── Community & Social ───────────────────────────────────────────────────
    ("Community & Social",          None),
    ("Volunteering",                "Community & Social"),
    ("Activism",                    "Community & Social"),
    ("LGBTQ+ community",            "Community & Social"),
    ("Charity work",                "Community & Social"),
    ("Mentoring",                   "Community & Social"),
    ("Book clubs",                  "Community & Social"),
    ("Meetups",                     "Community & Social"),
    ("Networking",                  "Community & Social"),
    ("Environmental activism",      "Community & Social"),
    ("Pride events",                "Community & Social"),
    ("Community organizing",        "Community & Social"),

    # ── Pets & Animals ───────────────────────────────────────────────────────
    ("Pets & Animals",              None),
    ("Dogs",                        "Pets & Animals"),
    ("Cats",                        "Pets & Animals"),
    ("Birds",                       "Pets & Animals"),
    ("Reptiles",                    "Pets & Animals"),
    ("Fish",                        "Pets & Animals"),
    ("Horse riding",                "Pets & Animals"),
    ("Dog training",                "Pets & Animals"),
    ("Pet fostering",               "Pets & Animals"),
    ("Wildlife conservation",       "Pets & Animals"),

    # ── Fashion & Style ──────────────────────────────────────────────────────
    ("Fashion & Style",             None),
    ("Street fashion",              "Fashion & Style"),
    ("Vintage clothing",            "Fashion & Style"),
    ("Thrifting",                   "Fashion & Style"),
    ("Luxury fashion",              "Fashion & Style"),
    ("Sneaker culture",             "Fashion & Style"),
    ("Cosplay",                     "Fashion & Style"),
    ("Drag",                        "Fashion & Style"),
    ("Watches",                     "Fashion & Style"),
    ("Skincare",                    "Fashion & Style"),
    ("Grooming",                    "Fashion & Style"),
    ("Accessories",                 "Fashion & Style"),

    # ── Home & Lifestyle ─────────────────────────────────────────────────────
    ("Home & Lifestyle",            None),
    ("Interior design",             "Home & Lifestyle"),
    ("Home improvement",            "Home & Lifestyle"),
    ("Minimalism",                  "Home & Lifestyle"),
    ("Sustainable living",          "Home & Lifestyle"),
    ("Zero-waste",                  "Home & Lifestyle"),
    ("Urban farming",               "Home & Lifestyle"),
    ("Smart home",                  "Home & Lifestyle"),
    ("Antiques",                    "Home & Lifestyle"),
    ("Collecting",                  "Home & Lifestyle"),
    ("Candle making",               "Home & Lifestyle"),
    ("Aromatherapy",                "Home & Lifestyle"),

    # ── Spirituality ─────────────────────────────────────────────────────────
    ("Spirituality",                None),
    ("Astrology",                   "Spirituality"),
    ("Tarot",                       "Spirituality"),
    ("Paganism",                    "Spirituality"),
    ("Buddhism",                    "Spirituality"),
    ("Stoicism",                    "Spirituality"),
    ("Mysticism",                   "Spirituality"),
    ("Numerology",                  "Spirituality"),
    ("Crystal healing",             "Spirituality"),
    ("Human design",                "Spirituality"),
    ("Reiki",                       "Spirituality"),

    # ── Cars & Vehicles ──────────────────────────────────────────────────────
    ("Cars & Vehicles",             None),
    ("Cars",                        "Cars & Vehicles"),
    ("Motorcycles",                 "Cars & Vehicles"),
    ("Off-roading",                 "Cars & Vehicles"),
    ("Car customization",           "Cars & Vehicles"),
    ("Racing",                      "Cars & Vehicles"),
    ("Classic cars",                "Cars & Vehicles"),
    ("Electric vehicles",           "Cars & Vehicles"),
    ("Cycling advocacy",            "Cars & Vehicles"),
]


class Command(BaseCommand):
    help = "Seed the Interest table with a comprehensive list of hobbies and interests."

    def add_arguments(self, parser):
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete all existing interests before seeding.",
        )

    def handle(self, *args, **options):
        if options["clear"]:
            count = Interest.objects.count()
            Interest.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {count} existing interests."))

        created_count = 0
        for name, _ in INTERESTS:
            _, created = Interest.objects.get_or_create(name=name)
            if created:
                created_count += 1

        for name, parent_name in INTERESTS:
            if parent_name is None:
                continue
            try:
                interest = Interest.objects.get(name=name)
                parent = Interest.objects.get(name=parent_name)
                if interest.parent_id != parent.pk:
                    interest.parent = parent
                    interest.save(update_fields=["parent"])
            except Interest.DoesNotExist as e:
                self.stdout.write(self.style.ERROR(f"Missing interest: {e}"))

        total = Interest.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f"Done. Created {created_count} new interests. {total} total in database."
        ))
