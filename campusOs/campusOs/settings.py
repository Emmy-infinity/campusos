"""
Django settings for campusOs project.
"""

import os
from pathlib import Path
from datetime import timedelta

import dj_database_url
from dotenv import load_dotenv

import cloudinary
import cloudinary.uploader
import cloudinary.api


load_dotenv()
BASE_DIR = Path(__file__).resolve().parent.parent

# ─── Security ────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-fallback-key-change-me")
DEBUG = os.getenv("DEBUG", "True") == "True"

ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "campusos-0ju0.onrender.com",
    ".onrender.com",
]

# ─── CORS & CSRF ─────────────────────────────────────────────────────
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://olailongfrontend.onrender.com")

CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "https://olailongfrontend.onrender.com",
    "https://olailongmarket-pwa.onrender.com",
    "https://campusos-0ju0.onrender.com",
    FRONTEND_URL,
]

CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://.*\.onrender\.com$",
]

if os.getenv("CORS_ALLOW_ALL", "False") == "True":
    CORS_ALLOW_ALL_ORIGINS = True

CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]

CORS_ALLOW_CREDENTIALS = True
CORS_EXPOSE_HEADERS = ["Content-Type", "X-CSRFToken"]

CSRF_TRUSTED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "https://olailongfrontend.onrender.com",
    "https://olailongmarket-pwa.onrender.com",
    "https://campusos-0ju0.onrender.com",
    "https://*.onrender.com",
]

# ─── Production Security (only when DEBUG=False) ─────────────────────
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_REFERRER_POLICY = "same-origin"

# ─── REST Framework & JWT ────────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": False,
}

# ─── Installed Apps ──────────────────────────────────────────────────
INSTALLED_APPS = [
    "unfold",
    "unfold.contrib.filters",
    "cloudinary_storage",
    "django.contrib.admin",
    "cloudinary",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "corsheaders",
    "rest_framework",
    "django_filters",
    "campusos",
]

# ─── Middleware ──────────────────────────────────────────────────────
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "campusOs.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "campusOs.wsgi.application"

# ─── Database ────────────────────────────────────────────────────────
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{os.path.join(BASE_DIR, 'db.sqlite3')}",
        conn_max_age=0,
        conn_health_checks=True,
    )
}

if DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"]["sslmode"] = "require"
    DATABASES["default"]["OPTIONS"]["connect_timeout"] = 10

# ─── Password Validation ─────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ─── Internationalization ────────────────────────────────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Kampala"
USE_I18N = True
USE_TZ = True

# ─── Static & Media ──────────────────────────────────────────────────
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"

STORAGES = {
    "default": {
        "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = DEBUG
WHITENOISE_MAX_AGE = 31536000 if not DEBUG else 0

# ─── Cloudinary (HARDCODED — see warning below) ──────────────────────
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME", "dtll1o9u0"),
    api_key=os.getenv("CLOUDINARY_API_KEY", "387833656525477"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET", "AmTSvrVHKiLlN2ArzFgctGx_-70"),
    secure=True,
)

CLOUDINARY_STORAGE = {
    "CLOUD_NAME": os.getenv("CLOUDINARY_CLOUD_NAME", "dtll1o9u0"),
    "API_KEY": os.getenv("CLOUDINARY_API_KEY", "387833656525477"),
    "API_SECRET": os.getenv("CLOUDINARY_API_SECRET", "AmTSvrVHKiLlN2ArzFgctGx_-70"),
}

# ─── Flutterwave ─────────────────────────────────────────────────────
FLW_SECRET_KEY = os.getenv("FLUTTERWAVE_SECRET_KEY", "")
FLW_SECRET_HASH = os.getenv("FLUTTERWAVE_WEBHOOK_SECRET_HASH", "")

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

# ─── Caching ─────────────────────────────────────────────────────────
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "unique-snowflake",
    }
}

# ─── Logging ─────────────────────────────────────────────────────────
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO" if not DEBUG else "DEBUG",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO" if not DEBUG else "DEBUG",
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["console"],
            "level": "WARNING" if not DEBUG else "INFO",
            "propagate": False,
        },
    },
}

# ─── Document Upload Settings ───────────────────────────────────────
DOCUMENT_ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".odt",
    ".txt", ".md", ".ppt", ".pptx",
    ".xls", ".xlsx", ".csv", ".rtf",
}

DOCUMENT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000
DATA_UPLOAD_MAX_NUMBER_FILES = 100

AUTH_USER_MODEL = "campusos.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ─── Celery Configuration ────────────────────────────────────────────
# On free tier: run tasks synchronously in-process (no broker needed).
# When you pay for Redis + Worker, set CELERY_TASK_ALWAYS_EAGER=False
# in Render's environment variables — no code changes required.

CELERY_TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "True") == "True"

# These only apply when CELERY_TASK_ALWAYS_EAGER is False
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)

CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True

# Production tuning (applies whether eager or async)
CELERY_TASK_TIME_LIMIT = 180            # hard kill after 3 minutes
CELERY_TASK_SOFT_TIME_LIMIT = 150       # graceful shutdown at 2.5 min
CELERY_TASK_ACKS_LATE = True            # retry if worker dies mid-run
CELERY_RESULT_EXPIRES = 3600            # keep results for 1 hour

# Eager-mode specifics
CELERY_TASK_EAGER_PROPAGATES = True     # raise exceptions synchronously in dev
CELERY_TASK_STORE_EAGER_RESULT = True   # store results in-process for AsyncResult

# ─── OpenRouter (AI) ────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_REFERER = os.getenv("OPENROUTER_REFERER", "https://campusos-0ju0.onrender.com")
OPENROUTER_APP_TITLE = os.getenv("OPENROUTER_APP_TITLE", "CampusOS")
OPENROUTER_JSON_MODE = True
OPENROUTER_TIMEOUT = 60

# Model used by ai_services.py
# "deepseek/deepseek-v4-flash" is what works on your OpenRouter account
AI_DEFAULT_MODEL = os.getenv("AI_DEFAULT_MODEL", "deepseek/deepseek-v4-flash")
AI_GRADING_MODEL = os.getenv("AI_GRADING_MODEL", "deepseek/deepseek-v4-flash")
AI_MAX_INPUT_CHARS = int(os.getenv("AI_MAX_INPUT_CHARS", "30000"))
