"""Security hardening: one test class per protection.

Covers the fixes from the 2026-09-23 security review — spoofed client IPs,
the production secret key, API-wide rate limits, account lockout, the admin
login, two-factor login, private API docs and the Content-Security-Policy.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
from unittest import mock

import pyotp
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle, UserRateThrottle

from core.audit.middleware import get_client_ip
from core.audit.models import AuditLog
from core.authentication import two_factor
from core.authentication.models import TwoFactor
from tests.base import APITestCaseBase
from tests.factories import (
    DEFAULT_PASSWORD,
    create_organization,
    create_user,
    user_with_system_role,
)

LOGIN_URL = "/api/v1/auth/login/"
BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent


def login(client, email, password=DEFAULT_PASSWORD, **extra):
    return client.post(LOGIN_URL, {"email": email, "password": password, **extra})


class ClientIpTests(TestCase):
    """X-Forwarded-For is written by the client; only our proxies' entries count."""

    def request(self, xff=None):
        headers = {"HTTP_X_FORWARDED_FOR": xff} if xff else {}
        return RequestFactory().get("/", REMOTE_ADDR="10.0.0.9", **headers)

    def test_without_proxies_the_header_is_ignored(self):
        self.assertEqual(get_client_ip(self.request("6.6.6.6")), "10.0.0.9")

    @override_settings(REST_FRAMEWORK={"NUM_PROXIES": 1})
    def test_behind_one_proxy_the_entry_it_added_is_used(self):
        from rest_framework.settings import api_settings

        api_settings.reload()
        try:
            # client wrote "6.6.6.6"; our proxy appended the real address.
            self.assertEqual(get_client_ip(self.request("6.6.6.6, 203.0.113.5")), "203.0.113.5")
        finally:
            api_settings.reload()

    def test_spoofed_header_cannot_dodge_the_login_limit(self):
        org = create_organization(code="ip-college")
        user = create_user(org, email="ip@test.edu")
        cache.clear()
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"login": "3/min"}):
            statuses = [
                self.client.post(
                    LOGIN_URL,
                    {"email": user.email, "password": "wrong"},
                    HTTP_X_FORWARDED_FOR=f"198.51.100.{i}",
                ).status_code
                for i in range(5)
            ]
        cache.clear()
        self.assertEqual(statuses[-1], 429)

    def test_spoofed_header_does_not_reach_the_audit_log(self):
        self.client.post(
            LOGIN_URL, {"email": "x@test.edu", "password": "wrong"}, HTTP_X_FORWARDED_FOR="6.6.6.6"
        )
        entry = AuditLog.objects.get(action=AuditLog.Action.LOGIN_FAILED)
        self.assertEqual(entry.ip_address, "127.0.0.1")


class ProductionSecretKeyTests(TestCase):
    """Production must refuse to start on a missing or placeholder key."""

    def import_production(self, secret_key):
        env = {
            **os.environ,
            "SECRET_KEY": secret_key,
            "DB_NAME": "x", "DB_USER": "x", "DB_PASSWORD": "x",
            "REDIS_URL": "redis://localhost:6379/0",
            "LOG_DIR": tempfile.mkdtemp(),
        }
        return subprocess.run(
            [sys.executable, "-c", "import config.settings.production"],
            cwd=BACKEND_DIR, env=env, capture_output=True, text=True,
        )

    def test_missing_key_is_refused(self):
        result = self.import_production("")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SECRET_KEY must be set", result.stderr)

    def test_placeholder_keys_are_refused(self):
        for key in ("change-me-to-a-long-random-string" + "x" * 30, "django-insecure-" + "x" * 50, "short"):
            with self.subTest(key=key[:20]):
                self.assertNotEqual(self.import_production(key).returncode, 0)

    def test_a_real_key_is_accepted(self):
        from django.core.management.utils import get_random_secret_key

        result = self.import_production(get_random_secret_key())
        self.assertEqual(result.returncode, 0, result.stderr[-500:])


class ApiRateLimitTests(APITestCaseBase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.org = create_organization(code="rl-college")

    def test_logged_in_users_are_limited(self):
        self.authenticate(user_with_system_role(self.org, "org-admin", email="p@test.edu"))
        with mock.patch.dict(UserRateThrottle.THROTTLE_RATES, {"user": "3/min"}):
            statuses = [self.client.get("/api/v1/students/").status_code for _ in range(5)]
        self.assertEqual(statuses[:3], [200, 200, 200])
        self.assertEqual(statuses[-1], 429)

    def test_anonymous_callers_are_limited(self):
        # On public endpoints: protected ones answer 401 before any limit
        # applies, which already returns nothing.
        with mock.patch.dict(AnonRateThrottle.THROTTLE_RATES, {"anon": "2/min"}):
            statuses = [
                self.client.post("/api/v1/auth/refresh/", {"refresh": "junk"}).status_code
                for _ in range(4)
            ]
        self.assertEqual(statuses[:2], [401, 401])
        self.assertEqual(statuses[-1], 429)

    def test_reading_public_forms_is_not_held_to_the_submission_limit(self):
        """A family reloading the admission page shouldn't use up the few submissions
        and token lookups an address gets; those stay strict."""
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"public_applications": "2/min",
                                                                 "public_read": "100/min"}):
            reads = {self.client.get(url).status_code
                     for url in ["/api/v1/public/organizations/rl-college/application-types/",
                                 "/api/v1/public/organizations/rl-college/careers/vacancies/"] * 3}
            lookups = [self.client.post("/api/v1/public/organizations/rl-college/applications/status/",
                                        {"number": "APP-1", "token": "x"}).status_code for _ in range(3)]
        self.assertEqual(reads, {200})
        self.assertEqual(lookups[:2], [404, 404])
        self.assertEqual(lookups[-1], 429)

    def test_health_probes_are_never_limited(self):
        with mock.patch.dict(AnonRateThrottle.THROTTLE_RATES, {"anon": "1/min"}):
            statuses = {self.client.get(url).status_code for url in ["/health/", "/ready/"] * 3}
        self.assertEqual(statuses, {200})


@override_settings(LOGIN_LOCKOUT_ATTEMPTS=3, LOGIN_LOCKOUT_MINUTES=15)
class AccountLockoutTests(APITestCaseBase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = create_user(create_organization(code="lock-college"), email="lock@test.edu")

    def fail(self, email, times):
        for _ in range(times):
            login(self.client, email, "wrong")

    def test_account_locks_even_for_the_right_password(self):
        self.fail(self.user.email, 3)

        response = login(self.client, self.user.email)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data["error"]["code"], "throttled")

    def test_lock_is_per_account_not_per_address(self):
        other = create_user(self.user.organization, email="other@test.edu")
        self.fail(self.user.email, 3)

        self.assertEqual(login(self.client, other.email).status_code, 200)

    def test_unknown_emails_lock_the_same_way(self):
        """Otherwise the lock would reveal which emails have accounts."""
        self.fail("ghost@test.edu", 3)

        self.assertEqual(login(self.client, "ghost@test.edu", "wrong").status_code, 429)

    def test_success_resets_the_count(self):
        self.fail(self.user.email, 2)
        login(self.client, self.user.email)
        self.fail(self.user.email, 2)

        self.assertEqual(login(self.client, self.user.email).status_code, 200)

    def test_lock_ends_when_the_window_passes(self):
        self.fail(self.user.email, 3)
        cache.clear()  # the cache entry expiring

        self.assertEqual(login(self.client, self.user.email).status_code, 200)

    def test_locked_attempts_are_audited(self):
        self.fail(self.user.email, 3)
        login(self.client, self.user.email)

        entry = AuditLog.objects.filter(action="login_failed").latest("created_at")
        self.assertEqual(entry.metadata["reason"], "locked")
        self.assertEqual(entry.organization_id, self.user.organization_id)


@override_settings(LOGIN_LOCKOUT_ATTEMPTS=3)
class AdminLoginTests(TestCase):
    URL = "/admin/login/"

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.staff = create_user(email="staff@test.edu", is_staff=True, is_superuser=True)

    def post(self, password=DEFAULT_PASSWORD, **extra):
        return self.client.post(self.URL, {"username": self.staff.email, "password": password, **extra})

    def test_login_page_offers_the_code_field(self):
        self.assertContains(self.client.get(self.URL), 'name="otp"')

    def test_password_login_still_works_without_two_factor(self):
        self.assertEqual(self.post().status_code, 302)

    def test_repeated_failures_lock_the_admin_login(self):
        for _ in range(3):
            self.post("wrong")

        self.assertEqual(self.post().status_code, 429)

    def test_two_factor_is_required_in_the_admin_too(self):
        secret = enable_two_factor(self.staff)

        without = self.post()
        with_code = self.post(otp=pyotp.TOTP(secret).now())

        self.assertEqual(without.status_code, 200)  # form shown again with an error
        self.assertContains(without, "valid two-factor code")
        self.assertEqual(with_code.status_code, 302)


def enable_two_factor(user) -> str:
    """Turn 2FA on for ``user`` through the real services; returns the secret."""
    secret, _ = two_factor.begin_setup(user)
    two_factor.confirm_setup(user, pyotp.TOTP(secret).now())
    # confirm_setup consumed the current step; let tests use "now" again.
    TwoFactor.objects.filter(user=user).update(last_used_step=None)
    return secret


class TwoFactorTests(APITestCaseBase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.org = create_organization(code="tfa-college")
        self.user = user_with_system_role(self.org, "staff", email="tfa@test.edu")

    def test_setup_needs_the_password(self):
        self.authenticate(self.user)

        wrong = self.client.post("/api/v1/auth/2fa/setup/", {"password": "wrong"})
        right = self.client.post("/api/v1/auth/2fa/setup/", {"password": DEFAULT_PASSWORD})

        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(right.status_code, 200)
        self.assertTrue(right.data["otpauth_uri"].startswith("otpauth://totp/"))

    def test_full_setup_then_login_needs_the_code(self):
        self.authenticate(self.user)
        secret = self.client.post("/api/v1/auth/2fa/setup/", {"password": DEFAULT_PASSWORD}).data["secret"]

        # Not active until confirmed: plain login still works.
        self.assertEqual(login(self.client, self.user.email).status_code, 200)

        confirmed = self.client.post("/api/v1/auth/2fa/confirm/", {"code": pyotp.TOTP(secret).now()})
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(len(confirmed.data["recovery_codes"]), 10)
        TwoFactor.objects.filter(user=self.user).update(last_used_step=None)

        self.logout()
        no_code = login(self.client, self.user.email)
        bad_code = login(self.client, self.user.email, otp="000000")
        good = login(self.client, self.user.email, otp=pyotp.TOTP(secret).now())

        self.assertEqual(no_code.data["error"]["code"], "otp_required")
        self.assertEqual(bad_code.data["error"]["code"], "invalid_otp")
        self.assertEqual(good.status_code, 200)

    def test_a_code_cannot_be_replayed(self):
        secret = enable_two_factor(self.user)
        code = pyotp.TOTP(secret).now()

        first = login(self.client, self.user.email, otp=code)
        replay = login(self.client, self.user.email, otp=code)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.data["error"]["code"], "invalid_otp")

    def test_wrong_password_never_reveals_two_factor(self):
        enable_two_factor(self.user)

        response = login(self.client, self.user.email, "wrong")

        self.assertEqual(response.data["error"]["code"], "invalid_credentials")

    def test_a_recovery_code_works_once(self):
        enable_two_factor(self.user)
        codes = two_factor.regenerate_recovery_codes(self.user)

        first = login(self.client, self.user.email, otp=codes[0])
        again = login(self.client, self.user.email, otp=codes[0])

        self.assertEqual(first.status_code, 200)
        self.assertEqual(again.data["error"]["code"], "invalid_otp")

    def authenticate_with_code(self, secret):
        token = login(self.client, self.user.email, otp=pyotp.TOTP(secret).now()).data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_wrong_codes_count_towards_the_lockout(self):
        enable_two_factor(self.user)
        with override_settings(LOGIN_LOCKOUT_ATTEMPTS=3):
            for _ in range(3):
                login(self.client, self.user.email, otp="000000")
            self.assertEqual(login(self.client, self.user.email, otp="000000").status_code, 429)

    def test_disable_needs_password_and_code(self):
        secret = enable_two_factor(self.user)
        self.authenticate_with_code(secret)
        TwoFactor.objects.filter(user=self.user).update(last_used_step=None)

        no_code = self.client.post("/api/v1/auth/2fa/disable/", {"password": DEFAULT_PASSWORD, "code": "000000"})
        ok = self.client.post(
            "/api/v1/auth/2fa/disable/", {"password": DEFAULT_PASSWORD, "code": pyotp.TOTP(secret).now()}
        )

        self.assertEqual(no_code.status_code, 400)
        self.assertEqual(ok.status_code, 204)
        self.assertFalse(two_factor.is_enabled(self.user))

    def test_status(self):
        self.authenticate(self.user)
        self.assertEqual(self.client.get("/api/v1/auth/2fa/").data["enabled"], False)

    def test_admin_can_reset_a_lost_phone_but_not_for_someone_above_them(self):
        enable_two_factor(self.user)
        principal = user_with_system_role(self.org, "org-admin", email="principal@test.edu")
        enable_two_factor(principal)
        head = user_with_system_role(self.org, "campus-admin", email="head@test.edu")
        self.authenticate(head)

        allowed = self.client.post(f"/api/v1/users/{self.user.pk}/reset-2fa/")
        refused = self.client.post(f"/api/v1/users/{principal.pk}/reset-2fa/")

        self.assertEqual(allowed.status_code, 204)
        self.assertEqual(refused.status_code, 403)
        self.assertFalse(two_factor.is_enabled(self.user))
        self.assertTrue(two_factor.is_enabled(principal))


class ApiDocsAccessTests(TestCase):
    URLS = ["/api/schema/", "/api/docs/", "/api/redoc/"]

    @override_settings(API_DOCS_PUBLIC=False)
    def test_private_docs_refuse_anonymous_and_non_staff(self):
        for url in self.URLS:
            with self.subTest(url=url):
                self.assertIn(self.client.get(url).status_code, (401, 403))

        self.client.force_login(create_user(email="teacher@test.edu"))
        self.assertEqual(self.client.get("/api/schema/").status_code, 403)

    @override_settings(API_DOCS_PUBLIC=False)
    def test_staff_session_can_read_private_docs(self):
        self.client.force_login(create_user(email="dev@test.edu", is_staff=True))

        for url in self.URLS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    @override_settings(API_DOCS_PUBLIC=True)
    def test_public_docs_are_open(self):
        self.assertEqual(self.client.get("/api/schema/").status_code, 200)


class ContentSecurityPolicyTests(TestCase):
    def setUp(self):
        from django.conf import settings

        self.policy = settings.CSP_POLICY
        self.client.force_login(create_user(email="dev@test.edu", is_staff=True))

    def test_policy_header_is_sent(self):
        with override_settings(SECURE_CSP=self.policy):
            header = self.client.get("/api/docs/").headers["Content-Security-Policy"]

        # Only our own files may run scripts; a nonce is added only on pages
        # that use one (the admin), so the docs page gets none at all.
        self.assertIn("script-src 'self'", header)
        self.assertNotIn("unsafe-inline';", header.split("script-src")[1].split(";")[0])
        self.assertIn("frame-ancestors 'none'", header)
        self.assertIn("object-src 'none'", header)

    def test_docs_pages_have_no_inline_scripts_or_cdn(self):
        """Inline scripts would be blocked by the policy; a CDN is off-origin."""
        import re

        for url in ("/api/docs/", "/api/redoc/"):
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html)
                self.assertEqual(inline, [])
                self.assertNotIn("cdn.jsdelivr.net", html)

    def test_admin_login_scripts_carry_the_nonce(self):
        import re

        self.client.logout()
        with override_settings(SECURE_CSP=self.policy):
            response = self.client.get("/admin/login/")
        nonce = re.search(r"'nonce-([^']+)'", response.headers["Content-Security-Policy"]).group(1)
        for tag in re.findall(r"<script[^>]*>", response.content.decode()):
            self.assertIn(f'nonce="{nonce}"', tag)
