from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.common.exceptions import ConflictError, ServiceError
from modules.library import services
from modules.library.models import (
    Author,
    Book,
    Category,
    Copy,
    CopyStatus,
    Fine,
    FineStatus,
    Issue,
    IssueStatus,
    Member,
    Publisher,
    Reservation,
    ReservationStatus,
    Shelf,
)
from modules.notifications.models import Notification
from tests.base import APITestCaseBase
from core.permissions.models import Role
from tests.factories import create_campus, create_organization, create_staff_member, create_student, create_user, \
    grant, user_with_system_role

API = "/api/v1"


class LibraryTestCase(APITestCaseBase):
    def setUp(self):
        self.org = create_organization(code="kmc")
        self.campus = create_campus(self.org, code="main")
        self.other_campus = create_campus(self.org, code="branch")
        self.office = user_with_system_role(self.org, "campus-admin", email="office@kmc.test", campus=self.campus)

        self.ram_user = create_user(self.org, email="ram@kmc.test", user_type="student")
        self.ram = create_student(self.campus, student_number="S-1", first_name="Ram", last_name="Thapa",
                                  user=self.ram_user)
        self.sita_user = create_user(self.org, email="sita@kmc.test", user_type="student")
        self.sita = create_student(self.campus, student_number="S-2", first_name="Sita", last_name="Rai",
                                   user=self.sita_user)

        self.author = Author.objects.create(organization=self.org, name="Amar Neupane")
        self.category = Category.objects.create(organization=self.org, code="fiction", name="Fiction")
        self.book = Book.objects.create(organization=self.org, title="Seto Dharti", category=self.category)
        self.book.authors.add(self.author)
        self.shelf = Shelf.objects.create(organization=self.org, campus=self.campus, code="A1")
        self.copy = Copy.objects.create(organization=self.org, book=self.book, campus=self.campus,
                                        shelf=self.shelf, accession_number="ACC-000001", price="500.00")

        self.ram_member = services.create_member(campus=self.campus, student=self.ram)
        self.sita_member = services.create_member(campus=self.campus, student=self.sita)

    def login(self, who):
        self.logout()
        self.authenticate(who)

    def assertError(self, response, status, code):
        self.assertEqual(response.status_code, status, response.data)
        self.assertEqual(response.data["error"]["code"], code, response.data)


class CatalogTests(LibraryTestCase):
    def test_anyone_signed_in_can_browse_books(self):
        self.login(self.ram_user)
        r = self.client.get(f"{API}/library/books/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["results"][0]["available_count"], 1)

    def test_a_student_cannot_create_a_book(self):
        self.login(self.ram_user)
        r = self.client.post(f"{API}/library/books/", {"title": "New Book"})
        self.assertEqual(r.status_code, 403)

    def test_the_office_creates_a_book(self):
        self.login(self.office)
        r = self.client.post(f"{API}/library/books/", {"title": "New Book", "category": self.category.pk})
        self.assertEqual(r.status_code, 201, r.data)

    def test_duplicate_isbn_in_the_same_org_is_refused_at_the_database(self):
        Book.objects.create(organization=self.org, title="A", isbn="111")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Book.objects.create(organization=self.org, title="B", isbn="111")

    def test_the_office_adds_a_copy_and_the_accession_number_is_generated(self):
        self.login(self.office)
        r = self.client.post(f"{API}/library/copies/", {"book": self.book.pk, "campus": self.campus.pk})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["accession_number"], "ACC-000002")
        self.assertEqual(r.data["status"], "available")

    def test_a_copy_cannot_sit_on_another_campus_shelf(self):
        far_shelf = Shelf.objects.create(organization=self.org, campus=self.other_campus, code="B1")
        self.login(self.office)
        r = self.client.post(f"{API}/library/copies/", {"book": self.book.pk, "campus": self.campus.pk,
                                                          "shelf": far_shelf.pk})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("shelf", r.data["error"]["details"])
        r = self.client.patch(f"{API}/library/copies/{self.copy.pk}/", {"shelf": far_shelf.pk})
        self.assertEqual(r.status_code, 400, r.data)

    def test_a_shelf_with_copies_cannot_move_campus(self):
        grant(self.office, Role.objects.get(code="campus-admin", organization=None), campus=self.other_campus)
        self.login(self.office)
        r = self.client.patch(f"{API}/library/shelves/{self.shelf.pk}/", {"campus": self.other_campus.pk})
        self.assertEqual(r.status_code, 400, r.data)
        self.copy.shelf = None
        self.copy.save(update_fields=["shelf"])
        r = self.client.patch(f"{API}/library/shelves/{self.shelf.pk}/", {"campus": self.other_campus.pk})
        self.assertEqual(r.status_code, 200, r.data)

    def test_a_shelf_code_is_unique_per_campus(self):
        self.login(self.office)
        r = self.client.post(f"{API}/library/shelves/", {"campus": self.campus.pk, "code": "A1"})
        self.assertEqual(r.status_code, 400)


class MembershipTests(LibraryTestCase):
    def test_a_student_membership_gets_student_defaults(self):
        self.assertEqual(self.ram_member.max_books, 3)
        self.assertEqual(self.ram_member.loan_period_days, 14)
        self.assertEqual(self.ram_member.member_number, "LM-000001")

    def test_a_staff_membership_gets_staff_defaults(self):
        staff = create_staff_member(self.campus, employee_number="E-1", first_name="Hari")
        member = services.create_member(campus=self.campus, staff=staff)
        self.assertEqual(member.max_books, 5)
        self.assertEqual(member.membership_type, "staff")

    def test_exactly_one_profile_is_required(self):
        with self.assertRaises(ServiceError):
            services.create_member(campus=self.campus)

    def test_a_student_cannot_have_two_memberships(self):
        with self.assertRaises(ConflictError):
            services.create_member(campus=self.campus, student=self.ram)

    def test_creating_a_second_membership_over_the_api_is_a_409_not_a_500(self):
        self.login(self.office)
        r = self.client.post(f"{API}/library/members/", {"campus": self.campus.pk, "student": self.ram.pk})
        self.assertError(r, 409, "already_member")

    def test_a_member_sees_their_own_membership(self):
        self.login(self.ram_user)
        r = self.client.get(f"{API}/library/members/me/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["id"], self.ram_member.pk)

    def test_a_student_cannot_list_members(self):
        self.login(self.ram_user)
        self.assertEqual(self.client.get(f"{API}/library/members/").status_code, 403)


class IssueTests(LibraryTestCase):
    def issue(self):
        self.login(self.office)
        return self.client.post(f"{API}/library/issues/", {"copy": self.copy.pk, "member": self.ram_member.pk})

    def test_the_office_issues_a_copy(self):
        r = self.issue()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["status"], "issued")
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "issued")

    def test_a_student_cannot_issue_a_book_themselves(self):
        self.login(self.ram_user)
        r = self.client.post(f"{API}/library/issues/", {"copy": self.copy.pk, "member": self.ram_member.pk})
        self.assertEqual(r.status_code, 403)

    def test_an_already_issued_copy_cannot_be_issued_again(self):
        self.issue()
        r = self.client.post(f"{API}/library/issues/", {"copy": self.copy.pk, "member": self.sita_member.pk})
        self.assertError(r, 409, "not_available")

    def test_the_max_books_limit_is_enforced(self):
        self.ram_member.max_books = 1
        self.ram_member.save(update_fields=["max_books"])
        self.issue()
        second_copy = Copy.objects.create(organization=self.org, book=self.book, campus=self.campus,
                                          accession_number="ACC-999999")
        r = self.client.post(f"{API}/library/issues/", {"copy": second_copy.pk, "member": self.ram_member.pk})
        self.assertError(r, 409, "limit_reached")

    def test_a_copy_at_another_campus_cannot_be_issued(self):
        # Office holds the desk permission at both campuses here, so the
        # failure below is the business rule, not a scoping refusal.
        grant(self.office, Role.objects.get(code="campus-admin", organization=None), campus=self.other_campus)
        self.login(self.office)
        far_copy = Copy.objects.create(organization=self.org, book=self.book, campus=self.other_campus,
                                       accession_number="ACC-777777")
        r = self.client.post(f"{API}/library/issues/", {"copy": far_copy.pk, "member": self.ram_member.pk})
        self.assertError(r, 400, "wrong_campus")

    def test_returning_on_time_frees_the_copy_and_raises_no_fine(self):
        issue_id = self.issue().data["id"]
        r = self.client.post(f"{API}/library/issues/{issue_id}/return/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], "returned")
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "available")
        self.assertFalse(Fine.objects.filter(issue_id=issue_id).exists())

    def test_returning_late_raises_an_overdue_fine(self):
        issue_id = self.issue().data["id"]
        issue = Issue.objects.get(pk=issue_id)
        issue.due_at = timezone.now() - timedelta(days=3)
        issue.save(update_fields=["due_at"])
        r = self.client.post(f"{API}/library/issues/{issue_id}/return/")
        self.assertEqual(r.status_code, 200)
        fine = Fine.objects.get(issue_id=issue_id)
        self.assertEqual(fine.category, "overdue")
        self.assertEqual(fine.amount, self.ram_member.daily_fine_rate * 3)

    def test_reporting_a_copy_lost_fines_its_price(self):
        issue_id = self.issue().data["id"]
        r = self.client.post(f"{API}/library/issues/{issue_id}/return/", {"outcome": "lost"})
        self.assertEqual(r.status_code, 200)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "lost")
        fine = Fine.objects.get(issue_id=issue_id, category="lost")
        self.assertEqual(fine.amount, self.copy.price)

    def test_reporting_lost_needs_a_price_on_the_copy(self):
        self.login(self.office)
        priceless = Copy.objects.create(organization=self.org, book=self.book, campus=self.campus,
                                        accession_number="ACC-000003")
        r = self.client.post(f"{API}/library/issues/", {"copy": priceless.pk, "member": self.ram_member.pk})
        issue_id = r.data["id"]
        r = self.client.post(f"{API}/library/issues/{issue_id}/return/", {"outcome": "lost"})
        self.assertError(r, 400, "no_price")

    def test_a_member_sees_their_own_issue_history(self):
        self.issue()
        self.login(self.ram_user)
        r = self.client.get(f"{API}/library/issues/me/")
        self.assertEqual(len(r.data), 1)

    def test_a_stranger_cannot_see_someone_elses_issue_via_list(self):
        self.issue()
        self.login(self.sita_user)
        r = self.client.get(f"{API}/library/issues/")
        self.assertEqual(r.data["count"], 0)


class ReservationTests(LibraryTestCase):
    def test_cannot_reserve_while_a_copy_is_available(self):
        self.login(self.ram_user)
        r = self.client.post(f"{API}/library/reservations/", {"book": self.book.pk, "member": self.ram_member.pk})
        self.assertError(r, 409, "copy_available")

    def test_reserving_and_the_queue_fulfilling_on_return(self):
        services.issue_book(copy=self.copy, member=self.sita_member)

        self.login(self.ram_user)
        r = self.client.post(f"{API}/library/reservations/", {"book": self.book.pk, "member": self.ram_member.pk})
        self.assertEqual(r.status_code, 201, r.data)
        reservation_id = r.data["id"]

        self.login(self.office)
        issue = Issue.objects.get(copy=self.copy, member=self.sita_member)
        self.client.post(f"{API}/library/issues/{issue.pk}/return/")

        reservation = Reservation.objects.get(pk=reservation_id)
        self.assertEqual(reservation.status, ReservationStatus.READY)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, CopyStatus.RESERVED)

        self.assertTrue(Notification.objects.filter(
            recipient=self.ram_user, event_type="library.reservation_ready").exists())

    def test_cancelling_a_ready_reservation_frees_the_held_copy(self):
        services.issue_book(copy=self.copy, member=self.sita_member)
        reservation = services.reserve_book(book=self.book, member=self.ram_member)
        services.return_book(issue=Issue.objects.get(copy=self.copy, member=self.sita_member), outcome="returned")
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, ReservationStatus.READY)

        self.login(self.ram_user)
        r = self.client.post(f"{API}/library/reservations/{reservation.pk}/cancel/", {"reason": "No longer needed"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "cancelled")
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, CopyStatus.AVAILABLE)

    def test_a_second_member_cannot_double_reserve(self):
        services.issue_book(copy=self.copy, member=self.sita_member)
        services.reserve_book(book=self.book, member=self.ram_member)
        with self.assertRaises(ConflictError):
            services.reserve_book(book=self.book, member=self.ram_member)

    def test_fulfilling_a_ready_reservation_issues_the_held_copy(self):
        services.issue_book(copy=self.copy, member=self.sita_member)
        reservation = services.reserve_book(book=self.book, member=self.ram_member)
        services.return_book(issue=Issue.objects.get(copy=self.copy, member=self.sita_member), outcome="returned")
        reservation.refresh_from_db()
        issue = services.fulfil_reservation(reservation, by=self.office)
        self.assertEqual(issue.member, self.ram_member)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, ReservationStatus.FULFILLED)

    def test_the_office_fulfils_a_ready_reservation_over_the_api(self):
        services.issue_book(copy=self.copy, member=self.sita_member)
        reservation = services.reserve_book(book=self.book, member=self.ram_member)
        services.return_book(issue=Issue.objects.get(copy=self.copy, member=self.sita_member), outcome="returned")

        self.login(self.office)
        r = self.client.post(f"{API}/library/reservations/{reservation.pk}/fulfil/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "issued")
        self.assertEqual(r.data["member"], self.ram_member.pk)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, ReservationStatus.FULFILLED)

    def test_a_member_cannot_fulfil_their_own_reservation(self):
        services.issue_book(copy=self.copy, member=self.sita_member)
        reservation = services.reserve_book(book=self.book, member=self.ram_member)
        services.return_book(issue=Issue.objects.get(copy=self.copy, member=self.sita_member), outcome="returned")

        self.login(self.ram_user)
        r = self.client.post(f"{API}/library/reservations/{reservation.pk}/fulfil/")
        self.assertEqual(r.status_code, 403)

    def test_expiring_a_stale_ready_reservation_frees_the_copy(self):
        services.issue_book(copy=self.copy, member=self.sita_member)
        reservation = services.reserve_book(book=self.book, member=self.ram_member)
        services.return_book(issue=Issue.objects.get(copy=self.copy, member=self.sita_member), outcome="returned")
        reservation.refresh_from_db()
        reservation.expires_at = timezone.now() - timedelta(days=1)
        reservation.save(update_fields=["expires_at"])

        self.login(self.office)
        r = self.client.post(f"{API}/library/reservations/expire-stale/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["expired"], 1)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, ReservationStatus.EXPIRED)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, CopyStatus.AVAILABLE)

    def test_a_stranger_cannot_reserve_on_someone_elses_behalf(self):
        services.issue_book(copy=self.copy, member=self.sita_member)
        self.login(self.sita_user)
        r = self.client.post(f"{API}/library/reservations/", {"book": self.book.pk, "member": self.ram_member.pk})
        self.assertEqual(r.status_code, 403)


class FineTests(LibraryTestCase):
    def _overdue_fine(self):
        issue = services.issue_book(copy=self.copy, member=self.ram_member)
        issue.due_at = timezone.now() - timedelta(days=2)
        issue.save(update_fields=["due_at"])
        services.return_book(issue=issue, outcome="returned")
        return Fine.objects.get(issue=issue)

    def test_the_office_can_pay_a_fine(self):
        fine = self._overdue_fine()
        self.login(self.office)
        r = self.client.post(f"{API}/library/fines/{fine.pk}/pay/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], "paid")

    def test_the_office_can_waive_a_fine_with_a_reason(self):
        fine = self._overdue_fine()
        self.login(self.office)
        r = self.client.post(f"{API}/library/fines/{fine.pk}/waive/", {"reason": "First offence"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], "waived")

    def test_waiving_needs_a_reason(self):
        fine = self._overdue_fine()
        self.login(self.office)
        r = self.client.post(f"{API}/library/fines/{fine.pk}/waive/", {"reason": ""})
        self.assertEqual(r.status_code, 400)

    def test_a_paid_fine_cannot_be_paid_again(self):
        fine = self._overdue_fine()
        self.login(self.office)
        self.client.post(f"{API}/library/fines/{fine.pk}/pay/")
        r = self.client.post(f"{API}/library/fines/{fine.pk}/pay/")
        self.assertError(r, 409, "not_pending")

    def test_a_member_sees_their_own_fines(self):
        self._overdue_fine()
        self.login(self.ram_user)
        r = self.client.get(f"{API}/library/fines/me/")
        self.assertEqual(len(r.data), 1)

    def test_a_student_cannot_pay_their_own_fine(self):
        fine = self._overdue_fine()
        self.login(self.ram_user)
        r = self.client.post(f"{API}/library/fines/{fine.pk}/pay/")
        self.assertEqual(r.status_code, 403)


class DeleteInUseTests(LibraryTestCase):
    """A soft delete skips the database's PROTECT, so each check lives in the view."""

    def setUp(self):
        super().setUp()
        self.login(self.office)

    def test_a_lent_copy_cannot_be_deleted_but_a_fresh_one_can(self):
        services.issue_book(copy=self.copy, member=self.ram_member)
        self.assertEqual(self.client.delete(f"{API}/library/copies/{self.copy.pk}/").status_code, 409)
        fresh = Copy.objects.create(organization=self.org, book=self.book, campus=self.campus,
                                    accession_number="ACC-000002")
        self.assertEqual(self.client.delete(f"{API}/library/copies/{fresh.pk}/").status_code, 204)

    def test_catalog_entries_in_use_cannot_be_deleted(self):
        self.book.publisher = Publisher.objects.create(organization=self.org, name="Sajha")
        self.book.save(update_fields=["publisher"])
        for url in (f"books/{self.book.pk}", f"authors/{self.author.pk}", f"categories/{self.category.pk}",
                    f"publishers/{self.book.publisher_id}", f"shelves/{self.shelf.pk}"):
            r = self.client.delete(f"{API}/library/{url}/")
            self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "in_use"), url)

    def test_accession_numbers_are_not_reused_after_a_delete(self):
        first = self.client.post(f"{API}/library/copies/", {"book": self.book.pk, "campus": self.campus.pk}).data
        self.client.delete(f"{API}/library/copies/{first['id']}/")
        r = self.client.post(f"{API}/library/copies/", {"book": self.book.pk, "campus": self.campus.pk})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["accession_number"], "ACC-000003")

    def test_a_fine_cannot_be_created_directly_even_by_a_superuser(self):
        from tests.factories import create_superuser

        self.login(create_superuser())
        r = self.client.post(f"{API}/library/fines/", {"organization": self.org.pk})
        self.assertEqual(r.status_code, 405)


class DeskSearchTests(LibraryTestCase):
    """The desk finds a copy by its label, and a member by number or name."""

    def setUp(self):
        super().setUp()
        self.login(self.office)

    def test_find_a_copy_by_accession_number(self):
        Copy.objects.create(organization=self.org, book=self.book, campus=self.campus, accession_number="ACC-000002")
        r = self.client.get(f"{API}/library/copies/", {"search": "ACC-000002"})
        self.assertEqual([c["accession_number"] for c in r.data["results"]], ["ACC-000002"])

    def test_find_a_member_by_name_or_number(self):
        by_name = self.client.get(f"{API}/library/members/", {"search": "Sita"})
        by_number = self.client.get(f"{API}/library/members/", {"search": self.ram_member.member_number})
        self.assertEqual([m["id"] for m in by_name.data["results"]], [self.sita_member.pk])
        self.assertEqual([m["id"] for m in by_number.data["results"]], [self.ram_member.pk])

    def test_find_a_loan_by_accession_number(self):
        services.issue_book(copy=self.copy, member=self.ram_member)
        r = self.client.get(f"{API}/library/issues/", {"search": "ACC-000001"})
        self.assertEqual(r.data["count"], 1)

    def test_list_only_overdue_loans(self):
        late = services.issue_book(copy=self.copy, member=self.ram_member)
        late.due_at = timezone.now() - timedelta(days=2)
        late.save(update_fields=["due_at"])
        on_time = Copy.objects.create(organization=self.org, book=self.book, campus=self.campus,
                                      accession_number="ACC-000002")
        services.issue_book(copy=on_time, member=self.sita_member)
        r = self.client.get(f"{API}/library/issues/", {"overdue": "true"})
        self.assertEqual([i["id"] for i in r.data["results"]], [late.pk])
