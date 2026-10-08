from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme


class OrganizationJWTScheme(SimpleJWTScheme):
    """The same bearer scheme as plain simplejwt; only the class differs."""

    target_class = "core.authentication.jwt.OrganizationJWTAuthentication"
