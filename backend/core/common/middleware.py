"""Project-wide HTTP middleware."""
import uuid

from django.utils import timezone

from core.common.logging import reset_request_id, set_request_id
from core.common.timezones import activate_for

REQUEST_ID_HEADER = "X-Request-ID"
_MAX_ID_LENGTH = 64


class RequestIDMiddleware:
    """Give every request an id, and hand it back on the response.

    An id supplied upstream (load balancer, API gateway, the frontend) is
    reused so one trace spans the whole hop; it is length-capped and stripped
    of anything but safe characters, because it is echoed to the client and
    written to the log file. Otherwise a fresh one is generated.

    Sits outermost in MIDDLEWARE so failures anywhere below it are still
    logged with an id.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = self._incoming_id(request) or uuid.uuid4().hex
        request.request_id = request_id
        token = set_request_id(request_id)
        try:
            response = self.get_response(request)
        finally:
            reset_request_id(token)
        response[REQUEST_ID_HEADER] = request_id
        return response

    @staticmethod
    def _incoming_id(request) -> str | None:
        raw = request.META.get("HTTP_X_REQUEST_ID", "")
        cleaned = "".join(
            char for char in raw[:_MAX_ID_LENGTH] if char.isalnum() or char in "-_"
        )
        return cleaned or None


class OrganizationTimezoneMiddleware:
    """Start each request on the default zone and put it back afterwards.

    API calls switch to the organization's zone when DRF authenticates them
    (see ``core.common.timezones``); a session login (the admin site) is
    already known here, so it switches now.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        timezone.deactivate()
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            activate_for(user)
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
