"""
Django settings for the campusOs project.

All deployment-specific values and secrets come from environment variables
(loaded from .env by python-decouple in local dev, from the platform's env
store in production). Nothing sensitive is committed.
"""
import os
from datetime import timedelta
from pathlib import Path

import cloudinary
import dj_database_url
from celery.schedules import crontab
from corsheaders.defaults import default_headers
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent

# ═════════════════════════ SWITCHES (env-driven) ═════════════════════════
DEBUG = config("DEBUG", default=False, cast=bool)
CELERY_TASK_ALWAYS_EAGER = config("CELERY_TASK_ALWAYS_EAGER", default=False, cast=bool)
AI_FALLBACK_TO_EAGER = config("AI_FALLBACK_TO_EAGER", default=False, cast=bool)
REDIS_URL = config("REDIS_URL", default="redis://127.0.0.1:6379").rstrip("/")
# ═════════════════════════════════════════════════════════════════════════

# ─── Tunnel / public host ────────────────────────────────────────────
NGROK_HOST = config("NGROK_HOST", default="")

# ─── Security ────────────────────────────────────────────────────────
# No default — the app fails fast if SECRET_KEY is missing.
SECRET_KEY = config("SECRET_KEY")

ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "campusos-0ju0.onrender.com",
    ".onrender.com",
    ".ngrok-free.dev",
    ".ngrok-free.app",
    ".ngrok.io",
    ".ngrok.app",
    ".trycloudflare.com",
]

if NGROK_HOST:
    ALLOWED_HOSTS.append(NGROK_HOST)

# Render sets this automatically; appending it makes the deploy just work.
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)

# ─── CORS & CSRF ─────────────────────────────────────────────────────
FRONTEND_URL = config(
    "FRONTEND_URL",
    default=f"https://{NGROK_HOST}" if NGROK_HOST else "http://localhost:5173",
)

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

if NGROK_HOST:
    CORS_ALLOWED_ORIGINS.append(f"https://{NGROK_HOST}")

CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://.*\.onrender\.com$",
    r"^https://.*\.ngrok-free\.dev$",
    r"^https://.*\.ngrok-free\.app$",
    r"^https://.*\.ngrok\.io$",
    r"^https://.*\.ngrok\.app$",
    r"^https://.*\.trycloudflare\.com$",
]

CORS_ALLOW_HEADERS = list(default_headers) + ["ngrok-skip-browser-warning"]
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
    "https://*.ngrok-free.dev",
    "https://*.ngrok-free.app",
    "https://*.ngrok.io",
    "https://*.ngrok.app",
    "https://*.trycloudflare.com",
]

if NGROK_HOST:
    CSRF_TRUSTED_ORIGINS.append(f"https://{NGROK_HOST}")

if RENDER_EXTERNAL_HOSTNAME:
    CSRF_TRUSTED_ORIGINS.append(f"https://{RENDER_EXTERNAL_HOSTNAME}")

# ─── Production Security (only when DEBUG=False) ─────────────────────
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_REFERRER_POLICY = "same-origin"
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_HTTPONLY = True
    X_FRAME_OPTIONS = "DENY"

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
    "DEFAULT_THROTTLE_RATES": {
        "ai_calls": "30/min",
        "ai_purchase": "10/hour",
        "register": "20/hour",
    },
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
    "campusos.apps.CampusosConfig",
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

# ─── Database (Bulletproof for Neon/Render) ──────────────────────────
# Fetch the exact string from the environment first, then explicitly 
# parse it. This bypasses dj_database_url's fallback bugs.
DATABASE_URL = config(
    "DATABASE_URL", 
    default="postgres://postgres:postgres@localhost:5432/campusos"
)

DATABASES = {
    "default": dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=60,
        conn_health_checks=True,
    )
}

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

STORAGES = {
    "default": {
        "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage",
    },
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ),
    },
}

WHITENOISE_USE_FINDERS = DEBUG
WHITENOISE_AUTOREFRESH = DEBUG
WHITENOISE_MAX_AGE = 0 if DEBUG else 31536000

# ─── Cloudinary (env-driven) ─────────────────────────────────────────
CLOUDINARY_CLOUD_NAME = config("CLOUDINARY_CLOUD_NAME", default="")
CLOUDINARY_API_KEY = config("CLOUDINARY_API_KEY", default="")
CLOUDINARY_API_SECRET = config("CLOUDINARY_API_SECRET", default="")

cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET,
    secure=True,
)

CLOUDINARY_STORAGE = {
    "CLOUD_NAME": CLOUDINARY_CLOUD_NAME,
    "API_KEY": CLOUDINARY_API_KEY,
    "API_SECRET": CLOUDINARY_API_SECRET,
}

# ─── Flutterwave (env-driven) ────────────────────────────────────────
FLW_SECRET_KEY = config("FLW_SECRET_KEY", default="")
FLW_SECRET_HASH = config("FLW_SECRET_HASH", default="")

# ─── Absolute backend URL ────────────────────────────────────────────
BASE_URL = config("BASE_URL", default="http://localhost:8000")

# ─── Redis helper ────────────────────────────────────────────────────
def redis_url(db):
    return f"{REDIS_URL}/{db}"


# ─── Caching ─────────────────────────────────────────────────────────
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": redis_url(2),
        "KEY_PREFIX": "campusos",
        "TIMEOUT": 3600,
        "OPTIONS": {
            "socket_connect_timeout": 2,
            "socket_timeout": 2,
        },
    }
}

# ─── Celery ──────────────────────────────────────────────────────────
CELERY_BROKER_URL = redis_url(0)
CELERY_RESULT_BACKEND = redis_url(1)
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": 3600,
    "socket_connect_timeout": 5,
}

CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_TASK_TRACK_STARTED = True
CELERY_RESULT_EXPIRES = 60 * 60 * 24

CELERY_TASK_TIME_LIMIT = 960
CELERY_TASK_SOFT_TIME_LIMIT = 900
CELERY_TASK_ACKS_LATE = False

CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200
CELERY_WORKER_CONCURRENCY = 4
CELERY_WORKER_SEND_TASK_EVENTS = True
CELERY_TASK_SEND_SENT_EVENT = True

CELERY_BEAT_SCHEDULE = {
    "reset-monthly-ai-allowances": {
        "task": "ai.reset_monthly_allowances",
        "schedule": crontab(hour=2, minute=0),
    },
}

# ─── AI provider (env-driven) ────────────────────────────────────────
AI_BASE_URL = config("AI_BASE_URL", default="https://openrouter.ai/api/v1")
AI_API_KEY = config("OPENROUTER_API_KEY", default="")

OPENROUTER_REFERER = FRONTEND_URL
OPENROUTER_APP_TITLE = config("OPENROUTER_APP_TITLE", default="CampusOS")
OPENROUTER_JSON_MODE = True
OPENROUTER_TIMEOUT = 60
OPENROUTER_SDK_RETRIES = 2
OPENROUTER_REQUIRE_PARAMETERS = False

AI_DEFAULT_MODEL = config("AI_DEFAULT_MODEL", default="deepseek/deepseek-v4-flash")
AI_GRADING_MODEL = config("AI_GRADING_MODEL", default="deepseek/deepseek-v4-flash")
AI_REASONING = {"enabled": False}
AI_EXTRA_BODY = {}

# ── Tunables: admin can override each one in AIServiceConfig ─────────
AI_HIGHLIGHT_MAX_TOKENS = config("AI_HIGHLIGHT_MAX_TOKENS", default=1500, cast=int)
AI_GRADING_MAX_TOKENS = config("AI_GRADING_MAX_TOKENS", default=1000, cast=int)
AI_RESEARCH_MAX_TOKENS = config("AI_RESEARCH_MAX_TOKENS", default=2000, cast=int)
AI_REPAIR_ATTEMPTS = config("AI_REPAIR_ATTEMPTS", default=1, cast=int)
AI_TEMPERATURE = config("AI_TEMPERATURE", default=0.0, cast=float)
AI_MAX_ANSWER_CHARS = config("AI_MAX_ANSWER_CHARS", default=10000, cast=int)

# ── Hard limits: env only, not exposed to admin ──────────────────────
AI_MAX_INPUT_CHARS = config("AI_MAX_INPUT_CHARS", default=30000, cast=int)
AI_MAX_TOKENS_CAP = config("AI_MAX_TOKENS_CAP", default=4000, cast=int)
AI_CACHE_TTL = config("AI_CACHE_TTL", default=7 * 24 * 3600, cast=int)
AI_CONFIG_TTL = config("AI_CONFIG_TTL", default=30, cast=int)
AI_GRADING_WORKERS = config("AI_GRADING_WORKERS", default=4, cast=int)

# ── Payment simulation (defaults to DEBUG; set explicitly in .env) ──
AI_SIMULATE_PAYMENTS = config("AI_SIMULATE_PAYMENTS", default=DEBUG, cast=bool)

EXAM_SUBMIT_GRACE_SECONDS = 60
EXAM_MAX_ANSWER_CHARS = 10000
EXAM_REVEAL_ANSWERS_AFTER_GRADING = False

# ─── Correction Report Branding (env-driven) ─────────────────────────
CORRECTION_UNIV_NAME = config("CORRECTION_UNIV_NAME", default="Gulu University")
CORRECTION_DEPT_NAME = config("CORRECTION_DEPT_NAME", default="Department of Geography")
CORRECTION_BRAND_COLOR = config("CORRECTION_BRAND_COLOR", default="#0d47a1")
CORRECTION_LOGO_PATH = config("CORRECTION_LOGO_PATH", default="")

# ─── Document Upload Settings ────────────────────────────────────────
DOCUMENT_ALLOWED_EXTENSIONS = {
    ".pdf",
    ".docx", ".doc",
    ".odt",
    ".rtf",
    ".pptx", ".ppt",
    ".xlsx", ".xls",
    ".csv",
    ".txt", ".md",
}

DOCUMENT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000
DATA_UPLOAD_MAX_NUMBER_FILES = 100

AUTH_USER_MODEL = "campusos.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ─── Logging ─────────────────────────────────────────────────────────
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": "DEBUG" if DEBUG else "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.db.backends": {
            "handlers": ["console"],
            "level": "INFO" if DEBUG else "WARNING",
            "propagate": False,
        },
        "openai": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
        "celery": {"level": "INFO"},
        "kombu": {"level": "WARNING"},
        "redis": {"level": "WARNING"},
        "campusos": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# ─── Pesapal (env-driven) ────────────────────────────────────────────
PESAPAL_CONSUMER_KEY = config("PESAPAL_CONSUMER_KEY", default="")
PESAPAL_CONSUMER_SECRET = config("PESAPAL_CONSUMER_SECRET", default="")
PESAPAL_ENV = config("PESAPAL_ENV", default="sandbox")

PESAPAL_BASE_URL = (
    "https://cybqa.pesapal.com/pesapalv3/api/"
    if PESAPAL_ENV == "sandbox"
    else "https://pay.pesapal.com/pesapalv3/api/"
)

PESAPAL_CALLBACK_URL = config(
    "PESAPAL_CALLBACK_URL", default="http://localhost:5173/tokens"
)
PESAPAL_IPN_URL = config(
    "PESAPAL_IPN_URL",
    default="http://localhost:8000/api/payments/ipn/",
)
PESAPAL_IPN_ID = config("PESAPAL_IPN_ID", default="")
