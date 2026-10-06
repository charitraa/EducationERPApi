from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from modules.alumni.models import AlumniProfile, Campaign, Donation, Mentorship
from modules.notices.models import Notice
from modules.notifications.models import Notification
from modules.students.services import change_student_status, place_student
from tests.base import APITestCaseBase
from tests.factories import (
    create_academic_year,
    create_campus,
    create_organization,
    create_program,
    create_section,
    create_student,
    create_user,
    user_with_system_role,
)

API = "/api/v1"


class AlumniTestCase(APITestCaseBase):
    def setUp(self):
        self.org = create_organization(code="kmc")
        self.campus = create_campus(self.org, code="main")
        self.branch = create_campus(self.org, code="branch")
        self.office = user_with_system_role(self.org, "campus-admin", email="office@kmc.test", campus=self.campus)
        self.branch_office = user_with_system_role(self.org, "campus-admin", email="branch@kmc.test",
                                                   campus=self.branch)
        self.program = create_program(self.org)
        self.year = create_academic_year(self.org, name="2082/83")
        self.section = create_section(self.campus, self.program, self.year, level=12, name="A")

        self.sita_user = create_user(self.org, email="sita@kmc.test", user_type="student")
        self.sita = create_student(self.campus, student_number="S-1", first_name="Sita", last_name="Thapa",
                                   user=self.sita_user, phone="9800000001")
        self.hari_user = create_user(self.org, email="hari@kmc.test", user_type="student")
        self.hari = create_student(self.campus, student_number="S-2", first_name="Hari", last_name="KC",
                                   user=self.hari_user)
        for student in (self.sita, self.hari):
            place_student(student=student, section=self.section)
        # Still at school: asks the alumni for mentoring.
        self.gita_user = create_user(self.org, email="gita@kmc.test", user_type="student")
        self.gita = create_student(self.campus, student_number="S-3", first_name="Gita", user=self.gita_user)

    def graduate(self, *students):
        for student in students:
            change_student_status(student=student, status="graduated")
        return [AlumniProfile.objects.get(student=s) for s in students]

    def get(self, user, url, **params):
        self.authenticate(user)
        return self.client.get(f"{API}/alumni/{url}", params)

    def post(self, user, url, body=None):
        self.authenticate(user)
        return self.client.post(f"{API}/alumni/{url}", body or {}, format="json")


class GraduationTests(AlumniTestCase):
    def test_graduating_makes_a_profile_and_an_alumni_login(self):
        self.authenticate(self.office)
        response = self.client.post(f"{API}/students/{self.sita.pk}/change-status/", {"status": "graduated"})
        self.assertEqual(response.status_code, 200, response.data)
        profile = AlumniProfile.objects.get(student=self.sita)
        self.assertEqual((profile.first_name, profile.user, profile.campus, profile.phone),
                         ("Sita", self.sita_user, self.campus, "9800000001"))
        self.assertEqual((profile.program, profile.program_name, profile.level, profile.section_name,
                          profile.academic_year, profile.graduated_on),
                         (self.program, "+2 Science", 12, "A", "2082/83", timezone.localdate()))
        self.sita_user.refresh_from_db()
        self.assertEqual(self.sita_user.user_type, "alumni")

        # The graduate now gets alumni notices, not student ones.
        Notice.objects.create(organization=self.org, title="For students", body=".", audience="students",
                              published_at=timezone.now())
        Notice.objects.create(organization=self.org, title="Reunion", body=".", audience="alumni",
                              published_at=timezone.now())
        self.authenticate(self.sita_user)
        titles = [n["title"] for n in self.client.get(f"{API}/notices/").data["results"]]
        self.assertEqual(titles, ["Reunion"])

    def test_graduate_a_whole_class(self):
        change_student_status(student=self.hari, status="suspended")
        response = self.post(self.office, "profiles/graduate/", {"section": self.section.pk})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["graduated"], 1)
        self.assertEqual([s["student"] for s in response.data["skipped"]], [self.hari.pk])
        self.assertEqual(response.data["skipped"][0]["code"], "invalid_transition")
        self.assertTrue(AlumniProfile.objects.filter(student=self.sita).exists())
        self.assertFalse(AlumniProfile.objects.filter(student=self.hari).exists())

    def test_graduation_rules(self):
        # A campus office graduates only its own campus's classes.
        self.assertEqual(self.post(self.branch_office, "profiles/graduate/",
                                   {"section": self.section.pk}).status_code, 403)
        self.assertEqual(self.post(self.office, "profiles/graduate/", {}).status_code, 400)
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(self.post(self.office, "profiles/graduate/",
                                   {"students": [self.sita.pk], "on_date": tomorrow}).status_code, 400)
        other = create_organization(code="other")
        theirs = create_student(create_campus(other), student_number="X-1")
        self.assertEqual(self.post(self.office, "profiles/graduate/", {"students": [theirs.pk]}).status_code, 400)

    def test_office_adds_older_alumni_and_cannot_delete_a_graduate(self):
        response = self.post(self.office, "profiles/", {"campus": self.campus.pk, "first_name": "Old",
                                                         "last_name": "Timer", "academic_year": "2060/61"})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["student"])
        self.assertEqual(self.client.delete(f"{API}/alumni/profiles/{response.data['id']}/").status_code, 204)
        [profile] = self.graduate(self.sita)
        self.assertEqual(self.client.delete(f"{API}/alumni/profiles/{profile.pk}/").data["error"]["code"], "in_use")
        # Another campus's office doesn't see them.
        self.assertEqual(self.get(self.branch_office, "profiles/").data["results"], [])


class SelfServiceTests(AlumniTestCase):
    def setUp(self):
        super().setUp()
        self.sita_profile, self.hari_profile = self.graduate(self.sita, self.hari)

    def test_my_profile(self):
        self.authenticate(self.sita_user)
        response = self.client.patch(f"{API}/alumni/profiles/me/",
                                     {"city": "Pokhara", "bio": "Engineer", "is_mentor": True,
                                      "first_name": "Changed", "academic_year": "1999"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.sita_profile.refresh_from_db()
        self.assertEqual((self.sita_profile.city, self.sita_profile.is_mentor), ("Pokhara", True))
        # Who they are and what they finished is the office's record.
        self.assertEqual((self.sita_profile.first_name, self.sita_profile.academic_year), ("Sita", "2082/83"))
        # Not an office: the register itself is closed.
        self.assertEqual(self.client.get(f"{API}/alumni/profiles/").status_code, 403)
        self.authenticate(self.gita_user)
        self.assertEqual(self.client.get(f"{API}/alumni/profiles/me/").status_code, 404)

    def test_history_is_kept_by_the_graduate_and_the_office(self):
        job = {"employer": "Nepal Telecom", "title": "Engineer", "start_date": "2024-01-01"}
        response = self.post(self.sita_user, "employments/", job)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["profile"], self.sita_profile.pk)
        pk = response.data["id"]
        # Hari can neither see nor change Sita's, nor add to her record.
        self.assertEqual(self.get(self.hari_user, "employments/").data["results"], [])
        self.assertEqual(self.client.patch(f"{API}/alumni/employments/{pk}/", {"title": "X"},
                                           format="json").status_code, 404)
        self.assertEqual(self.post(self.hari_user, "employments/",
                                   {**job, "profile": self.sita_profile.pk}).status_code, 403)
        # The office keeps everyone's at its campus; another campus's office can't.
        self.authenticate(self.office)
        self.assertEqual(self.client.patch(f"{API}/alumni/employments/{pk}/", {"title": "Senior engineer"},
                                           format="json").status_code, 200)
        self.assertEqual(self.get(self.branch_office, "employments/").data["results"], [])
        self.assertEqual(self.post(self.sita_user, "employments/",
                                   {**job, "start_date": "2030-01-01"}).status_code, 400)
        self.assertEqual(self.post(self.sita_user, "higher-studies/",
                                   {"institution": "TU", "qualification": "MSc", "start_year": 2025}).status_code,
                         201)
        self.assertEqual(self.post(self.sita_user, "achievements/", {"title": "Best paper"}).status_code, 201)
        # The current job shows on the profile.
        self.assertEqual(self.get(self.office, f"profiles/{self.sita_profile.pk}/").data["current_job"]["title"],
                         "Senior engineer")

    def test_directory_lists_only_those_who_chose_it(self):
        AlumniProfile.objects.filter(pk=self.sita_profile.pk).update(directory_visible=True,
                                                                     email="sita@mail.test")
        names = [p["full_name"] for p in self.get(self.hari_user, "profiles/directory/").data["results"]]
        self.assertEqual(names, ["Sita Thapa"])
        entry = self.get(self.hari_user, "profiles/directory/").data["results"][0]
        self.assertNotIn("email", entry)
        self.assertNotIn("phone", entry)
        # Contact details aren't searchable either.
        self.assertEqual(self.get(self.hari_user, "profiles/directory/", search="sita@mail").data["results"], [])
        self.assertEqual(len(self.get(self.hari_user, "profiles/directory/", search="Thapa").data["results"]), 1)
        # A current student isn't in the alumni directory.
        self.assertEqual(self.get(self.gita_user, "profiles/directory/").status_code, 403)


class MentoringTests(AlumniTestCase):
    def setUp(self):
        super().setUp()
        self.sita_profile, self.hari_profile = self.graduate(self.sita, self.hari)
        AlumniProfile.objects.filter(pk=self.sita_profile.pk).update(is_mentor=True, mentor_capacity=1,
                                                                     mentor_topics="Engineering")

    def test_request_accept_capacity_and_end(self):
        mentors = self.get(self.gita_user, "profiles/mentors/").data["results"]
        self.assertEqual([(m["full_name"], m["places_left"]) for m in mentors], [("Sita Thapa", 1)])

        response = self.post(self.gita_user, "mentorships/", {"mentor": self.sita_profile.pk, "topic": "IOE prep"})
        self.assertEqual(response.status_code, 201, response.data)
        pk = response.data["id"]
        self.assertTrue(Notification.objects.filter(recipient=self.sita_user,
                                                    event_type="alumni.mentorship_requested").exists())
        self.assertEqual(self.post(self.gita_user, "mentorships/",
                                   {"mentor": self.sita_profile.pk, "topic": "Again"}).status_code, 409)
        # Only the mentor answers.
        self.assertEqual(self.post(self.gita_user, f"mentorships/{pk}/accept/").status_code, 403)
        self.assertEqual(self.post(self.sita_user, f"mentorships/{pk}/accept/").data["status"], "accepted")

        # Full now: a second mentee can ask, but Sita can't accept.
        second = self.post(self.hari_user, "mentorships/", {"mentor": self.sita_profile.pk, "topic": "Jobs"})
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(self.post(self.sita_user, f"mentorships/{second.data['id']}/accept/")
                         .data["error"]["code"], "at_capacity")
        self.assertEqual(self.get(self.gita_user, "profiles/mentors/").data["results"][0]["places_left"], 0)

        self.assertEqual(self.post(self.gita_user, f"mentorships/{pk}/end/").data["status"], "ended")
        self.assertEqual(self.post(self.sita_user, f"mentorships/{second.data['id']}/accept/").status_code, 200)

    def test_who_sees_mentoring(self):
        self.post(self.gita_user, "mentorships/", {"mentor": self.sita_profile.pk, "topic": "IOE prep"})
        self.assertEqual(len(self.get(self.sita_user, "mentorships/").data["results"]), 1)
        self.assertEqual(len(self.get(self.gita_user, "mentorships/").data["results"]), 1)
        self.assertEqual(self.get(self.hari_user, "mentorships/").data["results"], [])
        self.assertEqual(len(self.get(self.office, "mentorships/").data["results"]), 1)
        self.assertEqual(self.get(self.branch_office, "mentorships/").data["results"], [])

    def test_refusals(self):
        AlumniProfile.objects.filter(pk=self.hari_profile.pk).update(is_mentor=False)
        self.assertEqual(self.post(self.gita_user, "mentorships/", {"mentor": self.hari_profile.pk, "topic": "x"})
                         .data["error"]["code"], "not_a_mentor")
        self.assertEqual(self.post(self.sita_user, "mentorships/", {"mentor": self.sita_profile.pk, "topic": "x"})
                         .data["error"]["code"], "self")
        staff = user_with_system_role(self.org, "staff", email="t@kmc.test")
        self.assertEqual(self.post(staff, "mentorships/", {"mentor": self.sita_profile.pk, "topic": "x"})
                         .status_code, 403)
        self.assertFalse(Mentorship.objects.filter(mentee__isnull=True, student__isnull=True).exists())

    def test_a_mentor_without_an_account_cannot_be_asked(self):
        AlumniProfile.objects.filter(pk=self.hari_profile.pk).update(is_mentor=True, user=None)
        # Not offered in the list either.
        mentors = self.get(self.gita_user, "profiles/mentors/").data["results"]
        self.assertEqual([m["full_name"] for m in mentors], ["Sita Thapa"])
        self.assertEqual(self.post(self.gita_user, "mentorships/", {"mentor": self.hari_profile.pk, "topic": "x"})
                         .data["error"]["code"], "mentor_unreachable")


class EventTests(AlumniTestCase):
    def setUp(self):
        super().setUp()
        self.sita_profile, self.hari_profile = self.graduate(self.sita, self.hari)
        starts = (timezone.now() + timedelta(days=10)).isoformat()
        response = self.post(self.office, "events/", {"campus": self.campus.pk, "title": "Reunion",
                                                       "starts_at": starts, "capacity": 3})
        self.assertEqual(response.status_code, 201, response.data)
        self.event = response.data["id"]

    def test_publish_rsvp_capacity_and_cancel(self):
        self.assertEqual(self.post(self.sita_user, f"events/{self.event}/rsvp/", {"response": "going"})
                         .status_code, 404)  # still a draft
        self.assertEqual(self.post(self.office, f"events/{self.event}/publish/").data["status"], "published")
        self.assertEqual([e["title"] for e in self.get(self.sita_user, "events/upcoming/").data["results"]],
                         ["Reunion"])
        response = self.post(self.sita_user, f"events/{self.event}/rsvp/", {"response": "going", "guests": 1})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.post(self.hari_user, f"events/{self.event}/rsvp/", {"response": "going", "guests": 1})
                         .data["error"]["code"], "full")
        self.assertEqual(self.post(self.hari_user, f"events/{self.event}/rsvp/", {"response": "going"})
                         .status_code, 200)
        # Changing your own answer doesn't count you twice.
        self.assertEqual(self.post(self.sita_user, f"events/{self.event}/rsvp/", {"response": "going", "guests": 1})
                         .status_code, 200)
        self.assertEqual(self.get(self.office, f"events/{self.event}/").data["places_taken"], 3)
        self.assertEqual(len(self.get(self.office, f"events/{self.event}/rsvps/").data), 2)

        self.assertEqual(self.post(self.office, f"events/{self.event}/cancel/", {"reason": "Rain"})
                         .data["status"], "cancelled")
        self.assertTrue(Notification.objects.filter(recipient=self.sita_user,
                                                    event_type="alumni.event_cancelled").exists())
        self.assertEqual(self.get(self.sita_user, "events/upcoming/").data["results"], [])

    def test_campus_and_scope(self):
        self.post(self.office, f"events/{self.event}/publish/")
        # An alumna of another campus doesn't see this campus's event.
        other = AlumniProfile.objects.create(organization=self.org, campus=self.branch, first_name="B",
                                             last_name="B", user=create_user(self.org, email="b@kmc.test",
                                                                             user_type="alumni"))
        self.assertEqual(self.get(other.user, "events/upcoming/").data["results"], [])
        self.assertEqual(self.post(other.user, f"events/{self.event}/rsvp/", {"response": "going"}).status_code, 404)
        # Only an organization-wide role sets up an event for every campus.
        starts = (timezone.now() + timedelta(days=5)).isoformat()
        self.assertEqual(self.post(self.office, "events/", {"title": "All", "starts_at": starts}).status_code, 403)
        # A published event can't be deleted.
        self.authenticate(self.office)
        self.assertEqual(self.client.delete(f"{API}/alumni/events/{self.event}/").status_code, 409)


class DonationTests(AlumniTestCase):
    def setUp(self):
        super().setUp()
        [self.sita_profile] = self.graduate(self.sita)
        response = self.post(self.office, "campaigns/", {"campus": self.campus.pk, "code": "Library-2083",
                                                          "name": "New library", "goal_amount": "500000",
                                                          "starts_on": timezone.localdate().isoformat()})
        self.assertEqual(response.status_code, 201, response.data)
        self.campaign = response.data["id"]
        self.assertEqual(response.data["code"], "library-2083")

    def test_record_refund_and_totals(self):
        response = self.post(self.office, "donations/", {"campus": self.campus.pk, "campaign": self.campaign,
                                                          "donor": self.sita_profile.pk, "amount": "10000",
                                                          "method": "bank", "reference": "TXN-1"})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["receipt_number"], response.data["donor_name"]), ("DON-000001", "Sita Thapa"))
        pk = response.data["id"]
        self.post(self.office, "donations/", {"campus": self.campus.pk, "campaign": self.campaign,
                                               "donor_name": "A well-wisher", "amount": "2500"})
        self.assertEqual(Campaign.objects.get(pk=self.campaign).raised_amount, Decimal("12500.00"))
        self.assertTrue(Notification.objects.filter(recipient=self.sita_user,
                                                    event_type="alumni.donation_received").exists())

        response = self.post(self.office, f"donations/{pk}/refund/", {"amount": "4000", "reason": "Bounced part"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["refunded_amount"], "4000.00")
        self.assertEqual(self.post(self.office, f"donations/{pk}/refund/", {"amount": "6000.01", "reason": "x"})
                         .data["error"]["code"], "over_refund")
        self.assertEqual(Campaign.objects.get(pk=self.campaign).raised_amount, Decimal("8500.00"))
        # Gifts are never edited or deleted, and a campaign with gifts stays.
        self.authenticate(self.office)
        # (403: the permission check runs before the method check.)
        self.assertIn(self.client.patch(f"{API}/alumni/donations/{pk}/", {"amount": "1"}).status_code, (403, 405))
        self.assertIn(self.client.delete(f"{API}/alumni/donations/{pk}/").status_code, (403, 405))
        self.assertEqual(self.client.delete(f"{API}/alumni/campaigns/{self.campaign}/").status_code, 409)
        self.assertEqual(Donation.objects.get(pk=pk).amount, Decimal("10000.00"))

        # The donor sees their own gifts; the open campaigns list shows the total.
        mine = self.get(self.sita_user, "donations/me/").data["results"]
        self.assertEqual([d["receipt_number"] for d in mine], ["DON-000001"])
        self.assertEqual(self.get(self.sita_user, "campaigns/open/").data["results"][0]["raised_amount"], "8500.00")
        self.assertEqual(self.get(self.sita_user, "donations/").status_code, 403)

    def test_campus_rules(self):
        body = {"campus": self.branch.pk, "donor_name": "X", "amount": "100"}
        self.assertEqual(self.post(self.office, "donations/", body).status_code, 403)
        self.assertEqual(self.post(self.branch_office, "donations/", {**body, "campaign": self.campaign})
                         .data["error"]["code"], "wrong_campus")
        self.assertEqual(self.post(self.office, "donations/", {"campus": self.campus.pk, "amount": "100"})
                         .status_code, 400)
        Campaign.objects.filter(pk=self.campaign).update(is_active=False)
        self.assertEqual(self.post(self.office, "donations/", {"campus": self.campus.pk, "campaign": self.campaign,
                                                               "donor_name": "X", "amount": "100"})
                         .data["error"]["code"], "campaign_closed")
