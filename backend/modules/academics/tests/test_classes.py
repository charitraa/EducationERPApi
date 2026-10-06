"""Campus-level classes: rooms, batches, sections and teaching assignments."""
from django.db import IntegrityError, transaction

from tests.base import APITestCaseBase
from tests.factories import (
    add_to_curriculum,
    create_academic_year,
    create_campus,
    create_organization,
    create_program,
    create_section,
    create_staff_member,
    create_student,
    create_subject,
    user_with_permissions,
    user_with_system_role,
)

from ..models import Room, Section

API = "/api/v1"
CLASSES = ["academics.view", "academics.manage_classes"]


class ClassesTestCase(APITestCaseBase):
    def setUp(self):
        self.org = create_organization(code="kmc")
        self.lalitpur = create_campus(self.org, code="lalitpur", name="Lalitpur")
        self.bhaktapur = create_campus(self.org, code="bhaktapur", name="Bhaktapur")
        self.year = create_academic_year(self.org)
        self.program = create_program(self.org)  # Grade 11–12
        self.authenticate(user_with_permissions(self.org, CLASSES, email="office@kmc.test"))

    def section_payload(self, **overrides):
        return {"academic_year": self.year.pk, "campus": self.lalitpur.pk,
                "program": self.program.pk, "level": 11, "name": "A", **overrides}


class RoomTests(ClassesTestCase):
    def test_create_and_codes_unique_per_campus(self):
        first = self.client.post(f"{API}/rooms/", {"campus": self.lalitpur.pk, "code": "R101", "name": "Room 101", "capacity": 40})
        same_campus = self.client.post(f"{API}/rooms/", {"campus": self.lalitpur.pk, "code": "r101", "name": "Dup"})
        other_campus = self.client.post(f"{API}/rooms/", {"campus": self.bhaktapur.pk, "code": "r101", "name": "Room 101"})

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(same_campus.status_code, 400)
        self.assertEqual(other_campus.status_code, 201)

    def test_a_room_cannot_move_campus(self):
        room = Room.objects.create(organization=self.org, campus=self.lalitpur, code="r1", name="R1")

        response = self.client.patch(f"{API}/rooms/{room.pk}/", {"campus": self.bhaktapur.pk})

        self.assertEqual(response.status_code, 400)


class SectionTests(ClassesTestCase):
    def test_create_a_section(self):
        teacher = create_staff_member(self.lalitpur)

        response = self.client.post(f"{API}/sections/", self.section_payload(class_teacher=teacher.pk))

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["display_name"], "Grade 11 A")
        self.assertEqual(response.data["student_count"], 0)

    def test_level_must_exist_in_the_program(self):
        response = self.client.post(f"{API}/sections/", self.section_payload(level=10))

        self.assertEqual(response.status_code, 400)
        self.assertIn("level", response.data["error"]["details"])

    def test_duplicate_section_is_rejected_and_backed_by_the_database(self):
        self.client.post(f"{API}/sections/", self.section_payload())

        self.assertEqual(self.client.post(f"{API}/sections/", self.section_payload()).status_code, 400)
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_section(self.lalitpur, self.program, self.year)

    def test_same_name_is_fine_at_another_campus_or_level(self):
        self.client.post(f"{API}/sections/", self.section_payload())

        other_campus = self.client.post(f"{API}/sections/", self.section_payload(campus=self.bhaktapur.pk))
        other_level = self.client.post(f"{API}/sections/", self.section_payload(level=12))

        self.assertEqual((other_campus.status_code, other_level.status_code), (201, 201))

    def test_room_teacher_and_batch_must_match_the_campus(self):
        far_room = Room.objects.create(organization=self.org, campus=self.bhaktapur, code="r1", name="R1")
        far_teacher = create_staff_member(self.bhaktapur)

        room = self.client.post(f"{API}/sections/", self.section_payload(home_room=far_room.pk))
        teacher = self.client.post(f"{API}/sections/", self.section_payload(class_teacher=far_teacher.pk))

        self.assertIn("home_room", room.data["error"]["details"])
        self.assertIn("class_teacher", teacher.data["error"]["details"])

    def test_placement_fields_are_locked_once_students_are_in(self):
        section = create_section(self.lalitpur, self.program, self.year)
        from modules.students.services import place_student

        place_student(student=create_student(self.lalitpur), section=section)

        moved = self.client.patch(f"{API}/sections/{section.pk}/", {"level": 12})
        renamed = self.client.patch(f"{API}/sections/{section.pk}/", {"capacity": 45})

        self.assertEqual(moved.status_code, 400)
        self.assertEqual(renamed.status_code, 200)

    def test_list_students_and_count(self):
        from modules.students.services import place_student

        section = create_section(self.lalitpur, self.program, self.year)
        for number in ("S-1", "S-2"):
            place_student(student=create_student(self.lalitpur, student_number=number), section=section)
        self.authenticate(user_with_system_role(self.org, "campus-admin", email="h@kmc.test"))

        detail = self.client.get(f"{API}/sections/{section.pk}/")
        students = self.client.get(f"{API}/sections/{section.pk}/students/")

        self.assertEqual(detail.data["student_count"], 2)
        self.assertEqual([s["student_number"] for s in students.data], ["S-1", "S-2"])

    def test_a_section_with_placement_history_cannot_be_deleted(self):
        from modules.students.services import place_student

        section = create_section(self.lalitpur, self.program, self.year)
        place_student(student=create_student(self.lalitpur), section=section)

        self.assertEqual(self.client.delete(f"{API}/sections/{section.pk}/").status_code, 409)

    def test_search_by_the_name_shown(self):
        create_section(self.lalitpur, self.program, self.year, level=11, name="A")
        create_section(self.lalitpur, self.program, self.year, level=12, name="A")

        found = self.client.get(f"{API}/sections/", {"search": "Grade 11 A"})

        self.assertEqual(found.status_code, 200)
        self.assertEqual([s["display_name"] for s in found.data["results"]], ["Grade 11 A"])


class BatchTests(ClassesTestCase):
    def test_create_a_batch_and_attach_a_section(self):
        batch = self.client.post(f"{API}/batches/", {
            "code": "csit-2082", "name": "BSc CSIT 2082", "program": self.program.pk,
            "campus": self.lalitpur.pk, "start_year": self.year.pk,
        })
        section = self.client.post(f"{API}/sections/", self.section_payload(batch=batch.data["id"]))
        wrong_campus = self.client.post(f"{API}/sections/", self.section_payload(
            campus=self.bhaktapur.pk, batch=batch.data["id"]))

        self.assertEqual(batch.status_code, 201, batch.data)
        self.assertEqual(section.status_code, 201, section.data)
        self.assertEqual(wrong_campus.status_code, 400)


class TeachingAssignmentTests(ClassesTestCase):
    def setUp(self):
        super().setUp()
        self.section = create_section(self.lalitpur, self.program, self.year)
        self.physics = create_subject(self.org)
        self.teacher = create_staff_member(self.lalitpur)

    def assign(self, **overrides):
        data = {"section": self.section.pk, "subject": self.physics.pk, "teacher": self.teacher.pk, **overrides}
        return self.client.post(f"{API}/teaching-assignments/", data)

    def test_subject_must_be_in_the_curriculum(self):
        before = self.assign()
        add_to_curriculum(self.program, self.physics, level=11)
        after = self.assign()

        self.assertEqual(before.status_code, 400)
        self.assertIn("curriculum", str(before.data["error"]["details"]["subject"]))
        self.assertEqual(after.status_code, 201, after.data)

    def test_a_teacher_who_left_cannot_be_assigned(self):
        add_to_curriculum(self.program, self.physics, level=11)
        gone = create_staff_member(self.lalitpur, employee_number="E-9", status="left", left_on="2025-01-01")

        self.assertEqual(self.assign(teacher=gone.pk).status_code, 400)

    def test_curriculum_entry_in_use_cannot_be_removed(self):
        entry = add_to_curriculum(self.program, self.physics, level=11)
        self.assign()
        self.authenticate(user_with_system_role(self.org, "org-admin", email="p@kmc.test"))

        self.assertEqual(self.client.delete(f"{API}/curriculum/{entry.pk}/").status_code, 409)


class CampusScopeTests(ClassesTestCase):
    """A campus-admin for Lalitpur runs Lalitpur's classes only."""

    def setUp(self):
        super().setUp()
        self.here = create_section(self.lalitpur, self.program, self.year, name="A")
        self.there = create_section(self.bhaktapur, self.program, self.year, name="A")
        self.authenticate(user_with_system_role(self.org, "campus-admin", email="ram@kmc.test", campus=self.lalitpur))

    def test_lists_only_own_campus(self):
        response = self.client.get(f"{API}/sections/")

        self.assertEqual([s["id"] for s in response.data["results"]], [self.here.pk])
        self.assertEqual(self.client.get(f"{API}/sections/{self.there.pk}/").status_code, 404)

    def test_cannot_create_at_another_campus(self):
        response = self.client.post(f"{API}/sections/", self.section_payload(campus=self.bhaktapur.pk, name="B"))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Section.objects.filter(campus=self.bhaktapur, name="B").exists())

    def test_teaching_assignments_follow_the_section_campus(self):
        physics = create_subject(self.org)
        add_to_curriculum(self.program, physics, level=11)
        teacher = create_staff_member(self.lalitpur)

        here = self.client.post(f"{API}/teaching-assignments/",
                                {"section": self.here.pk, "subject": physics.pk, "teacher": teacher.pk})
        there = self.client.post(f"{API}/teaching-assignments/",
                                 {"section": self.there.pk, "subject": physics.pk, "teacher": teacher.pk})

        self.assertEqual(here.status_code, 201, here.data)
        self.assertEqual(there.status_code, 403)

    def test_other_organizations_classes_are_invisible(self):
        foreign_org = create_organization(code="other")
        foreign = create_section(
            create_campus(foreign_org, code="main"), create_program(foreign_org), create_academic_year(foreign_org)
        )

        self.assertEqual(self.client.get(f"{API}/sections/{foreign.pk}/").status_code, 404)
        payload = self.section_payload(program=foreign.program_id, name="Z")
        self.assertEqual(self.client.post(f"{API}/sections/", payload).status_code, 400)
