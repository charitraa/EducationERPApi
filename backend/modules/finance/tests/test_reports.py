"""Statements, outstanding invoices, collection, and late fees."""
from datetime import timedelta
from decimal import Decimal as D

from tests.factories import create_campus, create_organization, user_with_system_role

from modules.finance.models import Invoice

from .base import API, TODAY, FinanceTestCase

REPORTS = f"{API}/invoices/reports"


class StatementTests(FinanceTestCase):
    def setUp(self):
        super().setUp()
        self.bill_term()
        self.login(self.office)
        self.invoice = self.invoice_of(self.ram)
        self.client.post(f"{API}/payments/", {"invoice": self.invoice.pk, "amount": "4000"})

    def test_student_statement(self):
        r = self.client.get(f"{REPORTS}/student/", {"student": self.ram.pk})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data["total_billed"], r.data["total_paid"], r.data["balance"]),
                         (10000.0, 4000.0, 6000.0))
        self.assertEqual(len(r.data["invoices"]), 1)

    def test_needs_a_student(self):
        self.assertEqual(self.client.get(f"{REPORTS}/student/").status_code, 400)

    def test_unknown_student_is_404(self):
        self.assertEqual(self.client.get(f"{REPORTS}/student/", {"student": 999999}).status_code, 404)


class OutstandingTests(FinanceTestCase):
    def setUp(self):
        super().setUp()
        self.bill_term()
        self.login(self.office)

    def make_overdue(self, student, days_overdue=5):
        invoice = self.invoice_of(student)
        invoice.due_date = TODAY - timedelta(days=days_overdue)
        invoice.save(update_fields=["due_date"])
        return invoice

    def test_lists_overdue_invoices_only(self):
        self.make_overdue(self.ram, 10)
        self.make_overdue(self.shyam, 2)
        # Gita and Binu stay due in the future.
        r = self.client.get(f"{REPORTS}/outstanding/")
        self.assertEqual(r.data["count"], 2)
        self.assertEqual(r.data["invoices"][0]["student_name"], "Ram Student")  # most overdue first

    def test_a_paid_invoice_is_not_outstanding(self):
        invoice = self.make_overdue(self.ram, 10)
        self.client.post(f"{API}/payments/", {"invoice": invoice.pk, "amount": "10000"})
        r = self.client.get(f"{REPORTS}/outstanding/")
        self.assertEqual(r.data["count"], 0)

    def test_filter_by_section(self):
        self.make_overdue(self.ram, 5)
        self.make_overdue(self.gita, 5)
        r = self.client.get(f"{REPORTS}/outstanding/", {"section": self.section_a.pk})
        self.assertEqual([i["student_name"] for i in r.data["invoices"]], ["Ram Student"])


class OutstandingTenancyTests(FinanceTestCase):
    """The report is one school's: an org-wide role (no campus filter) must
    still never see another organization's invoices."""

    make_overdue = OutstandingTests.make_overdue

    def setUp(self):
        super().setUp()
        self.bill_term()
        self.other_org = create_organization(code="other-school")
        self.other_campus = create_campus(self.other_org, code="elsewhere")

    def test_another_schools_overdue_invoice_is_not_listed(self):
        self.make_overdue(self.ram, 5)
        theirs = self.make_overdue(self.gita, 9)
        Invoice.objects.filter(pk=theirs.pk).update(organization=self.other_org, campus=self.other_campus)
        self.login(self.principal)
        r = self.client.get(f"{REPORTS}/outstanding/")
        self.assertEqual([i["student_name"] for i in r.data["invoices"]], ["Ram Student"])

    def test_another_schools_class_is_404(self):
        self.make_overdue(self.ram, 5)
        self.login(user_with_system_role(self.other_org, "org-admin", email="admin@other.test"))
        r = self.client.get(f"{REPORTS}/outstanding/", {"section": self.section_a.pk})
        self.assertEqual(r.status_code, 404)


class CollectionTests(FinanceTestCase):
    def setUp(self):
        super().setUp()
        self.bill_term()
        self.login(self.office)

    def test_totals_by_method(self):
        self.client.post(f"{API}/payments/", {"invoice": self.invoice_of(self.ram).pk, "amount": "1000",
                                              "method": "cash"})
        self.client.post(f"{API}/payments/", {"invoice": self.invoice_of(self.shyam).pk, "amount": "2000",
                                              "method": "bank"})
        r = self.client.get(f"{REPORTS}/collection/")
        self.assertEqual(r.data["total"], 3000.0)
        self.assertEqual(r.data["by_method"], {"cash": 1000.0, "bank": 2000.0})

    def test_span_defaults_to_the_last_30_days(self):
        r = self.client.get(f"{REPORTS}/collection/")
        self.assertEqual(r.status_code, 200)


class LateFeeTests(FinanceTestCase):
    def setUp(self):
        super().setUp()
        self.bill_term()
        self.login(self.office)

    def overdue(self, student, days=10):
        invoice = self.invoice_of(student)
        invoice.due_date = TODAY - timedelta(days=days)
        invoice.save(update_fields=["due_date"])
        return invoice

    def test_a_flat_late_fee(self):
        self.overdue(self.ram)
        self.overdue(self.shyam)
        r = self.client.post(f"{API}/invoices/assess-late-fees/", {"amount": "100"})
        self.assertEqual((r.status_code, r.data), (200, {"fined": 2}))
        invoice = self.invoice_of(self.ram)
        self.assertEqual(invoice.total, D("10100.00"))
        self.assertTrue(invoice.items.filter(kind="fine").exists())

    def test_a_percentage_late_fee(self):
        self.overdue(self.ram)
        self.client.post(f"{API}/invoices/assess-late-fees/", {"percentage": "5"})
        self.assertEqual(self.invoice_of(self.ram).total, D("10500.00"))

    def test_needs_exactly_one_of_amount_or_percentage(self):
        self.overdue(self.ram)
        self.assertEqual(self.client.post(f"{API}/invoices/assess-late-fees/", {}).status_code, 400)
        r = self.client.post(f"{API}/invoices/assess-late-fees/", {"amount": "10", "percentage": "5"})
        self.assertEqual(r.status_code, 400)

    def test_grace_days_delays_the_fine(self):
        self.overdue(self.ram, days=3)
        r = self.client.post(f"{API}/invoices/assess-late-fees/", {"amount": "100", "grace_days": 5})
        self.assertEqual(r.data["fined"], 0)

    def test_running_it_again_never_fines_the_same_invoice_twice(self):
        self.overdue(self.ram)
        self.client.post(f"{API}/invoices/assess-late-fees/", {"amount": "100"})
        r = self.client.post(f"{API}/invoices/assess-late-fees/", {"amount": "100"})
        self.assertEqual(r.data["fined"], 0)
        self.assertEqual(self.invoice_of(self.ram).items.filter(kind="fine").count(), 1)

    def test_an_invoice_already_paid_in_full_is_not_fined(self):
        invoice = self.overdue(self.ram)
        self.client.post(f"{API}/payments/", {"invoice": invoice.pk, "amount": "10000"})
        self.client.post(f"{API}/invoices/assess-late-fees/", {"amount": "100"})
        self.assertFalse(invoice.items.filter(kind="fine").exists())

    def test_a_teacher_cannot_assess_late_fees(self):
        from tests.factories import user_with_system_role

        teacher = user_with_system_role(self.org, "staff", email="teacher@kmc.test")
        self.login(teacher)
        self.assertEqual(self.client.post(f"{API}/invoices/assess-late-fees/", {"amount": "100"}).status_code, 403)


class VisibilityTests(FinanceTestCase):
    def setUp(self):
        super().setUp()
        self.bill_term()
        self.login(self.office)
        self.invoice = self.invoice_of(self.ram)
        self.client.post(f"{API}/payments/", {"invoice": self.invoice.pk, "amount": "3000"})
        from tests.factories import create_user

        self.ram_user = create_user(self.org, email="ram@kmc.test")
        self.ram.user = self.ram_user
        self.ram.save(update_fields=["user"])

    def test_a_student_sees_their_own_statement(self):
        self.login(self.ram_user)
        r = self.client.get(f"{API}/invoices/me/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["total_paid"], 3000.0)

    def test_a_student_cannot_use_the_staff_endpoints(self):
        self.login(self.ram_user)
        self.assertEqual(self.client.get(f"{API}/invoices/").status_code, 403)
        self.assertEqual(self.client.get(f"{API}/payments/").status_code, 403)

    def test_a_parent_sees_a_linked_childs_statement(self):
        from modules.parents.models import StudentParent
        from tests.factories import create_parent, create_user

        parent_user = create_user(self.org, email="parent@kmc.test")
        parent = create_parent(self.org, first_name="Pita", user=parent_user)
        StudentParent.objects.create(organization=self.org, parent=parent, student=self.ram, relationship="father")
        self.login(parent_user)
        r = self.client.get(f"{API}/invoices/me/")
        self.assertEqual(r.data["student"], self.ram.pk)
        self.assertEqual(self.client.get(f"{API}/invoices/me/", {"student": self.gita.pk}).status_code, 404)
