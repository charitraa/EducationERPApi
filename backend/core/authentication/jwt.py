"""Bearer-token login that also switches to the organization's time zone."""
from rest_framework_simplejwt.authentication import JWTAuthentication

from core.common.timezones import activate_for


class OrganizationJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            activate_for(result[0])
        return result
