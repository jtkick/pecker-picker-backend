from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-t57qsp7fkvo(504hpmgq!cq3q$x7l_tvxybvun15-ah2_fl$tj")

DEBUG = os.environ.get("DEBUG", "True") == "True"

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,192.168.1.12,triangulum.cc").split(",")


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

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}


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

MAX_UPLOAD_SIZE_MB = 10
MAX_GALLERY_IMAGES = 6   # public photos per persona

# Keyed by User.TIER_* (0 = Free, 1 = Plus).
PERSONA_LIMITS = {0: 2, 1: 12}
LIBRARY_LIMITS = {0: 30, 1: 200}

# ── AWS Rekognition (image auto-tagging) ─────────────────────────────────────
# Leave AWS_REKOGNITION_REGION blank to disable auto-tagging (dev / no-AWS).
# Rekognition uses its own credentials so the MinIO keys are not sent to AWS.
AWS_REKOGNITION_REGION           = os.environ.get("AWS_REKOGNITION_REGION", "")
AWS_REKOGNITION_ACCESS_KEY_ID    = os.environ.get("AWS_REKOGNITION_ACCESS_KEY_ID", "")
AWS_REKOGNITION_SECRET_ACCESS_KEY = os.environ.get("AWS_REKOGNITION_SECRET_ACCESS_KEY", "")

# ── Object storage (MinIO locally, S3 in production) ─────────────────────────
# Internal endpoint — used by boto3 for API calls from the Django process.
AWS_S3_ENDPOINT_URL = os.environ.get("MINIO_ENDPOINT_URL", "http://localhost:9000")
AWS_ACCESS_KEY_ID = os.environ.get("MINIO_ACCESS_KEY", "peckeradmin")
AWS_SECRET_ACCESS_KEY = os.environ.get("MINIO_SECRET_KEY", "ChangeMeNow123!")
AWS_STORAGE_BUCKET_NAME = os.environ.get("MINIO_BUCKET", "pecker-picker")
AWS_S3_ADDRESSING_STYLE = "path"   # MinIO requires path-style; S3 uses virtual-hosted
AWS_DEFAULT_ACL = None             # Rely on bucket policy for public read
AWS_QUERYSTRING_AUTH = False       # Public URLs — no signed query strings needed

# Public base URL returned to clients so they can load images directly.
# For MinIO: http://triangulum.cc:9000
# For S3:    https://your-bucket.s3.amazonaws.com  (or a CloudFront domain)
MEDIA_PUBLIC_URL = os.environ.get("MEDIA_PUBLIC_URL", "http://triangulum.cc:9000")

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
}

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
