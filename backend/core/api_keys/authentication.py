"""``Authorization: Api-Key erp_<prefix>_<secret>`` (or ``X-API-Key``).

Runs after JWT, so a request carries one or the other. A key that's
unknown, revoked or expired, or whose organization or user is inactive,
is refused with 401 and the same message, so a probe learns nothing. A
known key used from an address it isn't allowed, or for a write when it is
read-only, gets 403.
"""
import ipaddress
from datetime import timedelta

from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import SAFE_METHODS

from core.audit.middleware import get_client_ip
from core.common.timezones import activate_for

from . import keys
from .models import ApiKey

KEYWORD = "Api-Key"
# last_used_at is written at most this often, not on every request.
TOUCH_EVERY = timedelta(minutes=1)


def raw_key(request) -> str | None:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if header.startswith(f"{KEYWORD} "):
        return header[len(KEYWORD) + 1:].strip()
    return request.META.get("HTTP_X_API_KEY") or None


def ip_allowed(key: ApiKey, ip: str | None) -> bool:
    if not key.allowed_ips:
        return True
    if ip is None:
        return False
    address = ipaddress.ip_address(ip)
    return any(address in ipaddress.ip_network(net, strict=False) for net in key.allowed_ips)


class ApiKeyAuthentication(BaseAuthentication):
    def authenticate(self, request):
        raw = raw_key(request)
        if raw is None:
            return None
        invalid = exceptions.AuthenticationFailed("Invalid API key.", code="invalid_api_key")
        parts = keys.split(raw)
        if parts is None:
            raise invalid
        prefix, secret = parts
        key = ApiKey.objects.select_related("user", "organization").filter(prefix=prefix).first()
        if key is None or not keys.matches(secret, key.secret_hash):
            raise invalid
        now = timezone.now()
        if (key.revoked_at is not None or (key.expires_at is not None and key.expires_at <= now)
                or not key.organization.is_active or not key.user.is_active):
            raise invalid

        ip = get_client_ip(request)
        if not ip_allowed(key, ip):
            raise exceptions.PermissionDenied("This API key can't be used from your address.", code="ip_not_allowed")
        if key.read_only and request.method not in SAFE_METHODS:
            raise exceptions.PermissionDenied("This API key is read-only.", code="read_only_key")

        if key.last_used_at is None or now - key.last_used_at >= TOUCH_EVERY or key.last_used_ip != ip:
            ApiKey.objects.filter(pk=key.pk).update(last_used_at=now, last_used_ip=ip)
        activate_for(key.user)
        return key.user, key

    def authenticate_header(self, request):
        return KEYWORD
