"""
Base settings shared by every environment.

Environment-specific modules (development.py / production.py) import everything
from here and override only what actually differs.
"""
from datetime import timedelta
from pathlib import Path

from decouple import Csv, config
from django.utils.csp import CSP

# backend/config/settings/base.py -> backend/
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------
SECRET_KEY = config("SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = config("DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="", cast=Csv())

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------
DJANGO_APPS = [
    # django.contrib.admin, with the login limit and two-factor step added.
    "core.authentication.admin_config.ERPAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "drf_spectacular_sidecar",
    "drf_spectacular",
]

# The platform: identity, permissions, audit. Every module depends on these.
CORE_APPS = [
    "core.common",
    "core.organizations",
    "core.accounts",
    "core.permissions",
    "core.audit",
    "core.authentication",
    "core.files",
    "core.api_keys",
    "core.signup",
]

# Business modules, in dependency order: each may use the ones above it.
MODULE_APPS: list[str] = [
    "modules.students",
    "modules.parents",
    "modules.staff",
    "modules.admissions",
    "modules.academics",
    "modules.timetable",
    "modules.attendance",
    "modules.examinations",
    "modules.finance",
    "modules.events",
    "modules.notifications",
    "modules.notices",
    "modules.communication",
    "modules.support",
    "modules.library",
    "modules.inventory",
    "modules.hr",
    "modules.payroll",
    "modules.hostel",
    "modules.transport",
    "modules.applications",
    "modules.alumni",
    "modules.careers",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + CORE_APPS + MODULE_APPS

MIDDLEWARE = [
    # Outermost: everything below it, including failures, is logged with an id.
    "core.common.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Sends SECURE_CSP / SECURE_CSP_REPORT_ONLY (set per environment).
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    # Serves /static/ straight from the app container, so the image needs no
    # nginx sidecar to render /admin/ and the DRF docs. No-ops when DEBUG is on
    # and Django's own staticfiles handler takes over.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # Must sit above CommonMiddleware so preflight OPTIONS requests get the
    # CORS headers even when another middleware short-circuits the response.
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Each request starts on the default zone; login switches to the school's.
    "core.common.middleware.OrganizationTimezoneMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.audit.middleware.AuditContextMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                # Nonces for the admin's own <script> tags under CSP.
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --------------------------------------------------------------------------
# Database — overridden per environment (SQLite dev / PostgreSQL prod).
# All business logic goes through the ORM so the engine stays swappable.
# --------------------------------------------------------------------------
# SQLite has no row locks (select_for_update is a no-op), so two requests
# that read the same row and then both write would fail with "database is
# locked". IMMEDIATE takes the write lock when a transaction starts, making
# concurrent writers wait their turn, the way row locks make them wait on
# PostgreSQL.
SQLITE_OPTIONS = {"transaction_mode": "IMMEDIATE", "timeout": 20}

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": SQLITE_OPTIONS,
    }
}

# --------------------------------------------------------------------------
# Passwords / i18n / static
# --------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = config("TIME_ZONE", default="UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Uploaded files (résumés and the like) are private: they live outside
# MEDIA_ROOT, have no URL of their own, and are only served by the API after
# an access check (core/files). Swap the "private" backend for S3-compatible
# storage later without touching the modules.
PRIVATE_MEDIA_ROOT = config("PRIVATE_MEDIA_ROOT", default="") or str(BASE_DIR / "private")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "private": {"BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": PRIVATE_MEDIA_ROOT, "base_url": None}},
}
FILE_UPLOAD_MAX_BYTES = config("FILE_UPLOAD_MAX_BYTES", default=5 * 1024 * 1024, cast=int)

# --------------------------------------------------------------------------
# CORS / CSRF
# The SPA is served from its own origin, so the browser preflights every API
# call. Origins are an explicit allow-list from the environment — never "*",
# because credentialed requests are rejected by the browser when it is.
# --------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="", cast=Csv())

# JWT travels in the Authorization header, not a cookie, so credentials stay
# off by default. Flip it on only if you move refresh tokens into cookies.
CORS_ALLOW_CREDENTIALS = config("CORS_ALLOW_CREDENTIALS", default=False, cast=bool)
# Only the API is cross-origin; /admin/ and the docs are same-origin.
CORS_URLS_REGEX = r"^/api/.*$"
CORS_EXPOSE_HEADERS = ["Content-Disposition"]
CORS_PREFLIGHT_MAX_AGE = config("CORS_PREFLIGHT_MAX_AGE", default=3600, cast=int)

# Session-authenticated POSTs from the SPA also need the origin trusted here;
# CORS alone does not satisfy Django's CSRF origin check.
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

# --------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        # simplejwt, plus the organization's time zone for the request.
        "core.authentication.jwt.OrganizationJWTAuthentication",
        # Programs: "Authorization: Api-Key erp_..." (core/api_keys).
        "core.api_keys.authentication.ApiKeyAuthentication",
    ),
    # Secure by default: every endpoint requires auth unless it opts out.
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    # JSON only by default; development.py adds the browsable API back, so it
    # can never be served in production by inheriting DRF's defaults.
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
    ),
    "DEFAULT_PAGINATION_CLASS": "core.common.pagination.DefaultPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "core.common.exceptions.api_exception_handler",
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    # How many proxies sit in front of the app (load balancer, nginx). The
    # client IP used for rate limiting and the audit log is read from the
    # X-Forwarded-For entries those proxies added. Left unset, DRF trusted
    # the whole header — which the client writes — so sending a different
    # fake value per request bypassed the login limit.
    "NUM_PROXIES": config("NUM_PROXIES", default=0, cast=int),
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ("v1",),
    # Counters live in the cache: Redis in production, so every worker and
    # replica shares one count; a file cache in development.
    "DEFAULT_THROTTLE_CLASSES": (
        # Per-endpoint limits for views that set throttle_scope (login).
        "rest_framework.throttling.ScopedRateThrottle",
        # A ceiling on everything else, so a stolen token or a script cannot
        # copy out the whole student list in seconds.
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        # Each API key's own limit; does nothing for a person's login.
        "core.api_keys.throttling.ApiKeyRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "login": config("THROTTLE_LOGIN", default="10/min"),
        "anon": config("THROTTLE_ANON", default="60/min"),
        "user": config("THROTTLE_USER", default="600/min"),
        # Attendance devices sending punches (generic device API).
        "device": config("THROTTLE_DEVICE", default="120/min"),
        # Public application forms (no account): per address.
        "public_applications": config("THROTTLE_PUBLIC_APPLICATIONS", default="20/hour"),
        # Reading public forms and vacancies: a page view, so far looser than submitting.
        "public_read": config("THROTTLE_PUBLIC_READ", default="300/hour"),
        # File uploads, per user.
        "uploads": config("THROTTLE_UPLOADS", default="30/hour"),
        # An API key without a rate of its own.
        "api_key": config("THROTTLE_API_KEY", default="300/min"),
        # Public signup and its resend, per address.
        "signup": config("THROTTLE_SIGNUP", default="10/hour"),
        # Code availability and link checks, per address.
        "signup_check": config("THROTTLE_SIGNUP_CHECK", default="60/min"),
        # Forgot-password requests and confirmations, per address.
        "password_reset": config("THROTTLE_PASSWORD_RESET", default="10/hour"),
    },
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=config("ACCESS_TOKEN_LIFETIME_MINUTES", default=60, cast=int)
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=config("REFRESH_TOKEN_LIFETIME_DAYS", default=7, cast=int)
    ),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "TOKEN_OBTAIN_SERIALIZER": "core.authentication.serializers.LoginSerializer",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Education ERP API",
    "DESCRIPTION": (
        "An education ERP for schools, colleges and universities: students, "
        "academics, timetable, attendance, exams, fees, HR and payroll, library, "
        "inventory, hostel, transport, applications, alumni and careers."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v[0-9]",
    "COMPONENT_SPLIT_REQUEST": True,
    "SORT_OPERATIONS": False,
    # Several models have a `status` field with different choices; give each
    # set its own name so generated clients get StudentStatus, not Status93aEnum.
    # Swagger UI and Redoc files come from our own static files
    # (drf-spectacular-sidecar), not a CDN: nothing a third party can change,
    # and the pages work under the Content-Security-Policy below.
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
    "ENUM_NAME_OVERRIDES": {
        "StudentStatusEnum": "modules.students.models.Student.Status",
        "EnrollmentStatusEnum": "modules.students.models.Enrollment.Status",
        "StaffStatusEnum": "modules.staff.models.StaffMember.Status",
        "AdmissionStatusEnum": "modules.admissions.models.Admission.Status",
        "RelationshipEnum": "modules.parents.models.StudentParent.Relationship",
        "WeekdayEnum": "modules.timetable.models.Weekday",
        "AttendanceStatusEnum": "modules.attendance.models.AttendanceStatus",
        "AttendanceSessionStatusEnum": "modules.attendance.models.AttendanceSession.Status",
        "StaffDayStatusEnum": "modules.attendance.models.StaffAttendanceDay.Status",
        "ExamStatusEnum": "modules.examinations.models.Exam.Status",
        "MarkSheetStatusEnum": "modules.examinations.models.MarkSheet.Status",
        "TermResultStatusEnum": "modules.examinations.models.ResultPlan.Status",
        "AdmitCardStatusEnum": "modules.examinations.models.AdmitCard.Status",
        "MarkStatusEnum": "modules.examinations.models.MarkStatus",
        "ResultStatusEnum": "modules.examinations.models.ResultStatus",
        "ExamComponentKindEnum": "modules.examinations.models.ExamComponent.Kind",
        "InvoiceStatusEnum": "modules.finance.models.InvoiceStatus",
        "FrequencyEnum": "modules.finance.models.Frequency",
        "ScholarshipKindEnum": "modules.finance.models.ScholarshipKind",
        "InvoiceItemKindEnum": "modules.finance.models.InvoiceItemKind",
        "PaymentMethodEnum": "modules.finance.models.PaymentMethod",
        "RegistrationModeEnum": "modules.events.models.RegistrationMode",
        "EventStatusEnum": "modules.events.models.EventStatus",
        "RegistrationStatusEnum": "modules.events.models.RegistrationStatus",
        "EventAttendanceStatusEnum": "modules.events.models.AttendanceStatus",
        "ParticipationRoleEnum": "modules.events.models.ParticipationRole",
        "AttendanceSourceEnum": "modules.attendance.models.Source",
        "PointSourceEnum": "modules.events.models.PointSource",
        "AwardKindEnum": "modules.events.models.AwardKind",
        "ThresholdKindEnum": "modules.events.models.ThresholdKind",
        "ContractKindEnum": "modules.hr.models.ContractKind",
        "StaffDocumentKindEnum": "modules.hr.models.DocumentKind",
        "LeaveStatusEnum": "modules.hr.models.LeaveStatus",
        "TaxStatusEnum": "modules.hr.models.TaxStatus",
        "PayComponentKindEnum": "modules.payroll.models.ComponentKind",
        "PayslipLineKindEnum": "modules.payroll.models.LineKind",
        "PayslipLineSourceEnum": "modules.payroll.models.LineSource",
        "PayrollRunStatusEnum": "modules.payroll.models.RunStatus",
        # The hostel and transport modules added other "gender" and "direction" fields; keep these names stable.
        "GenderEnum": "core.common.choices.Gender",
        "DirectionEnum": "modules.attendance.models.Punch.Direction",
        "InvoiceSourceEnum": "modules.finance.models.InvoiceSource",
        "BuildingGenderEnum": "modules.hostel.models.BuildingGender",
        "AllocationStatusEnum": "modules.hostel.models.AllocationStatus",
        "ComplaintCategoryEnum": "modules.hostel.models.ComplaintCategory",
        "ComplaintStatusEnum": "modules.hostel.models.ComplaintStatus",
        "VehicleKindEnum": "modules.transport.models.VehicleKind",
        "VehicleDocumentKindEnum": "modules.transport.models.DocumentKind",
        "CrewRoleEnum": "modules.transport.models.CrewRole",
        "RideDirectionEnum": "modules.transport.models.Direction",
        "TripDirectionEnum": "modules.transport.models.TripDirection",
        "TripStatusEnum": "modules.transport.models.TripStatus",
        "BoardingStatusEnum": "modules.transport.models.BoardingStatus",
        "VehicleMaintenanceKindEnum": "modules.transport.models.MaintenanceKind",
        "ConditionEnum": "modules.inventory.models.AssetCondition",
        "AssetMaintenanceKindEnum": "modules.inventory.models.MaintenanceKind",
        "DisposalMethodEnum": "modules.inventory.models.DisposalMethod",
        "ApplicationKindEnum": "modules.applications.models.Kind",
        "ApplicationStatusEnum": "modules.applications.models.Status",
        "ApplicationEventActionEnum": "modules.applications.models.EventAction",
        "StudyStatusEnum": "modules.alumni.models.StudyStatus",
        "AlumniEventStatusEnum": "modules.alumni.models.EventStatus",
        "RsvpResponseEnum": "modules.alumni.models.RsvpResponse",
        "MentorshipStatusEnum": "modules.alumni.models.MentorshipStatus",
        "VacancyStatusEnum": "modules.careers.models.VacancyStatus",
        "InterviewModeEnum": "modules.careers.models.InterviewMode",
        "InterviewStatusEnum": "modules.careers.models.InterviewStatus",
        "RecommendationEnum": "modules.careers.models.Recommendation",
        "OfferStatusEnum": "modules.careers.models.OfferStatus",
        "PostingKindEnum": "modules.careers.models.PostingKind",
        "PostingAudienceEnum": "modules.careers.models.Audience",
        "PostingStatusEnum": "modules.careers.models.PostingStatus",
        "AudienceEnum": "modules.notices.models.NoticeAudience",
        "StaffTypeEnum": "modules.staff.models.StaffMember.StaffType",
        "UserTypeEnum": "core.accounts.models.User.Type",
        "TypeEnum": "core.organizations.models.Organization.Type",
        "SignupRequestStatusEnum": "core.signup.models.SignupRequest.Status",
    },
}

# --------------------------------------------------------------------------
# Public signup and password reset — see core/signup/
# --------------------------------------------------------------------------
# Off by default: a school running its own copy doesn't want strangers
# creating organizations on it. Production refuses to start with signup on
# and no CAPTCHA.
SIGNUP_ENABLED = config("SIGNUP_ENABLED", default=False, cast=bool)
# A platform admin approves each verified signup before it goes live.
SIGNUP_REQUIRE_APPROVAL = config("SIGNUP_REQUIRE_APPROVAL", default=False, cast=bool)
# How long the emailed verification link works.
SIGNUP_TOKEN_HOURS = config("SIGNUP_TOKEN_HOURS", default=24, cast=int)
# Frontend pages the emails link to; {token}, {uid} are filled in.
SIGNUP_VERIFY_URL = config("SIGNUP_VERIFY_URL", default="http://localhost:5173/signup/verify?token={token}")
PASSWORD_RESET_URL = config("PASSWORD_RESET_URL",
                            default="http://localhost:5173/reset-password?uid={uid}&token={token}")
# Django's reset tokens are good for this many seconds; we take minutes.
PASSWORD_RESET_TIMEOUT = config("PASSWORD_RESET_MINUTES", default=60, cast=int) * 60
# Extra throwaway-mail domains to refuse, on top of the built-in list.
SIGNUP_BLOCKED_EMAIL_DOMAINS = config("SIGNUP_BLOCKED_EMAIL_DOMAINS", default="", cast=Csv())
# Emails one address can be sent per hour (signup, resend, reset), so the
# forms can't be used to flood someone's inbox from many addresses.
EMAILS_PER_ADDRESS_PER_HOUR = config("EMAILS_PER_ADDRESS_PER_HOUR", default=5, cast=int)

# CAPTCHA for no-login forms: off | turnstile | hcaptcha | recaptcha.
CAPTCHA_PROVIDER = config("CAPTCHA_PROVIDER", default="off")
CAPTCHA_SITE_KEY = config("CAPTCHA_SITE_KEY", default="")
CAPTCHA_SECRET_KEY = config("CAPTCHA_SECRET_KEY", default="")
# reCAPTCHA v3 only: the lowest score (0.0 bot - 1.0 human) let through.
CAPTCHA_MIN_SCORE = config("CAPTCHA_MIN_SCORE", default=0.5, cast=float)
if CAPTCHA_PROVIDER not in ("off", "turnstile", "hcaptcha", "recaptcha"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(f"Unknown CAPTCHA_PROVIDER {CAPTCHA_PROVIDER!r}.")

# console: log mail instead of sending it | django: send via EMAIL_* settings.
EMAIL_DELIVERY = config("EMAIL_DELIVERY", default="console")
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="Education ERP <no-reply@localhost>")

# --------------------------------------------------------------------------
# Login protection — see core/authentication/lockout.py
# --------------------------------------------------------------------------
# Failures on one account (from any address) before it is locked, and how
# long it stays locked after the last failure.
LOGIN_LOCKOUT_ATTEMPTS = config("LOGIN_LOCKOUT_ATTEMPTS", default=5, cast=int)
LOGIN_LOCKOUT_MINUTES = config("LOGIN_LOCKOUT_MINUTES", default=15, cast=int)

# --------------------------------------------------------------------------
# API documentation access
# --------------------------------------------------------------------------
# Off: /api/schema/, /api/docs/ and /api/redoc/ are for staff users only
# (log in at /admin/ first). Development turns this on.
API_DOCS_PUBLIC = config("API_DOCS_PUBLIC", default=False, cast=bool)

# --------------------------------------------------------------------------
# Content-Security-Policy — enforced in production, report-only in
# development. The API itself returns JSON; this protects the HTML pages it
# serves (the admin and the API docs).
# --------------------------------------------------------------------------
CSP_POLICY = {
    "default-src": [CSP.SELF],
    # Scripts only from our own static files, plus the admin's nonced tags.
    "script-src": [CSP.SELF, CSP.NONCE],
    # Swagger UI and Redoc set inline style attributes; nonces can't cover
    # those. Inline styles cannot run code, so this stays low-risk.
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],
    "img-src": [CSP.SELF, "data:"],
    "font-src": [CSP.SELF, "data:"],
    "connect-src": [CSP.SELF],
    "worker-src": [CSP.SELF, "blob:"],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
}

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_context": {
            "()": "core.common.logging.RequestContextFilter",
        },
    },
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
        # Used for errors: everything needed to chase a 500 without opening
        # the database — which request, which user, which endpoint.
        "detailed": {
            "format": (
                "{levelname} {asctime} {name} "
                "request_id={request_id} user={user_id} ip={client_ip} "
                "{method} {path}\n{message}"
            ),
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["request_context"],
        },
        # Replaced by a rotating file handler in production.py. Keeping the
        # name defined here means the logger wiring below is identical in
        # every environment.
        "errors": {
            "class": "logging.StreamHandler",
            "level": "ERROR",
            "formatter": "detailed",
            "filters": ["request_context"],
        },
    },
    "loggers": {
        # Django logs every unhandled view exception here at ERROR with the
        # traceback attached. This is the 500 log.
        "django.request": {
            "handlers": ["console", "errors"],
            "level": "ERROR",
            "propagate": False,
        },
        # Uncaught exceptions escaping the WSGI/ASGI handler itself.
        "django.server": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        # Our own code: errors it raises deliberately go to the same file.
        "core": {
            "handlers": ["console", "errors"],
            "level": config("LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
    },
    "root": {"handlers": ["console"], "level": config("LOG_LEVEL", default="INFO")},
}
