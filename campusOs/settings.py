"""
Django settings for the campusOs project (Redis-backed, hardcoded values).

Everything is set directly in this file: no .env and no environment variables.
The three switches you are most likely to change are right below.

Redis layout (one server, separate logical databases):
    db 0  Celery broker      db 1  Celery results      db 2  Django cache
The cache backs DRF throttling, the grading lock, the in-flight dedupe keys and
the AI result cache, so it must be a shared Redis (not per-process locmem).
"""
from datetime import timedelta
from pathlib import Path

import cloudinary
from celery.schedules import crontab
from corsheaders.defaults import default_headers

BASE_DIR = Path(__file__).resolve().parent.parent

# ═════════════════════════ SWITCHES ═════════════════════════
DEBUG = True                        # False for real production (turns on HTTPS rules)
CELERY_TASK_ALWAYS_EAGER = True     # True = run tasks inside the request (no worker needed)
AI_FALLBACK_TO_EAGER = True         # broker unreachable -> run the task inline instead of failing
REDIS_URL = "redis://127.0.0.1:6379"
# ════════════════════════════════════════════════════════════

# ─── Tunnel / public host (single source of truth) ───────────────────
# Whichever tunnel you're using right now. Change this line and it
# propagates to ALLOWED_HOSTS / CORS / CSRF / referers below.
#
# ngrok (free tier — subdomain changes every restart unless you use a
# static domain):
#   NGROK_HOST = "false-atrium-dallying.ngrok-free.dev"
#
# Cloudflare Tunnel (free — random subdomain each `cloudflared` session,
# but the wildcard ".trycloudflare.com" below covers them all):
#   Just leave NGROK_HOST as-is; the wildcards do the work.
NGROK_HOST = "false-atrium-dallying.ngrok-free.dev"

# ─── Security ────────────────────────────────────────────────────────
SECRET_KEY = "django-insecure-fallback-key-change-me"

ALLOWED_HOSTS = [
    # Local dev
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    # Custom LAN IPs (add your own if needed)
    # "192.168.1.50",

    # Render
    "campusos-0ju0.onrender.com",
    ".onrender.com",

    # ngrok (exact + wildcards for future subdomains)
    NGROK_HOST,
    ".ngrok-free.dev",
    ".ngrok-free.app",
    ".ngrok.io",
    ".ngrok.app",

    # Cloudflare quick tunnels — covers every random subdomain
    ".trycloudflare.com",
]

# ─── CORS & CSRF ─────────────────────────────────────────────────────
FRONTEND_URL = f"https://{NGROK_HOST}"

CORS_ALLOWED_ORIGINS = [
    # Local dev
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",

    # Static deploy targets
    "https://olailongfrontend.onrender.com",
    "https://olailongmarket-pwa.onrender.com",
    "https://campusos-0ju0.onrender.com",

    # Exact ngrok host
    f"https://{NGROK_HOST}",

    # Frontend URL from above
    FRONTEND_URL,
]

# Wildcard regexes — these are what actually keep tunnels working
# across subdomain changes. Order doesn't matter.
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://.*\.onrender\.com$",
    r"^https://.*\.ngrok-free\.dev$",
    r"^https://.*\.ngrok-free\.app$",
    r"^https://.*\.ngrok\.io$",
    r"^https://.*\.ngrok\.app$",
    r"^https://.*\.trycloudflare\.com$",     # ← Cloudflare quick tunnels
]

CORS_ALLOW_HEADERS = list(default_headers) + ["ngrok-skip-browser-warning"]
CORS_ALLOW_CREDENTIALS = True
CORS_EXPOSE_HEADERS = ["Content-Type", "X-CSRFToken"]

CSRF_TRUSTED_ORIGINS = [
    # Local dev
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
     "https://driving-tags-kenny-baking.trycloudflare.com/",
    # Static deploy targets
    "https://olailongfrontend.onrender.com",
    "https://olailongmarket-pwa.onrender.com",
    "https://campusos-0ju0.onrender.com",
    "https://*.onrender.com",
    

    # ngrok
    f"https://{NGROK_HOST}",
    "https://*.ngrok-free.dev",
    "https://*.ngrok-free.app",
    "https://*.ngrok.io",
    "https://*.ngrok.app",

    # Cloudflare
    "https://*.trycloudflare.com",           # ← Cloudflare quick tunnels
]

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
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "campusos",
        "USER": "campusos_user",
        "PASSWORD": "localdev123",
        "HOST": "localhost",
        "PORT": "5432",
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "connect_timeout": 10,
        },
    }
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

# ─── Cloudinary ──────────────────────────────────────────────────────
cloudinary.config(
    cloud_name="dtll1o9u0",
    api_key="387833656525477",
    api_secret="AmTSvrVHKiLlN2ArzFgctGx_-70",
    secure=True,
)

CLOUDINARY_STORAGE = {
    "CLOUD_NAME": "dtll1o9u0",
    "API_KEY": "387833656525477",
    "API_SECRET": "AmTSvrVHKiLlN2ArzFgctGx_-70",
}

# ─── Flutterwave ─────────────────────────────────────────────────────
FLW_SECRET_KEY = ""
FLW_SECRET_HASH = ""

# Absolute base URL of this backend, as seen from the internet.
# Leave as localhost when using Vite proxy (frontend tunnels, backend stays local).
BASE_URL = "http://localhost:8000"

# ─── Redis ───────────────────────────────────────────────────────────
REDIS_URL = REDIS_URL.rstrip("/")


def redis_url(db):
    return f"{REDIS_URL}/{db}"


# ─── Caching (Redis, shared by every web and worker process) ─────────
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

# ─── AI provider (OpenRouter, or a local OpenAI-compatible server) ───
AI_BASE_URL = "https://openrouter.ai/api/v1"
AI_API_KEY = ""

OPENROUTER_REFERER = FRONTEND_URL
OPENROUTER_APP_TITLE = "CampusOS"
OPENROUTER_JSON_MODE = True
OPENROUTER_TIMEOUT = 60
OPENROUTER_SDK_RETRIES = 2
OPENROUTER_REQUIRE_PARAMETERS = False

AI_DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
AI_GRADING_MODEL = "deepseek/deepseek-v4-flash"
AI_REASONING = {"enabled": False}
AI_EXTRA_BODY = {}

AI_MAX_INPUT_CHARS = 30000
AI_HIGHLIGHT_MAX_TOKENS = 1500
AI_GRADING_MAX_TOKENS = 1000
AI_RESEARCH_MAX_TOKENS = 2000
AI_MAX_TOKENS_CAP = 4000
AI_REPAIR_ATTEMPTS = 1
AI_CACHE_TTL = 7 * 24 * 3600
AI_CONFIG_TTL = 30
AI_GRADING_WORKERS = 4
AI_SIMULATE_PAYMENTS = DEBUG

EXAM_SUBMIT_GRACE_SECONDS = 60
EXAM_MAX_ANSWER_CHARS = 10000
EXAM_REVEAL_ANSWERS_AFTER_GRADING = False

# ─── Correction Report Branding ──────────────────────────────────────
CORRECTION_UNIV_NAME = "Gulu University"
CORRECTION_DEPT_NAME = "Department of Geography"
CORRECTION_BRAND_COLOR = "#0d47a1"
CORRECTION_LOGO_PATH = ""

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


from decouple import config  # add this near the top with other imports

# ─── Pesapal ─────────────────────────────────────────────────
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

# Toggle the simulated-payment path off now that Pesapal is live
AI_SIMULATE_PAYMENTS = False