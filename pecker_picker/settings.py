from pathlib import Path
import base64
import os
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.environ.get("DEBUG", "True") == "True"

# The fallback key is for local dev only; production must set its own.
SECRET_KEY = os.environ.get("SECRET_KEY") or (
    "django-insecure-t57qsp7fkvo(504hpmgq!cq3q$x7l_tvxybvun15-ah2_fl$tj" if DEBUG else ""
)
if not SECRET_KEY:
    raise ImproperlyConfigured("SECRET_KEY must be set when DEBUG is False")

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,192.168.1.12,triangulum.cc").split(",")
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o]

# ── HTTPS (production sits behind Caddy, which terminates TLS) ───────────────
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    # Container and uptime health checks hit the backend directly over HTTP.
    SECURE_REDIRECT_EXEMPT = [r"^api/health/$"]
    SECURE_HSTS_SECONDS = 30 * 24 * 60 * 60
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

CORS_ALLOWED_ORIGINS = os.environ.get(
    "CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
).split(",")

ROOT_URLCONF = "pecker_picker.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "pecker_picker.wsgi.application"


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

# Postgres when POSTGRES_DB is set; otherwise the local SQLite file.
if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "postgres"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# ── Realtime (Django Channels) ────────────────────────────────────────────────
# Redis is required once more than one server process runs; the in-memory
# layer only reaches sockets in the same process.
REDIS_URL = os.environ.get("REDIS_URL", "")
# redis-py 8 defaults socket_timeout to 5s, the same as channels_redis's
# blocking BZPOPMIN wait, so idle reads time out before Redis replies.
# Shared across server processes (discover feed snapshots); per-process memory without Redis.
CACHES = {
    "default": (
        {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}
        if REDIS_URL else
        {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    ),
}

CHANNEL_LAYERS = {
    "default": (
        {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [{"address": REDIS_URL, "socket_timeout": 10}]},
        }
        if REDIS_URL else
        {"BACKEND": "channels.layers.InMemoryChannelLayer"}
    ),
}

# ── Push notifications (Firebase Cloud Messaging) ─────────────────────────────
# Path to a Firebase service-account JSON. Leave blank to disable push.
FIREBASE_CREDENTIALS_FILE = os.environ.get("FIREBASE_CREDENTIALS_FILE", "")


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Area lookup file built by deploy/build_areas.py; without it, neighbourhoods come from Nominatim.
AREAS_DB = os.environ.get("AREAS_DB", str(BASE_DIR / "data" / "areas.sqlite3"))

MAX_UPLOAD_SIZE_MB = 10
MAX_GALLERY_IMAGES = 6   # public photos per persona
MAX_SAVED_PHRASES = 30
MAX_CHAT_IMAGES = 4      # photos per message

# Keyed by User.TIER_* (0 = Free, 1 = Plus).
PERSONA_LIMITS = {0: 2, 1: 12}

# Beta only: every account sees demo profiles (not just testers), at any
# distance, so new testers and App Review have something to browse.
DEMO_PROFILES_FOR_EVERYONE = os.environ.get("DEMO_PROFILES_FOR_EVERYONE", "False") == "True"
LIBRARY_LIMITS = {0: 30, 1: 200}

# ── AWS Rekognition (image auto-tagging) ─────────────────────────────────────
# Leave AWS_REKOGNITION_REGION blank to disable auto-tagging (dev / no-AWS).
# Rekognition uses its own credentials so the MinIO keys are not sent to AWS.
AWS_REKOGNITION_REGION           = os.environ.get("AWS_REKOGNITION_REGION", "")
AWS_REKOGNITION_ACCESS_KEY_ID    = os.environ.get("AWS_REKOGNITION_ACCESS_KEY_ID", "")
AWS_REKOGNITION_SECRET_ACCESS_KEY = os.environ.get("AWS_REKOGNITION_SECRET_ACCESS_KEY", "")

# ── Object storage (MinIO locally, S3 in production) ─────────────────────────
# Internal endpoint — used by boto3 for API calls from the Django process.
AWS_S3_ENDPOINT_URL = os.environ.get("MINIO_ENDPOINT_URL", "http://localhost:9010")
AWS_ACCESS_KEY_ID = os.environ.get("MINIO_ACCESS_KEY", "peckeradmin")
AWS_SECRET_ACCESS_KEY = os.environ.get("MINIO_SECRET_KEY", "ChangeMeNow123!")
AWS_STORAGE_BUCKET_NAME = os.environ.get("MINIO_BUCKET", "pecker-picker")
AWS_S3_ADDRESSING_STYLE = "path"   # MinIO requires path-style; S3 uses virtual-hosted
AWS_S3_REGION_NAME = os.environ.get("MINIO_REGION", "us-east-1")
AWS_DEFAULT_ACL = None             # Bucket is private; clients get presigned URLs

# Lifetime of presigned media URLs (see api.media.presigned_url).
MEDIA_URL_EXPIRY_SECONDS = int(os.environ.get("MEDIA_URL_EXPIRY_SECONDS", 6 * 60 * 60))

# Public base URL returned to clients so they can load images directly.
# For MinIO: http://triangulum.cc:9010
# For S3:    https://your-bucket.s3.amazonaws.com  (or a CloudFront domain)
MEDIA_PUBLIC_URL = os.environ.get("MEDIA_PUBLIC_URL", "http://triangulum.cc:9010")

STORAGES = {
    "default": {
        "BACKEND": "api.storage.MediaStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

AUTH_USER_MODEL = "api.User"

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "messages": "60/min",
        "discover": "60/min",
        "auth": "30/min",       # per IP, signed-out passkey/OTP endpoints
        "otp_send": "5/hour",   # per phone number
        "otp_verify": "10/hour",
    },
}
if not DEBUG:
    # Caddy is the one proxy in front; without this every client shares Caddy's IP.
    REST_FRAMEWORK["NUM_PROXIES"] = 1

from datetime import timedelta

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'api': {'level': 'INFO'},
    },
}

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "")
TWILIO_VERIFY_SERVICE_SID = os.environ.get("TWILIO_VERIFY_SERVICE_SID", "")
TWILIO_ENABLED = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_VERIFY_SERVICE_SID)

OTP_EXPIRY_MINUTES = 10

# ── WebAuthn / Passkeys ───────────────────────────────────────────────────────
WEBAUTHN_RP_ID = os.environ.get("WEBAUTHN_RP_ID", "triangulum.cc")
WEBAUTHN_RP_NAME = os.environ.get("WEBAUTHN_RP_NAME", "Pecker Picker")
WEBAUTHN_ORIGINS = os.environ.get("WEBAUTHN_ORIGINS", "https://triangulum.cc").split(",")
# Run: keytool -list -v -keystore your-release.jks  to get this value
ANDROID_SHA256_CERT_FINGERPRINT = os.environ.get("ANDROID_SHA256_CERT_FINGERPRINT", "PLACEHOLDER")


def android_app_origin(fingerprint: str) -> str:
    """The WebAuthn origin Android reports for a native app: its signing cert's SHA-256."""
    cert_hash = bytes.fromhex(fingerprint.replace(":", ""))
    return "android:apk-key-hash:" + base64.urlsafe_b64encode(cert_hash).decode().rstrip("=")


if ANDROID_SHA256_CERT_FINGERPRINT not in ("", "PLACEHOLDER"):
    WEBAUTHN_ORIGINS.append(android_app_origin(ANDROID_SHA256_CERT_FINGERPRINT))

# ── Stripe ────────────────────────────────────────────────────────────────────
STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
# Map tier level → Stripe Price ID (recurring).  Add more entries for new tiers.
STRIPE_TIER_PRICES = {
    1: os.environ.get("STRIPE_PRICE_TIER1", ""),
}
# Where Stripe redirects after checkout or portal.
STRIPE_SUCCESS_URL = os.environ.get("STRIPE_SUCCESS_URL", "https://triangulum.cc/subscription/success")
STRIPE_CANCEL_URL  = os.environ.get("STRIPE_CANCEL_URL",  "https://triangulum.cc/subscription/cancel")
