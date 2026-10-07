"""'Today' is the school's day, not the server's."""
from datetime import UTC, datetime, time, timedelta
from unittest import mock

from django.utils import timezone

from tests.base import APITestCaseBase
from tests.factories import create_organization, user_with_system_role

API = "/api/v1"


class OrganizationTimezoneTests(APITestCaseBase):
    def setUp(self):
        # 20:00 UTC is already 01:45 the next morning in Kathmandu.
        self.utc_today = datetime.now(UTC).date()
        self.evening = datetime.combine(self.utc_today, time(20, 0), tzinfo=UTC)

    def as_of(self, zone):
        org = create_organization(code=zone.lower().replace("/", "-"), timezone=zone)
        self.authenticate(user_with_system_role(org, "org-admin", email=f"admin@{org.code}.test"))
        with mock.patch("django.utils.timezone.now", return_value=self.evening):
            response = self.client.get(f"{API}/invoices/reports/outstanding/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["as_of"]

    def test_a_school_ahead_of_the_server_is_already_on_tomorrow(self):
        self.assertEqual(str(self.as_of("Asia/Kathmandu")), str(self.utc_today + timedelta(days=1)))

    def test_a_utc_school_is_on_the_servers_day(self):
        self.assertEqual(str(self.as_of("UTC")), str(self.utc_today))

    def test_the_zone_does_not_outlive_the_request(self):
        self.as_of("Asia/Kathmandu")
        self.assertEqual(timezone.get_current_timezone_name(), "UTC")
