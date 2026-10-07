"""Read-side: statements and reports. Everything answers as plain data."""
from collections import defaultdict
from datetime import date as Date
from decimal import Decimal

from django.db.models import F
from django.utils import timezone

from .models import Invoice, InvoiceStatus, Payment

ZERO = Decimal("0")


def num(value):
    return None if value is None else float(value)


def student_statement(student, start: Date | None = None, end: Date | None = None) -> dict:
    """One student's invoices, what's been paid, and what's still owed."""
    invoices = (Invoice.objects.filter(student=student).exclude(status=InvoiceStatus.CANCELLED)
               .select_related("term", "academic_year").prefetch_related("items").order_by("-issue_date", "-pk"))
    if start:
        invoices = invoices.filter(issue_date__gte=start)
    if end:
        invoices = invoices.filter(issue_date__lte=end)
    rows = [{
        "invoice": i.pk, "invoice_number": i.invoice_number, "term": i.term_id, "term_name": str(i.term) if i.term else None,
        "issue_date": i.issue_date, "due_date": i.due_date, "total": num(i.total), "paid": num(i.paid_amount),
        "balance": num(i.balance), "is_paid": i.is_paid, "is_overdue": i.is_overdue,
    } for i in invoices]
    total_billed = sum((i.total for i in invoices), ZERO)
    total_paid = sum((i.paid_amount for i in invoices), ZERO)
    return {
        "student": student.pk, "student_name": student.full_name, "student_number": student.student_number,
        "invoices": rows, "total_billed": num(total_billed), "total_paid": num(total_paid),
        "balance": num(total_billed - total_paid),
    }


def outstanding(organization_id, campus_ids=None, program=None, section=None, as_of: Date | None = None) -> dict:
    """Overdue invoices: who owes what, and for how long. ``campus_ids`` None
    means every campus *of this organization*, never every tenant's."""
    as_of = as_of or timezone.localdate()
    invoices = (Invoice.objects.filter(organization_id=organization_id, status=InvoiceStatus.ISSUED, due_date__lt=as_of)
               .exclude(paid_amount__gte=F("total"))
               .select_related("student", "enrollment__section__program"))
    if campus_ids is not None:
        invoices = invoices.filter(campus_id__in=campus_ids)
    if program is not None:
        invoices = invoices.filter(enrollment__section__program=program)
    if section is not None:
        invoices = invoices.filter(enrollment__section=section)
    rows = [{
        "invoice": i.pk, "invoice_number": i.invoice_number, "student": i.student_id,
        "student_name": i.student.full_name, "student_number": i.student.student_number,
        "section_name": i.enrollment.section.display_name if i.enrollment.section_id else None,
        "due_date": i.due_date, "days_overdue": (as_of - i.due_date).days, "total": num(i.total),
        "balance": num(i.balance),
    } for i in invoices if i.balance > 0]
    rows.sort(key=lambda r: -r["days_overdue"])
    return {"as_of": as_of.isoformat(), "count": len(rows), "total_outstanding": sum(r["balance"] for r in rows),
           "invoices": rows}


def collection(organization_id, start: Date, end: Date, campus_ids=None) -> dict:
    """Payments received in a span, totalled by method — a day sheet."""
    payments = Payment.objects.filter(organization_id=organization_id, paid_at__date__gte=start,
                                      paid_at__date__lte=end).select_related("invoice__student")
    if campus_ids is not None:
        payments = payments.filter(campus_id__in=campus_ids)
    by_method = defaultdict(lambda: ZERO)
    rows = []
    for p in payments.order_by("paid_at"):
        by_method[p.method] += p.amount
        rows.append({
            "payment": p.pk, "invoice": p.invoice_id, "invoice_number": p.invoice.invoice_number,
            "student_name": p.invoice.student.full_name, "amount": num(p.amount), "method": p.method,
            "reference": p.reference, "paid_at": p.paid_at,
        })
    return {"from": start.isoformat(), "to": end.isoformat(), "total": num(sum(by_method.values(), ZERO)),
           "by_method": {k: num(v) for k, v in by_method.items()}, "payments": rows}
