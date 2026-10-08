"""Every model change ships with its migration.

A model edited without ``makemigrations`` deploys fine until the next
developer runs it and finds someone else's change folded into theirs, or a
deploy pipeline running ``makemigrations --check`` refuses to go out.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase


class MigrationsTests(TestCase):
    def test_no_model_change_is_missing_a_migration(self):
        out = StringIO()
        try:
            call_command("makemigrations", check=True, dry_run=True, stdout=out, stderr=out)
        except SystemExit:
            self.fail(f"Models have changes without a migration; run makemigrations.\n{out.getvalue()}")
