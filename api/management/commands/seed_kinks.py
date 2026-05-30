from django.core.management.base import BaseCommand
from api.models import Kink

# (name, parent_name or None)
# Top-level entries have parent=None and act as category headers.
KINKS = [
    # ── Bondage & Restraint ───────────────────────────────────────────────────
    ("Bondage & Restraint",         None),
    ("Rope bondage",                "Bondage & Restraint"),
    ("Shibari / Kinbaku",           "Bondage & Restraint"),
    ("Suspension bondage",          "Bondage & Restraint"),
    ("Hogtie",                      "Bondage & Restraint"),
    ("Handcuffs",                   "Bondage & Restraint"),
    ("Chains",                      "Bondage & Restraint"),
    ("Tape bondage",                "Bondage & Restraint"),
    ("Mummification",               "Bondage & Restraint"),
    ("Straitjacket",                "Bondage & Restraint"),
    ("Cage play",                   "Bondage & Restraint"),
    ("Spreader bars",               "Bondage & Restraint"),
    ("Collar & leash",              "Bondage & Restraint"),
    ("Sleepsack",                   "Bondage & Restraint"),
    ("Vacuum bed",                  "Bondage & Restraint"),
    ("Stocks / pillory",            "Bondage & Restraint"),

    # ── Impact Play ───────────────────────────────────────────────────────────
    ("Impact Play",                 None),
    ("Spanking",                    "Impact Play"),
    ("Paddling",                    "Impact Play"),
    ("Flogging",                    "Impact Play"),
    ("Whipping",                    "Impact Play"),
    ("Caning",                      "Impact Play"),
    ("Cropping",                    "Impact Play"),
    ("Slapping",                    "Impact Play"),
    ("Belt",                        "Impact Play"),

    # ── Power Exchange ────────────────────────────────────────────────────────
    ("Power Exchange",              None),
    ("Dominance",                   "Power Exchange"),
    ("Submission",                  "Power Exchange"),
    ("Total power exchange (TPE)",  "Power Exchange"),
    ("Service submission",          "Power Exchange"),
    ("Human furniture",             "Power Exchange"),
    ("Humiliation",                 "Power Exchange"),
    ("Degradation",                 "Power Exchange"),
    ("Verbal abuse / dirty talk",   "Power Exchange"),
    ("Worship & adoration",         "Power Exchange"),
    ("Ownership / collaring",       "Power Exchange"),
    ("Financial domination",        "Power Exchange"),
    ("Consensual non-consent (CNC)","Power Exchange"),
    ("Objectification",             "Power Exchange"),

    # ── Role Play & Fantasy ───────────────────────────────────────────────────
    ("Role Play & Fantasy",         None),
    ("Daddy / boy",                 "Role Play & Fantasy"),
    ("Sir / boy",                   "Role Play & Fantasy"),
    ("Master / slave",              "Role Play & Fantasy"),
    ("Doctor / patient",            "Role Play & Fantasy"),
    ("Police / prisoner",           "Role Play & Fantasy"),
    ("Military / soldier",          "Role Play & Fantasy"),
    ("Teacher / student",           "Role Play & Fantasy"),
    ("Coach / athlete",             "Role Play & Fantasy"),
    ("Stranger / anonymous",        "Role Play & Fantasy"),
    ("Hypnosis & mind control",     "Role Play & Fantasy"),
    ("Alpha / beta",                "Role Play & Fantasy"),
    ("Kidnapping / abduction",      "Role Play & Fantasy"),
    ("Superhero / villain",         "Role Play & Fantasy"),
    ("Fantasy / sci-fi scenarios",  "Role Play & Fantasy"),

    # ── Pup & Pet Play ────────────────────────────────────────────────────────
    ("Pup & Pet Play",              None),
    ("Pup play",                    "Pup & Pet Play"),
    ("Handler / pup dynamic",       "Pup & Pet Play"),
    ("Puppy gear (hood, mitts)",    "Pup & Pet Play"),
    ("Pet play (other animals)",    "Pup & Pet Play"),
    ("Furry",                       "Pup & Pet Play"),

    # ── Leather & Gear ────────────────────────────────────────────────────────
    ("Leather & Gear",              None),
    ("Leather",                     "Leather & Gear"),
    ("Rubber / latex",              "Leather & Gear"),
    ("Neoprene / wetsuit",          "Leather & Gear"),
    ("Spandex / lycra",             "Leather & Gear"),
    ("Jockstraps",                  "Leather & Gear"),
    ("Singlets / wrestling gear",   "Leather & Gear"),
    ("Boots",                       "Leather & Gear"),
    ("Uniforms",                    "Leather & Gear"),
    ("Harnesses",                   "Leather & Gear"),
    ("Hoods & masks",               "Leather & Gear"),
    ("Gas masks",                   "Leather & Gear"),
    ("Waders / overalls",           "Leather & Gear"),
    ("Sportswear / gym gear",       "Leather & Gear"),
    ("Underwear fetish",            "Leather & Gear"),
    ("Suits & formal wear",         "Leather & Gear"),

    # ── Chastity & Orgasm Control ─────────────────────────────────────────────
    ("Chastity & Orgasm Control",   None),
    ("Chastity devices",            "Chastity & Orgasm Control"),
    ("Orgasm denial",               "Chastity & Orgasm Control"),
    ("Edging",                      "Chastity & Orgasm Control"),
    ("Forced orgasm",               "Chastity & Orgasm Control"),
    ("Ruined orgasm",               "Chastity & Orgasm Control"),
    ("Long-term chastity",          "Chastity & Orgasm Control"),

    # ── Cock & Ball Torture (CBT) ─────────────────────────────────────────────
    ("Cock & Ball Torture (CBT)",   None),
    ("Ball busting",                "Cock & Ball Torture (CBT)"),
    ("Cock torture",                "Cock & Ball Torture (CBT)"),
    ("Sounding / urethral play",    "Cock & Ball Torture (CBT)"),
    ("Ball stretching",             "Cock & Ball Torture (CBT)"),
    ("Cock & ball bondage",         "Cock & Ball Torture (CBT)"),
    ("Parachute / weights",         "Cock & Ball Torture (CBT)"),
    ("Cock rings",                  "Cock & Ball Torture (CBT)"),
    ("Milking",                     "Cock & Ball Torture (CBT)"),

    # ── Body Worship ──────────────────────────────────────────────────────────
    ("Body Worship",                None),
    ("Foot worship",                "Body Worship"),
    ("Boot worship",                "Body Worship"),
    ("Muscle worship",              "Body Worship"),
    ("Armpit worship",              "Body Worship"),
    ("Ass worship",                 "Body Worship"),
    ("Cock worship",                "Body Worship"),
    ("Nipple worship",              "Body Worship"),

    # ── Body Part Fetishes ────────────────────────────────────────────────────
    ("Body Part Fetishes",          None),
    ("Feet",                        "Body Part Fetishes"),
    ("Hands",                       "Body Part Fetishes"),
    ("Armpits",                     "Body Part Fetishes"),
    ("Ass / glutes",                "Body Part Fetishes"),
    ("Chest hair",                  "Body Part Fetishes"),
    ("Facial hair / beards",        "Body Part Fetishes"),
    ("Muscles",                     "Body Part Fetishes"),
    ("Foreskin",                    "Body Part Fetishes"),
    ("Nipples",                     "Body Part Fetishes"),
    ("Balls / scrotum",             "Body Part Fetishes"),
    ("Big cock",                    "Body Part Fetishes"),
    ("Neck",                        "Body Part Fetishes"),
    ("Bulge",                       "Body Part Fetishes"),

    # ── Sensory Play ──────────────────────────────────────────────────────────
    ("Sensory Play",                None),
    ("Blindfolding",                "Sensory Play"),
    ("Sensory deprivation",         "Sensory Play"),
    ("Wax play",                    "Sensory Play"),
    ("Ice play",                    "Sensory Play"),
    ("Electrostimulation (e-stim)", "Sensory Play"),
    ("Massage",                     "Sensory Play"),
    ("Tickling",                    "Sensory Play"),
    ("Temperature play",            "Sensory Play"),
    ("Feathers / soft sensation",   "Sensory Play"),

    # ── Edge Play ─────────────────────────────────────────────────────────────
    ("Edge Play",                   None),
    ("Knife / blade play",          "Edge Play"),
    ("Breath play",                 "Edge Play"),
    ("Choking",                     "Edge Play"),
    ("Fire play",                   "Edge Play"),
    ("Needle play",                 "Edge Play"),

    # ── Oral Sex ──────────────────────────────────────────────────────────────
    ("Oral Sex",                    None),
    ("Blowjobs",                    "Oral Sex"),
    ("Deepthroat",                  "Oral Sex"),
    ("Face fucking",                "Oral Sex"),
    ("Rimming",                     "Oral Sex"),
    ("69",                          "Oral Sex"),
    ("Spit",                        "Oral Sex"),
    ("Gagging",                     "Oral Sex"),

    # ── Anal Play ─────────────────────────────────────────────────────────────
    ("Anal Play",                   None),
    ("Fucking / intercourse",       "Anal Play"),
    ("Breeding / bareback",         "Anal Play"),
    ("Double penetration",          "Anal Play"),
    ("Felching",                    "Anal Play"),
    ("Prostate massage",            "Anal Play"),
    ("Anal training",               "Anal Play"),

    # ── Fisting ───────────────────────────────────────────────────────────────
    ("Fisting",                     None),
    ("Hand balling",                "Fisting"),
    ("Punch fisting",               "Fisting"),
    ("Arm fisting",                 "Fisting"),
    ("Double fisting",              "Fisting"),
    ("Fisting with toys",           "Fisting"),

    # ── Fluids & Bodily Play ──────────────────────────────────────────────────
    ("Fluids & Bodily Play",        None),
    ("Watersports / piss play",     "Fluids & Bodily Play"),
    ("Scat",                        "Fluids & Bodily Play"),
    ("Cum play",                    "Fluids & Bodily Play"),
    ("Cum eating",                  "Fluids & Bodily Play"),
    ("Snowballing",                 "Fluids & Bodily Play"),
    ("Spitting",                    "Fluids & Bodily Play"),
    ("Sweat / body musk",           "Fluids & Bodily Play"),
    ("Enemas / cleansing play",     "Fluids & Bodily Play"),
    ("Poppers",                     "Fluids & Bodily Play"),

    # ── Toys & Devices ────────────────────────────────────────────────────────
    ("Toys & Devices",              None),
    ("Dildos",                      "Toys & Devices"),
    ("Butt plugs",                  "Toys & Devices"),
    ("Vibrators",                   "Toys & Devices"),
    ("Prostate massagers",          "Toys & Devices"),
    ("Sex machines / fucking machines", "Toys & Devices"),
    ("Electrostimulation toys",     "Toys & Devices"),
    ("Sounding rods",               "Toys & Devices"),
    ("Strap-ons",                   "Toys & Devices"),
    ("Plugs & tunnels (stretching)","Toys & Devices"),
    ("Fucking machines",            "Toys & Devices"),

    # ── Exhibitionism & Voyeurism ─────────────────────────────────────────────
    ("Exhibitionism & Voyeurism",   None),
    ("Exhibitionism",               "Exhibitionism & Voyeurism"),
    ("Voyeurism",                   "Exhibitionism & Voyeurism"),
    ("Cruising",                    "Exhibitionism & Voyeurism"),
    ("Public sex",                  "Exhibitionism & Voyeurism"),
    ("Semi-public spaces",          "Exhibitionism & Voyeurism"),
    ("Nude beaches / resorts",      "Exhibitionism & Voyeurism"),
    ("Locker rooms",                "Exhibitionism & Voyeurism"),
    ("Bathhouses / saunas",         "Exhibitionism & Voyeurism"),

    # ── Group Sex ─────────────────────────────────────────────────────────────
    ("Group Sex",                   None),
    ("Threesomes",                  "Group Sex"),
    ("Orgies",                      "Group Sex"),
    ("Gangbang",                    "Group Sex"),
    ("Glory holes",                 "Group Sex"),
    ("Sex parties / play parties",  "Group Sex"),
    ("Daisy chaining",              "Group Sex"),
    ("Bukakke",                     "Group Sex"),
    ("Cum dump / cumdump",          "Group Sex"),
    ("Pig parties",                 "Group Sex"),

    # ── Wrestling & Combat ────────────────────────────────────────────────────
    ("Wrestling & Combat",          None),
    ("Erotic wrestling",            "Wrestling & Combat"),
    ("Gut punching",                "Wrestling & Combat"),
    ("Headscissors",                "Wrestling & Combat"),
    ("Submission wrestling",        "Wrestling & Combat"),
    ("Boxing / fighting",           "Wrestling & Combat"),

    # ── Smells & Scents ───────────────────────────────────────────────────────
    ("Smells & Scents",             None),
    ("Body odor / musk",            "Smells & Scents"),
    ("Leather smell",               "Smells & Scents"),
    ("Rubber smell",                "Smells & Scents"),
    ("Feet smell",                  "Smells & Scents"),
    ("Sweat smell",                 "Smells & Scents"),

    # ── Photography & Media ───────────────────────────────────────────────────
    ("Photography & Media",         None),
    ("Filming / being filmed",      "Photography & Media"),
    ("Amateur porn",                "Photography & Media"),
    ("Sexting / photo sharing",     "Photography & Media"),
    ("Watching porn together",      "Photography & Media"),
    ("Live camming",                "Photography & Media"),

    # ── Outdoor & Nature ──────────────────────────────────────────────────────
    ("Outdoor & Nature",            None),
    ("Outdoor sex",                 "Outdoor & Nature"),
    ("Skinny dipping",              "Outdoor & Nature"),
    ("Camping sex",                 "Outdoor & Nature"),
    ("Beach sex",                   "Outdoor & Nature"),
    ("Car sex",                     "Outdoor & Nature"),

    # ── Physical Types ────────────────────────────────────────────────────────
    ("Physical Types",              None),
    ("Bears",                       "Physical Types"),
    ("Cubs",                        "Physical Types"),
    ("Otters",                      "Physical Types"),
    ("Muscle men",                  "Physical Types"),
    ("Chubs / chubbies",            "Physical Types"),
    ("Twinks",                      "Physical Types"),
    ("Daddies",                     "Physical Types"),
    ("Silver foxes / silver daddies","Physical Types"),
    ("Jocks",                       "Physical Types"),
    ("Hairy men",                   "Physical Types"),
    ("Smooth men",                  "Physical Types"),
    ("Hung men",                    "Physical Types"),

    # ── Subcultures & Communities ─────────────────────────────────────────────
    ("Subcultures & Communities",   None),
    ("Leather culture",             "Subcultures & Communities"),
    ("Bear culture",                "Subcultures & Communities"),
    ("Skinhead culture",            "Subcultures & Communities"),
    ("Circuit / party scene",       "Subcultures & Communities"),
    ("Kink events & conferences",   "Subcultures & Communities"),
    ("Fetish clubs",                "Subcultures & Communities"),

    # ── Kink Lifestyle ────────────────────────────────────────────────────────
    ("Kink Lifestyle",              None),
    ("24/7 dynamic",                "Kink Lifestyle"),
    ("Poly / open relationship kink","Kink Lifestyle"),
    ("Mentorship / training",       "Kink Lifestyle"),
    ("Protocol & ritual",           "Kink Lifestyle"),
    ("Kink education & workshops",  "Kink Lifestyle"),
    ("Online / long-distance kink", "Kink Lifestyle"),
]


class Command(BaseCommand):
    help = "Seed the Kink table with a comprehensive list of kinks for gay men."

    def add_arguments(self, parser):
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete all existing kinks before seeding.",
        )

    def handle(self, *args, **options):
        if options["clear"]:
            count = Kink.objects.count()
            Kink.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {count} existing kinks."))

        # First pass: create all kinks without parents so we can reference them.
        created_count = 0
        for name, _ in KINKS:
            _, created = Kink.objects.get_or_create(name=name)
            if created:
                created_count += 1

        # Second pass: assign parents.
        for name, parent_name in KINKS:
            if parent_name is None:
                continue
            try:
                kink = Kink.objects.get(name=name)
                parent = Kink.objects.get(name=parent_name)
                if kink.parent_id != parent.pk:
                    kink.parent = parent
                    kink.save(update_fields=["parent"])
            except Kink.DoesNotExist as e:
                self.stdout.write(self.style.ERROR(f"Missing kink: {e}"))

        total = Kink.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f"Done. Created {created_count} new kinks. {total} total in database."
        ))
