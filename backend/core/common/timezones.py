"""The organization's own time zone, for "today" and for times shown back.

A school in Kathmandu and the server in UTC disagree about the date for 5¾
hours every night. Once a request is authenticated, the caller's
organization's zone is activated, so every ``timezone.localdate()`` and
``timezone.localtime()`` in the request means *the school's* day. Requests
without an organization (anonymous, the platform admin) stay on
``settings.TIME_ZONE``. ``OrganizationTimezoneMiddleware`` resets it after
each request so a reused worker thread never carries one school's zone into
the next request.
"""
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone


def organization_zone(organization) -> ZoneInfo | None:
    name = getattr(organization, "timezone", None)
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def activate_for(user) -> None:
    """Use the user's organization's zone for the rest of this request."""
    zone = organization_zone(getattr(user, "organization", None)) if user is not None else None
    if zone is not None:
        timezone.activate(zone)
