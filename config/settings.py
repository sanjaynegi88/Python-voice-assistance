"""
Django settings for the AIDA voice-banking tool API + admin dashboard.

Everything environment-specific is read from the process environment (a local
.env is loaded first, if present) so the same image runs locally, in docker
compose, and on a host with real credentials.
"""
from pathlib import Path
from urllib.parse import unquote, urlparse
import os

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# small env helpers
# ---------------------------------------------------------------------------
def env(name, default=""):
    return os.getenv(name, default)


def env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_list(name, default=""):
    raw = os.getenv(name, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY", "insecure-dev-key-do-not-use-in-production")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0,web")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# Behind a tunnel (ngrok/cloudflared) or a load balancer, trust the proto header
# so Django builds https:// absolute URLs on the Settings page.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "bank",
    "api",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# ---------------------------------------------------------------------------
# database
# DATABASE_URL wins; otherwise a local SQLite file so `manage.py runserver`
# works with zero infrastructure.
# ---------------------------------------------------------------------------
def database_from_url(url):
    parsed = urlparse(url)
    engines = {
        "postgres": "django.db.backends.postgresql",
        "postgresql": "django.db.backends.postgresql",
        "sqlite": "django.db.backends.sqlite3",
    }
    engine = engines.get(parsed.scheme)
    if engine is None:
        raise ValueError("Unsupported DATABASE_URL scheme: " + repr(parsed.scheme))

    if engine.endswith("sqlite3"):
        return {"ENGINE": engine, "NAME": parsed.path or str(BASE_DIR / "db.sqlite3")}

    return {
        "ENGINE": engine,
        "NAME": unquote(parsed.path or "").lstrip("/"),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
        "CONN_MAX_AGE": 60,
    }


DATABASE_URL = env("DATABASE_URL")
if DATABASE_URL:
    DATABASES = {"default": database_from_url(DATABASE_URL)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

LOGIN_URL = "/dashboard/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/dashboard/login/"


# ---------------------------------------------------------------------------
# tool API
# ---------------------------------------------------------------------------
TOOL_API_KEYS = env_list("TOOL_API_KEYS", "dev-aida-key-change-me")
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "").rstrip("/")
# Local API of the ngrok agent, used to discover the live tunnel URL.
# "ngrok" is the compose service name; use 127.0.0.1 when running ngrok on the host.
NGROK_API_URL = env("NGROK_API_URL", "http://ngrok:4040/api/tunnels")
# The dashboard can run the agent as a child process of this container, which is
# what makes a Start button possible without mounting the Docker socket. Local
# development only -- the views refuse it for non-local requests.
NGROK_AUTHTOKEN = env("NGROK_AUTHTOKEN")
NGROK_BINARY = env("NGROK_BINARY", "ngrok")
NGROK_PIDFILE = env("NGROK_PIDFILE", "/tmp/ngrok-agent.pid")
NGROK_LOGFILE = env("NGROK_LOGFILE", "/tmp/ngrok-agent.log")
NGROK_FORWARD_ADDR = env("NGROK_FORWARD_ADDR", "http://127.0.0.1:8000")

AUTH_SESSION_TTL_SECONDS = env_int("AUTH_SESSION_TTL_SECONDS", 900)
MAX_PIN_ATTEMPTS = env_int("MAX_PIN_ATTEMPTS", 3)
LOCKOUT_MINUTES = env_int("LOCKOUT_MINUTES", 15)
OTP_TTL_SECONDS = env_int("OTP_TTL_SECONDS", 300)
MAX_OTP_ATTEMPTS = env_int("MAX_OTP_ATTEMPTS", 3)
REQUIRE_OTP_FOR_CARD_BLOCK = env_bool("REQUIRE_OTP_FOR_CARD_BLOCK", False)


# ---------------------------------------------------------------------------
# notifications
#
# These are seed values only. On first run they are copied into the
# bank.ProviderSettings row, which is the live source of truth from then on and
# is editable at Dashboard -> Settings -> Providers. Changing them here after
# that point has no effect unless you delete the row (or use the "Reload from
# environment" button on that page).
# ---------------------------------------------------------------------------
# Legacy single-provider switch, still honoured as a per-channel default.
NOTIFICATION_PROVIDER = env("NOTIFICATION_PROVIDER", "console").strip().lower()
# Preferred: one provider per channel.
EMAIL_PROVIDER = env("EMAIL_PROVIDER").strip().lower()
SMS_PROVIDER = env("SMS_PROVIDER").strip().lower()
DEFAULT_NOTIFICATION_CHANNEL = env("DEFAULT_NOTIFICATION_CHANNEL", "sms").strip().lower()

# Brevo -- one API key covers both transactional email and transactional SMS.
BREVO_API_KEY = env("BREVO_API_KEY")
BREVO_SENDER_EMAIL = env("BREVO_SENDER_EMAIL")
BREVO_SENDER_NAME = env("BREVO_SENDER_NAME", "AIDA Bank")
BREVO_SMS_SENDER = env("BREVO_SMS_SENDER", "AIDABank")

# SMTP. The Django mail backend is configured per-send from ProviderSettings,
# so these are only the initial seed values.
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST")
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "no-reply@aidabank.example")

TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = env("TWILIO_FROM_NUMBER")

# Outbound HTTP timeout for provider APIs, seconds.
PROVIDER_HTTP_TIMEOUT = env_int("PROVIDER_HTTP_TIMEOUT", 10)


# ---------------------------------------------------------------------------
# logging -- every tool call is emitted on the `toolcall` logger as one line,
# which is what makes the agent's tool use verifiable from `docker compose logs`.
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)-7s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "toolcall": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.db.backends": {"level": "WARNING"},
    },
}
