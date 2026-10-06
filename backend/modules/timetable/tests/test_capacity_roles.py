"""Real life: rooms too small for the class, and one subject with several
teachers (theory and lab, lab groups in parallel, co-teaching)."""
from tests.factories import create_staff_member, create_student, create_teaching_assignment

from modules.students.services import place_student

from .test_entries import API, MONDAY, TimetableTestCase


class RoomCapacityTests(TimetableTestCase):
    def setUp(self):
        super().setUp()
        for n in range(3):
            place_student(student=create_student(self.lalitpur, student_number=f"A-{n}"), section=self.section_a)
        for n in range(2):
            place_student(student=create_student(self.lalitpur, student_number=f"B-{n}"), section=self.section_b)
        self.r101.capacity = 4
        self.r101.save()

    def test_a_class_that_fits(self):
        self.assertEqual(self.schedule(self.a_physics, self.p1, room=self.r101.pk).status_code, 201)

    def test_a_combined_class_too_big_for_the_room(self):
        lesson = self.schedule(self.a_physics, self.p1, room=self.r101.pk).data["id"]

        refused = self.client.post(f"{API}/timetable/", {"teaching_assignment": self.b_physics.pk,
                                                          "combine_with": lesson})
        allowed = self.client.post(f"{API}/timetable/", {"teaching_assignment": self.b_physics.pk,
                                                          "combine_with": lesson, "allow_over_capacity": True})

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.data["error"]["code"], "room_too_small")
        self.assertIn("5 students", refused.data["error"]["message"])
        self.assertNotIn("allow_over_capacity", refused.data["error"]["message"])
        self.assertEqual(allowed.status_code, 201, allowed.data)

    def test_an_elective_counts_only_the_students_who_take_it(self):
        from modules.academics.models import StudentElective
        from modules.students.models import Enrollment

        computer = create_teaching_assignment(self.section_a, self.computer, self.sita)
        self.r102.capacity = 1
        self.r102.save()
        enrollment = Enrollment.objects.filter(section=self.section_a).first()
        StudentElective.objects.create(organization=self.org, enrollment=enrollment, subject=self.computer,
                                       started_on=enrollment.started_on)

        self.assertEqual(self.schedule(computer, self.p1, room=self.r102.pk).status_code, 201)

    def test_moving_to_a_smaller_room(self):
        lesson = self.schedule(self.a_physics, self.p1, room=self.r102.pk).data["id"]
        self.r101.capacity = 2
        self.r101.save()

        response = self.client.patch(f"{API}/timetable/{lesson}/", {"room": self.r101.pk})

        self.assertEqual(response.status_code, 409)


class TeachingRoleTests(TimetableTestCase):
    def test_theory_and_lab_by_different_teachers(self):
        response = self.client.post(f"{API}/teaching-assignments/", {
            "section": self.section_a.pk, "subject": self.physics.pk, "teacher": self.sita.pk,
            "role": "practical"})

        self.assertEqual(response.status_code, 201, response.data)

    def test_one_teacher_can_take_theory_and_lab(self):
        response = self.client.post(f"{API}/teaching-assignments/", {
            "section": self.section_a.pk, "subject": self.physics.pk, "teacher": self.hari.pk,
            "role": "practical"})

        self.assertEqual(response.status_code, 201, response.data)

    def test_lab_groups_run_in_parallel(self):
        lab_1 = create_teaching_assignment(self.section_a, self.physics, self.sita, role="practical")
        lab_2 = create_teaching_assignment(
            self.section_a, self.physics, create_staff_member(self.lalitpur, employee_number="E-7"), role="practical")

        first = self.schedule(lab_1, self.p1, room=self.r101.pk)
        second = self.schedule(lab_2, self.p1, room=self.r102.pk)
        # ...but a lecture can't run alongside them.
        lecture = self.schedule(self.a_physics, self.p1, room=None)

        self.assertEqual((first.status_code, second.status_code), (201, 201), second.data)
        self.assertClash(lecture, "section")

    def test_a_co_teacher_shares_the_room(self):
        co = create_teaching_assignment(self.section_a, self.physics, self.sita, role="co_teaching")
        self.schedule(self.a_physics, self.p1, room=self.r101.pk)

        self.assertEqual(self.schedule(co, self.p1, room=self.r101.pk).status_code, 201)

    def test_a_retired_assignment_gets_no_new_lessons(self):
        self.a_physics.is_active = False
        self.a_physics.save()

        response = self.schedule(self.a_physics, self.p1)

        self.assertEqual(response.status_code, 400)
        self.assertIn("no longer active", str(response.data))
