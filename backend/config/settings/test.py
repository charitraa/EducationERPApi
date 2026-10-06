"""Test settings — fast password hashing, in-memory database."""
from .base import *  # noqa: F401,F403
from .base import REST_FRAMEWORK as BASE_REST_FRAMEWORK

DEBUG = False
ALLOWED_HOSTS = ["*"]
# At least 32 bytes, or PyJWT warns about the HMAC key length.
SECRET_KEY = "test-only-secret-key-not-used-anywhere-real-0123456789"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# In-memory and per-process: tests must not share state through disk or Redis.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Copied rather than mutated — see development.py. Throttling is effectively
# off so tests that log in repeatedly don't trip the limiter; the dedicated
# throttling test lowers the rate itself.
REST_FRAMEWORK = {  # noqa: F405
    **BASE_REST_FRAMEWORK,
    "DEFAULT_THROTTLE_RATES": {"login": "10000/min", "anon": "10000/min", "user": "10000/min",
                               "device": "10000/min", "public_applications": "10000/min", "public_read": "10000/min",
                               "uploads": "10000/min", "api_key": "10000/min", "signup": "10000/min",
                               "signup_check": "10000/min", "password_reset": "10000/min"},
}

# Like the throttles above: effectively off, so tests that log in often don't
# lock their accounts. The lockout tests lower it themselves.
LOGIN_LOCKOUT_ATTEMPTS = 10000
EMAILS_PER_ADDRESS_PER_HOUR = 10000

LOGGING = {"version": 1, "disable_existing_loggers": False, "root": {"handlers": []}}

# Uploads go to a throwaway directory, never the project's private/ folder.
import tempfile  # noqa: E402

PRIVATE_MEDIA_ROOT = tempfile.mkdtemp(prefix="erp-test-private-")
STORAGES = {  # noqa: F405
    **STORAGES,  # noqa: F405
    "private": {"BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": PRIVATE_MEDIA_ROOT, "base_url": None}},
}
