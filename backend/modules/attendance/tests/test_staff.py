"""Staff days from punches, schedules, overrides and gate QR check-in."""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone

from tests.factories import create_calendar_event, create_work_schedule

from ..models import Punch, StaffAttendanceDay
from ..services import record_punch
from .base import API, MONDAY, SUNDAY, TODAY, AttendanceTestCase


def at(day, hhmm):
    hour, minute = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=dt_timezone.utc)


class StaffTestCase(AttendanceTestCase):
    def setUp(self):
        super().setUp()
        self.office_hours = create_work_schedule(self.lalitpur, start="10:00", end="17:00",
                                                 grace_minutes=10, half_day_minutes=240,
                                                 is_default=True)

    def punch(self, staff, when, key=None):
        return record_punch(organization=self.org, campus=staff.campus, staff=staff,
                            punched_at=when, source="manual",
                            dedupe_key=key or f"t:{staff.pk}:{when.isoformat()}")

    def day(self, staff, day=MONDAY):
        return StaffAttendanceDay.objects.get(staff=staff, date=day)


class StaffDayTests(StaffTestCase):
    def test_first_punch_in_last_punch_out(self):
        for hhmm in ("09:55", "13:00", "17:05"):
            self.punch(self.hari, at(MONDAY, hhmm))

        day = self.day(self.hari)

        self.assertEqual(day.status, "present")
        self.assertEqual((day.first_in, day.last_out), (at(MONDAY, "09:55"), at(MONDAY, "17:05")))
        self.assertEqual(day.worked_minutes, 430)

    def test_late_after_the_grace_period(self):
        self.punch(self.hari, at(MONDAY, "10:09"))
        self.punch(self.sita, at(MONDAY, "10:11"))

        self.assertEqual(self.day(self.hari).status, "present")
        self.assertEqual(self.day(self.sita).status, "late")

    def test_leaving_early_is_a_half_day(self):
        self.punch(self.hari, at(MONDAY, "10:00"))
        self.punch(self.hari, at(MONDAY, "12:30"))

        self.assertEqual(self.day(self.hari).status, "half_day")

    def test_one_punch_is_a_check_in_without_a_check_out(self):
        self.punch(self.hari, at(MONDAY, "09:50"))

        day = self.day(self.hari)
        self.assertEqual((day.status, day.last_out, day.worked_minutes), ("present", None, None))

    def test_a_staff_members_own_schedule_wins(self):
        from ..models import StaffWorkSchedule

        morning = create_work_schedule(self.lalitpur, name="Morning", start="06:00", end="10:00",
                                       half_day_minutes=120)
        StaffWorkSchedule.objects.create(organization=self.org, staff=self.hari, schedule=morning)
        self.punch(self.hari, at(MONDAY, "06:30"))

        self.assertEqual(self.day(self.hari).status, "late")

    def test_the_same_punch_twice_counts_once(self):
        _, created = self.punch(self.hari, at(MONDAY, "09:55"), key="dup")
        _, again = self.punch(self.hari, at(MONDAY, "09:55"), key="dup")

        self.assertEqual((created, again), (True, False))
        self.assertEqual(Punch.objects.count(), 1)


class OverrideTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.office)

    def test_the_office_sets_a_day_by_hand(self):
        response = self.client.post(f"{API}/staff-days/", {
            "staff": self.hari.pk, "date": MONDAY.isoformat(), "status": "on_duty",
            "note": "Board exam invigilation",
        })

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["is_override"])

    def test_punches_do_not_change_a_day_set_by_hand(self):
        self.client.post(f"{API}/staff-days/", {"staff": self.hari.pk, "date": MONDAY.isoformat(),
                                               "status": "leave", "note": "Sick"})

        self.punch(self.hari, at(MONDAY, "11:00"))

        self.assertEqual(self.day(self.hari).status, "leave")

    def test_clearing_it_goes_back_to_the_punches(self):
        self.punch(self.hari, at(MONDAY, "11:00"))
        self.client.post(f"{API}/staff-days/", {"staff": self.hari.pk, "date": MONDAY.isoformat(),
                                               "status": "on_duty", "note": "Field trip"})
        day = self.day(self.hari)

        self.assertEqual(self.client.delete(f"{API}/staff-days/{day.pk}/").status_code, 204)
        self.assertEqual(self.day(self.hari).status, "late")

    def test_clearing_a_day_without_punches_removes_it(self):
        self.client.post(f"{API}/staff-days/", {"staff": self.hari.pk, "date": MONDAY.isoformat(),
                                               "status": "leave", "note": "Sick"})

        self.client.delete(f"{API}/staff-days/{self.day(self.hari).pk}/")

        self.assertFalse(StaffAttendanceDay.objects.exists())

    def test_a_day_from_punches_cannot_be_cleared(self):
        self.punch(self.hari, at(MONDAY, "10:00"))

        response = self.client.delete(f"{API}/staff-days/{self.day(self.hari).pk}/")

        self.assertError(response, 409, "not_override")

    def test_a_manual_punch_needs_a_note_and_counts(self):
        # ManualPunchSerializer rejects a future punched_at, unlike the plain
        # record_punch() the other tests in this file call directly — clamp to
        # "now" for the one case where MONDAY is today and 09:58 hasn't happened yet.
        when = min(at(MONDAY, "09:58"), timezone.now() - timedelta(minutes=1))
        missing_note = self.client.post(f"{API}/punches/", {
            "staff": self.hari.pk, "punched_at": when.isoformat()})
        response = self.client.post(f"{API}/punches/", {
            "staff": self.hari.pk, "punched_at": when.isoformat(),
            "note": "Reader was down"})

        self.assertEqual(missing_note.status_code, 400)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["source"], response.data["marked_by"]),
                         ("manual", self.office.pk))
        self.assertEqual(self.day(self.hari).status, "present")

    def test_teachers_cannot_set_days(self):
        self.login(self.hari)

        response = self.client.post(f"{API}/staff-days/", {
            "staff": self.hari.pk, "date": MONDAY.isoformat(), "status": "present", "note": "me"})

        self.assertEqual(response.status_code, 403)


class GateQRTests(StaffTestCase):
    def code(self, **options):
        self.login(self.office)
        response = self.client.post(f"{API}/punches/qr/", {"campus": self.lalitpur.pk, **options})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["token"]

    def test_staff_check_in_by_scanning_the_gate_code(self):
        token = self.code()
        self.login(self.hari)

        response = self.client.post(f"{API}/punches/check-in/", {"token": token})

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["staff"], response.data["source"]), (self.hari.pk, "qr"))
        self.assertTrue(StaffAttendanceDay.objects.filter(staff=self.hari, date=TODAY).exists())

    def test_a_double_tap_is_one_punch(self):
        token = self.code()
        self.login(self.hari)
        self.client.post(f"{API}/punches/check-in/", {"token": token})

        response = self.client.post(f"{API}/punches/check-in/", {"token": token})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Punch.objects.count(), 1)

    def test_a_student_cannot_check_in_as_staff(self):
        from tests.factories import create_user

        token = self.code()
        student_user = create_user(self.org, email="ram.s@kmc.test")
        self.ram.user = student_user
        self.ram.save(update_fields=["user"])
        self.login(student_user)

        self.assertError(self.client.post(f"{API}/punches/check-in/", {"token": token}), 403, "not_staff")

    def test_a_gate_code_scanned_from_afar_names_the_campus(self):
        token = self.code(latitude=27.6588, longitude=85.3247, radius=100)
        self.login(self.hari)

        response = self.client.post(f"{API}/punches/check-in/", {"token": token, "latitude": 27.7172,
                                                                  "longitude": 85.3240})

        self.assertError(response, 403, "too_far")
        self.assertIn("too far from the campus", str(response.data))

    def test_a_session_code_is_not_a_gate_code(self):
        self.login(self.hari)
        session = self.open_lesson(self.physics_a).data["id"]
        token = self.client.post(f"{API}/sessions/{session}/qr/").data["token"]

        response = self.client.post(f"{API}/punches/check-in/", {"token": token})

        self.assertError(response, 400, "invalid_qr")


class StaffReportTests(StaffTestCase):
    def test_absent_counts_working_days_without_any_record(self):
        """Sun–Fri working week; a holiday and Saturday don't count."""
        start = MONDAY - timedelta(days=7)
        end = start + timedelta(days=6)  # Monday to Sunday of last week
        create_calendar_event(self.org, title="Tihar", day=start + timedelta(days=2), kind="holiday",
                              suspends_classes=True)
        self.punch(self.hari, at(start, "09:55"))
        self.punch(self.hari, at(start + timedelta(days=1), "10:30"))
        self.login(self.office)

        response = self.client.get(f"{API}/reports/staff/", {
            "staff": self.hari.pk, "from": start.isoformat(), "to": end.isoformat()})

        self.assertEqual(response.status_code, 200, response.data)
        [row] = response.data["staff"]
        # Mon, Tue, Thu, Fri, Sun are working days (Wed is Tihar, Sat is off).
        self.assertEqual(row["working_days"], 5)
        self.assertEqual((row["present"], row["late"], row["absent"]), (1, 1, 3))

    def test_days_before_joining_are_not_absences(self):
        self.hari.joined_on = MONDAY
        self.hari.save(update_fields=["joined_on"])
        self.login(self.office)

        response = self.client.get(f"{API}/reports/staff/", {
            "staff": self.hari.pk, "from": (MONDAY - timedelta(days=7)).isoformat(),
            "to": (MONDAY - timedelta(days=1)).isoformat()})

        self.assertEqual(response.data["staff"][0]["working_days"], 0)

    def test_my_own_attendance(self):
        self.punch(self.hari, at(MONDAY, "09:55"))
        self.login(self.hari)

        response = self.client.get(f"{API}/staff-days/me/", {"from": SUNDAY.isoformat(),
                                                             "to": MONDAY.isoformat()})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([d["status"] for d in response.data["days"]], ["present"])
