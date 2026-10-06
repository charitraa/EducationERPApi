"""Real life: moves recorded ahead of their date, and class capacity."""
from datetime import timedelta

from django.utils import timezone

from tests.base import APITestCaseBase
from tests.factories import (
    add_to_curriculum,
    create_academic_year,
    create_campus,
    create_organization,
    create_program,
    create_section,
    create_student,
    create_subject,
    user_with_system_role,
)

from modules.academics.models import StudentElective
from modules.students.models import Enrollment
from modules.students.selectors import get_current_enrollment, students_in_section
from modules.students.services import change_student_status, place_student

API = "/api/v1"


class ScheduledMoveTests(APITestCaseBase):
    """Chaitra: the office records next year's promotions. Baisakh 1 they
    take effect. Until then, everyone is still in this year's class."""

    def setUp(self):
        self.org = create_organization(code="kmc")
        self.campus = create_campus(self.org, code="main")
        self.year = create_academic_year(self.org)
        self.next_year = create_academic_year(self.org, name="2083/84", start=self.year.end_date + timedelta(days=1))
        self.program = create_program(self.org)
        self.grade_11 = create_section(self.campus, self.program, self.year, level=11, name="A")
        self.grade_12 = create_section(self.campus, self.program, self.next_year, level=12, name="A")
        self.ram = create_student(self.campus, student_number="S-1", admitted_on=timezone.localdate() - timedelta(days=30))
        place_student(student=self.ram, section=self.grade_11)
        self.new_year_day = self.next_year.start_date
        self.authenticate(user_with_system_role(self.org, "org-admin", email="p@kmc.test"))

    def promote(self, **extra):
        return self.client.post(f"{API}/students/{self.ram.pk}/place/", {
            "section": self.grade_12.pk, "on_date": self.new_year_day.isoformat(), **extra})

    def test_the_student_stays_in_class_until_the_date(self):
        self.assertEqual(self.promote().status_code, 200)

        self.assertIn(self.ram, students_in_section(self.grade_11))
        self.assertNotIn(self.ram, students_in_section(self.grade_12))
        self.assertEqual(get_current_enrollment(self.ram).section, self.grade_11)
        # On the day, they're in Grade 12.
        self.assertIn(self.ram, students_in_section(self.grade_12, on=self.new_year_day))
        self.assertNotIn(self.ram, students_in_section(self.grade_11, on=self.new_year_day))
        self.assertEqual(self.client.get(f"{API}/sections/{self.grade_11.pk}/").data["student_count"], 1)

    def test_a_scheduled_move_can_be_revised(self):
        grade_12_b = create_section(self.campus, self.program, self.next_year, level=12, name="B")
        self.promote()

        place_student(student=self.ram, section=grade_12_b, on_date=self.new_year_day)

        self.assertEqual(Enrollment.objects.filter(student=self.ram).count(), 2)
        self.assertIn(self.ram, students_in_section(grade_12_b, on=self.new_year_day))

    def test_placing_back_cancels_the_scheduled_move(self):
        self.promote()

        place_student(student=self.ram, section=self.grade_11)

        enrollment = Enrollment.objects.get(student=self.ram)
        self.assertEqual((enrollment.section, enrollment.status, enrollment.ended_on),
                         (self.grade_11, "active", None))

    def test_a_scheduled_move_blocks_transfer_or_withdrawal_until_cancelled(self):
        self.promote()

        response = self.client.post(f"{API}/students/{self.ram.pk}/change-status/", {"status": "withdrawn"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "scheduled_move")

    def test_promoting_a_whole_class_ahead(self):
        response = self.client.post(f"{API}/sections/{self.grade_11.pk}/promote/", {
            "to_section": self.grade_12.pk, "on_date": self.new_year_day.isoformat()}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(self.ram, students_in_section(self.grade_11))

    def test_a_student_admitted_mid_year_isnt_in_class_before(self):
        late = create_student(self.campus, student_number="S-2", admitted_on=timezone.localdate())
        place_student(student=late, section=self.grade_11)

        self.assertNotIn(late, students_in_section(self.grade_11, on=timezone.localdate() - timedelta(days=1)))
        self.assertIn(late, students_in_section(self.grade_11))

    def test_history_says_which_class_on_any_date(self):
        grade_11_b = create_section(self.campus, self.program, self.year, level=11, name="B")
        yesterday = timezone.localdate() - timedelta(days=1)
        place_student(student=self.ram, section=grade_11_b, on_date=yesterday)

        self.assertEqual(get_current_enrollment(self.ram, on=yesterday - timedelta(days=1)).section, self.grade_11)
        self.assertEqual(get_current_enrollment(self.ram).section, grade_11_b)


class CapacityTests(APITestCaseBase):
    def setUp(self):
        self.org = create_organization(code="kmc")
        self.campus = create_campus(self.org, code="main")
        self.year = create_academic_year(self.org)
        self.program = create_program(self.org)
        self.full = create_section(self.campus, self.program, self.year, name="A", capacity=1)
        self.other = create_section(self.campus, self.program, self.year, name="B")
        place_student(student=create_student(self.campus, student_number="S-1"), section=self.full)
        self.newcomer = create_student(self.campus, student_number="S-2")
        self.authenticate(user_with_system_role(self.org, "org-admin", email="p@kmc.test"))

    def place(self, **extra):
        return self.client.post(f"{API}/students/{self.newcomer.pk}/place/", {"section": self.full.pk, **extra})

    def test_a_full_class_refuses_unless_told(self):
        refused = self.place()
        allowed = self.place(allow_over_capacity=True)

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.data["error"]["code"], "over_capacity")
        # Shown to office staff as-is, so no API field names.
        self.assertNotIn("allow_over_capacity", refused.data["error"]["message"])
        self.assertEqual(allowed.status_code, 200, allowed.data)

    def test_promotion_into_a_full_class(self):
        place_student(student=self.newcomer, section=self.other)
        create_student(self.campus, student_number="S-3")

        response = self.client.post(f"{API}/sections/{self.other.pk}/promote/",
                                    {"to_section": self.full.pk}, format="json")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["details"]["students"][0]["code"], "over_capacity")


class ElectiveHistoryTests(APITestCaseBase):
    """A student drops an elective mid-year: old records still show they
    took it, new ones don't."""

    def setUp(self):
        self.org = create_organization(code="kmc")
        self.campus = create_campus(self.org, code="main")
        self.year = create_academic_year(self.org)
        self.program = create_program(self.org)
        self.computer = create_subject(self.org, code="computer", name="Computer")
        add_to_curriculum(self.program, self.computer, level=11, is_elective=True)
        self.section = create_section(self.campus, self.program, self.year)
        self.ram = create_student(self.campus, student_number="S-1", admitted_on=timezone.localdate() - timedelta(days=30))
        place_student(student=self.ram, section=self.section)
        enrollment = Enrollment.objects.get(student=self.ram)
        self.choice = StudentElective.objects.create(
            organization=self.org, enrollment=enrollment, subject=self.computer,
            started_on=enrollment.started_on,
        )
        self.authenticate(user_with_system_role(self.org, "org-admin", email="p@kmc.test"))

    def test_dropping_keeps_the_history(self):
        from modules.academics.selectors import students_taking

        response = self.client.delete(f"{API}/student-electives/{self.choice.pk}/")

        self.assertEqual(response.status_code, 204)
        self.choice.refresh_from_db()
        self.assertEqual(self.choice.ended_on, timezone.localdate())
        yesterday = timezone.localdate() - timedelta(days=1)
        self.assertIn(self.ram, students_taking(self.section, self.computer.pk, on=yesterday))
        self.assertNotIn(self.ram, students_taking(self.section, self.computer.pk))

    def test_taking_it_up_again(self):
        self.client.delete(f"{API}/student-electives/{self.choice.pk}/")

        again = self.client.post(f"{API}/student-electives/", {"student": self.ram.pk, "subject": self.computer.pk})

        self.assertEqual(again.status_code, 201, again.data)

    def test_withdrawn_student_keeps_their_choices_as_history(self):
        change_student_status(student=self.ram, status="withdrawn")

        self.assertTrue(StudentElective.objects.filter(pk=self.choice.pk).exists())
        self.assertFalse(StudentElective.objects.on().filter(pk=self.choice.pk).exists())
