from django.apps import AppConfig


class AuthenticationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core.authentication"
    label = "authentication"
    verbose_name = "Authentication"

    def ready(self):
        from . import schema  # noqa: F401  (documents the bearer scheme in OpenAPI)
