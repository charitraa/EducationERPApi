from django.test import TestCase
from rest_framework.test import APIClient

from tests.base import APITestCaseBase
from tests.factories import create_organization, user_with_permissions


class HealthEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_health_is_public(self):
        response = self.client.get("/health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "ok")

    def test_ready_checks_the_database(self):
        response = self.client.get("/ready/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["checks"]["database"], "ok")

    def test_ready_checks_the_cache(self):
        response = self.client.get("/ready/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["checks"]["cache"], "ok")


class ErrorEnvelopeTests(APITestCaseBase):
    """Every failure uses the same response shape so clients can rely on it."""

    def setUp(self):
        self.org = create_organization(code="err-college")
        self.user = user_with_permissions(
            self.org, ["campuses.view", "campuses.create"], email="err@test.edu"
        )
        self.authenticate(self.user)

    def test_validation_error_shape(self):
        response = self.client.post("/api/v1/campuses/", {"name": ""})

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.data)
        self.assertIn("code", response.data["error"])
        self.assertIn("message", response.data["error"])
        self.assertIn("details", response.data["error"])

    def test_not_found_shape(self):
        response = self.client.get("/api/v1/campuses/999999/")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"]["code"], "not_found")

    def test_permission_denied_shape(self):
        response = self.client.delete("/api/v1/campuses/1/")

        self.assertEqual(response.status_code, 403)
        self.assertIn("error", response.data)


class APIVersioningTests(APITestCaseBase):
    def test_v1_prefix_is_live(self):
        response = self.client.get("/api/v1/auth/me/")
        self.assertEqual(response.status_code, 401)  # reached the view, not a 404

    def test_unversioned_path_is_not_served(self):
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 404)


class SchemaTests(APITestCaseBase):
    def test_openapi_schema_generates(self):
        # Docs are staff-only unless API_DOCS_PUBLIC (see tests/test_security.py).
        from tests.factories import create_user

        self.client.force_login(create_user(email="staff@test.edu", is_staff=True))
        response = self.client.get("/api/schema/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"openapi", response.content[:200].lower())


class StableOrderingTests(APITestCaseBase):
    """``?ordering=`` on a column with ties keeps the default order after it."""

    def setUp(self):
        from tests.factories import create_campus, create_student

        self.org = create_organization(code="kmc")
        campus = create_campus(self.org)
        # Created out of order so the primary key can't pass for the tie-break.
        for number, last in (("S-1", "Thapa"), ("S-2", "Basnet"), ("S-3", "Karki")):
            create_student(campus, student_number=number, first_name="Aakriti", last_name=last)
        self.authenticate(user_with_permissions(self.org, ["students.view"]))

    def names(self, query):
        response = self.client.get(f"/api/v1/students/?{query}")
        self.assertEqual(response.status_code, 200, response.data)
        return [s["last_name"] for s in response.data["results"]]

    def test_ties_fall_back_to_the_default_order(self):
        self.assertEqual(self.names("ordering=first_name"), ["Basnet", "Karki", "Thapa"])

    def test_pages_neither_repeat_nor_skip_rows(self):
        pages = [self.names(f"ordering=first_name&page_size=1&page={n}") for n in (1, 2, 3)]

        self.assertEqual(sum(pages, []), ["Basnet", "Karki", "Thapa"])

    def test_descending_keeps_the_tie_break_ascending(self):
        self.assertEqual(self.names("ordering=-first_name"), ["Basnet", "Karki", "Thapa"])

    def test_a_field_the_client_names_is_not_repeated(self):
        self.assertEqual(self.names("ordering=-last_name"), ["Thapa", "Karki", "Basnet"])
