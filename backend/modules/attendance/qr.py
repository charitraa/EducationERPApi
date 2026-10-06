"""Short-lived QR tokens.

A token is signed with the project's secret key, so it can't be forged or
edited, and carries its own expiry, so a screenshot shared on a group chat
stops working within a minute or so. It names *what* is being attended (a
session, or a campus gate for staff), never *who*: the person always comes
from the account that scans it.
"""
import math
import time
from dataclasses import dataclass

from django.core import signing

from core.common.exceptions import PermissionDeniedError, ServiceError

_SALT = "attendance.qr"
MIN_TTL, MAX_TTL, DEFAULT_TTL = 15, 300, 60


@dataclass
class Token:
    kind: str  # "session" or "staff"
    organization_id: int
    target_id: int  # the session, or the campus
    expires_at: float
    late_after: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    radius: int | None = None

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None and self.radius is not None


def issue(kind, *, organization_id, target_id, ttl=DEFAULT_TTL, late_after=None,
          latitude=None, longitude=None, radius=None) -> tuple[str, float]:
    ttl = max(MIN_TTL, min(MAX_TTL, int(ttl)))
    expires_at = time.time() + ttl
    payload = {"k": kind, "o": organization_id, "t": target_id, "e": expires_at}
    if late_after is not None:
        payload["l"] = late_after
    if latitude is not None and longitude is not None and radius is not None:
        payload.update(la=latitude, lo=longitude, r=radius)
    return signing.dumps(payload, salt=_SALT, compress=True), expires_at


def read(raw: str, kind: str) -> Token:
    """The token's contents; 400 if it isn't one of ours, 403 once expired."""
    try:
        payload = signing.loads(raw or "", salt=_SALT)
    except signing.BadSignature:
        raise ServiceError("This isn't a valid attendance QR code.", code="invalid_qr")
    if payload.get("k") != kind:
        raise ServiceError("This isn't a valid attendance QR code.", code="invalid_qr")
    if payload["e"] < time.time():
        raise PermissionDeniedError("This QR code has expired. Scan the one on screen now.",
                                    code="qr_expired")
    return Token(kind, payload["o"], payload["t"], payload["e"], payload.get("l"),
                 payload.get("la"), payload.get("lo"), payload.get("r"))


def check_location(token: Token, latitude, longitude) -> None:
    """Refuse a scan made further than the token's radius from where it was shown."""
    if not token.has_location:
        return
    if latitude is None or longitude is None:
        raise PermissionDeniedError("Turn on location to scan this code.", code="location_required")
    if distance_m(token.latitude, token.longitude, latitude, longitude) > token.radius:
        place = "class" if token.kind == "session" else "campus"
        raise PermissionDeniedError(f"You're too far from the {place} to scan this code.",
                                    code="too_far")


def distance_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in metres (haversine)."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
