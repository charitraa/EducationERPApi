"""Local development settings — debug on, permissive hosts, choice of database."""
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, REST_FRAMEWORK as BASE_REST_FRAMEWORK, config

DEBUG = config("DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = ["*"]

# Local frontend dev servers (Vite / CRA / Next). Override in .env if the
# frontend runs on a different port.
CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS",
    default=(
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:3000,http://127.0.0.1:3000"
    ),
    cast=Csv(),  # noqa: F405
)
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS

# mysql    — the team's local dev database (MySQL 8.4+ / MariaDB 10.6+).
#            Caveat: MySQL cannot enforce conditional unique constraints
#            (models.W036), so rules such as "campus code unique among
#            non-deleted campuses" are only checked by the serializers here
#            and by the database in production. Duplicates dev accepts,
#            production rejects.
# postgres — same engine as production; the Docker dev stack uses this.
# sqlite   — no server needed; the fallback when nothing is configured.
DEV_DATABASE = config("DEV_DATABASE", default="sqlite").lower()

if DEV_DATABASE == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": SQLITE_OPTIONS,  # noqa: F405
        }
    }
elif DEV_DATABASE in ("mysql", "postgres"):
    DATABASES = {
        "default": {
            "ENGINE": {
                "mysql": "django.db.backends.mysql",
                "postgres": "django.db.backends.postgresql",
            }[DEV_DATABASE],
            "NAME": config("DB_NAME"),
            "USER": config("DB_USER"),
            "PASSWORD": config("DB_PASSWORD"),
            "HOST": config("DB_HOST", default="localhost"),
            # Blank means the engine's own default: 3306 / 5432.
            "PORT": config("DB_PORT", default=""),
        }
    }
    if DEV_DATABASE == "mysql":
        DATABASES["default"]["OPTIONS"] = {
            # utf8mb4, not MySQL's 3-byte "utf8": names with emoji or some
            # scripts would otherwise fail to save.
            "charset": "utf8mb4",
            # Strict mode rejects over-long or invalid values instead of
            # silently truncating them, which is what Postgres does too.
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        }
        # The five W036 warnings are the known limitation described at the
        # top of this block and in the README. Silenced so they don't bury
        # real warnings on every command; only for MySQL, so Postgres and
        # SQLite would still report a new occurrence.
        SILENCED_SYSTEM_CHECKS = ["models.W036"]
else:
    raise ImproperlyConfigured(
        f"DEV_DATABASE must be mysql, postgres or sqlite, not {DEV_DATABASE!r}."
    )

# File-based so cached values and throttle counts survive a runserver reload
# and are shared by every process, without running Redis locally. Blank
# CACHE_DIR means backend/.cache/, which is gitignored.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": config("CACHE_DIR", default="") or str(BASE_DIR / ".cache"),
        "TIMEOUT": 300,
    }
}

# Copied, not mutated: `from .base import *` shares the dict object, so editing
# it in place would also change the settings any other module sees.
REST_FRAMEWORK = {
    **BASE_REST_FRAMEWORK,
    # The browsable API is useful locally and is deliberately absent in base,
    # so production can never inherit a writable HTML UI.
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ),
    # Session auth makes the browsable API and /admin/ usable while developing.
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "core.authentication.jwt.OrganizationJWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
}

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = config("EMAIL_HOST", default="")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = True

# Docs open to anyone locally; staff-only elsewhere (see base.py).
API_DOCS_PUBLIC = config("API_DOCS_PUBLIC", default=True, cast=bool)

# Report-only: the browser console shows what production would block,
# without breaking Django's debug pages, which use inline scripts.
SECURE_CSP_REPORT_ONLY = CSP_POLICY  # noqa: F405
