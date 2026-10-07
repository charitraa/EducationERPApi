from django.utils import timezone
from django_filters import rest_framework as filters
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.common.exceptions import ConflictError
from core.common.mixins import CampusScopedMixin, CampusScopedViewSet, OrganizationScopedMixin, OrganizationScopedViewSet
from core.common.permissions import HasPermission, IsSameOrganization
from modules.parents.selectors import links_for_parent, parent_for_user
from modules.students.selectors import student_for_user, students_visible_to

from . import selectors, services
from .models import (
    FeeCategory,
    FeeStructure,
    Invoice,
    Payment,
    Receipt,
    Refund,
    Scholarship,
    StudentScholarship,
)
from .serializers import (
    AddInvoiceItemSerializer,
    AssessLateFeesSerializer,
    CancelInvoiceSerializer,
    EndScholarshipSerializer,
    FeeCategorySerializer,
    FeeStructureSerializer,
    GenerateOneTimeInvoiceSerializer,
    GenerateTermInvoicesSerializer,
    InvoiceItemSerializer,
    InvoiceListSerializer,
    InvoiceSerializer,
    PaymentSerializer,
    ReceiptSerializer,
    RecordPaymentSerializer,
    RefundSerializer,
    RequestRefundSerializer,
    ScholarshipSerializer,
    SetInstallmentsSerializer,
    StudentScholarshipSerializer,
)
from .services import COLLECT, MANAGE, VIEW

TAG = "finance"


def _schema(noun: str, *actions):
    summaries = {"list": f"List {noun}s", "retrieve": f"Retrieve a {noun}",
                 "create": f"Create a {noun}", "update": f"Replace a {noun}",
                 "partial_update": f"Update a {noun}", "destroy": f"Delete a {noun}"}
    return extend_schema_view(**{a: extend_schema(tags=[TAG], summary=summaries[a])
                                 for a in actions or summaries})


def _own_student(request):
    """The student behind ``request``: the signed-in student, or a parent's
    child (``?student=`` when they have several)."""
    student = student_for_user(request.user)
    if student is not None:
        return student
    parent = parent_for_user(request.user)
    if parent is None:
        raise NotFound("No student or parent profile is linked to your account.")
    links = list(links_for_parent(parent))
    wanted = request.query_params.get("student")
    if wanted is not None:
        if not wanted.isdigit():
            raise ValidationError({"student": "Must be an id."})
        links = [link for link in links if link.student_id == int(wanted)]
        if not links:
            raise NotFound("That student isn't linked to you.")
    elif len(links) > 1:
        raise ValidationError({"student": "You have several children linked; choose one."})
    if not links:
        raise NotFound("No student is linked to you.")
    return links[0].student


class FinanceViewSet(CampusScopedViewSet):
    audit_module = "finance"


# ---------------------------------------------------------------------------
# Fee structure
# ---------------------------------------------------------------------------
@_schema("fee category")
class FeeCategoryViewSet(OrganizationScopedViewSet):
    queryset = FeeCategory.objects.all()
    serializer_class = FeeCategorySerializer
    audit_module = "finance"
    filterset_fields = ["is_active"]
    search_fields = ["name", "code"]
    required_permissions = {"list": [VIEW], "retrieve": [VIEW], "create": [MANAGE], "update": [MANAGE],
                            "partial_update": [MANAGE], "destroy": [MANAGE]}

    def perform_destroy(self, instance):
        from .models import InvoiceItem

        if FeeStructure.objects.filter(items__category=instance).exists() or InvoiceItem.objects.filter(
                category=instance).exists() or Scholarship.objects.filter(category=instance).exists():
            raise ConflictError("This category is used by a fee structure, an invoice or a scholarship.",
                                code="in_use")
        super().perform_destroy(instance)


@_schema("fee structure")
class FeeStructureViewSet(OrganizationScopedViewSet):
    queryset = FeeStructure.objects.select_related("program", "academic_year").prefetch_related("items__category")
    serializer_class = FeeStructureSerializer
    audit_module = "finance"
    filterset_fields = ["program", "level", "academic_year", "is_active"]
    required_permissions = {
        "list": [VIEW], "retrieve": [VIEW], "create": [MANAGE], "update": [MANAGE], "partial_update": [MANAGE],
        "destroy": [MANAGE], "generate_term_invoices": [MANAGE], "generate_one_time_invoice": [MANAGE],
    }

    def perform_destroy(self, instance):
        if instance.invoices.exists():
            raise ConflictError("Invoices have been generated from this structure.", code="in_use")
        super().perform_destroy(instance)

    @extend_schema(tags=[TAG], summary="Generate this term's invoices from the structure",
                   description="One invoice per student currently placed in a matching class. Already-invoiced "
                               "students are skipped, so running it again only bills whoever is new.",
                   request=GenerateTermInvoicesSerializer, responses={200: None})
    @action(detail=True, methods=["post"], url_path="generate-invoices")
    def generate_term_invoices(self, request, pk=None):
        structure = self.get_object()
        serializer = GenerateTermInvoicesSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        return Response(services.generate_term_invoices(structure, by=request.user, **serializer.validated_data))

    @extend_schema(tags=[TAG], summary="Generate one student's one-time invoice (e.g. admission)",
                   request=GenerateOneTimeInvoiceSerializer, responses={201: InvoiceSerializer})
    @action(detail=True, methods=["post"], url_path="generate-one-time-invoice")
    def generate_one_time_invoice(self, request, pk=None):
        structure = self.get_object()
        serializer = GenerateOneTimeInvoiceSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        invoice = services.generate_one_time_invoice(structure, by=request.user, **serializer.validated_data)
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Scholarships
# ---------------------------------------------------------------------------
@_schema("scholarship")
class ScholarshipViewSet(OrganizationScopedViewSet):
    queryset = Scholarship.objects.select_related("category")
    serializer_class = ScholarshipSerializer
    audit_module = "finance"
    filterset_fields = ["is_active", "kind"]
    required_permissions = {"list": [VIEW], "retrieve": [VIEW], "create": [MANAGE], "update": [MANAGE],
                            "partial_update": [MANAGE], "destroy": [MANAGE]}

    def perform_destroy(self, instance):
        if instance.grants.exists():
            raise ConflictError("Students hold this scholarship.", code="in_use")
        super().perform_destroy(instance)


@_schema("student scholarship", "list", "retrieve", "create")
class StudentScholarshipViewSet(CampusScopedViewSet):
    """Who holds which scholarship. History, like an elective: ending one
    keeps the record rather than deleting it — see the ``end`` action."""

    http_method_names = ["get", "post", "head", "options"]
    queryset = StudentScholarship.objects.select_related("student__campus", "scholarship")
    serializer_class = StudentScholarshipSerializer
    campus_field = "student__campus"
    filterset_fields = ["student", "scholarship"]
    required_permissions = {"list": [VIEW], "retrieve": [VIEW], "create": [MANAGE], "end": [MANAGE]}

    def campus_of(self, validated_data):
        student = validated_data.get("student")
        return student.campus if student is not None else None

    @extend_schema(tags=[TAG], summary="End a student's scholarship", request=EndScholarshipSerializer,
                   responses={200: StudentScholarshipSerializer})
    @action(detail=True, methods=["post"])
    def end(self, request, pk=None):
        grant = self.get_object()
        serializer = EndScholarshipSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        grant = services.end_scholarship(grant, serializer.validated_data["ended_on"], by=request.user)
        return Response(StudentScholarshipSerializer(grant).data)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------
class InvoiceFilter(filters.FilterSet):
    date_from = filters.DateFilter(field_name="issue_date", lookup_expr="gte")
    date_to = filters.DateFilter(field_name="issue_date", lookup_expr="lte")

    class Meta:
        model = Invoice
        fields = ["student", "campus", "term", "academic_year", "status", "fee_structure", "source"]


@_schema("invoice", "list", "retrieve")
class InvoiceViewSet(FinanceViewSet):
    http_method_names = ["get", "post", "head", "options"]
    queryset = Invoice.objects.select_related("student", "term", "campus").prefetch_related(
        "items__category", "installments")
    serializer_class = InvoiceSerializer
    filterset_class = InvoiceFilter
    search_fields = ["invoice_number", "student__first_name", "student__last_name", "student__student_number"]
    ordering_fields = ["issue_date", "due_date", "total"]
    required_permissions = {
        "list": [VIEW], "retrieve": [VIEW], "add_item": [MANAGE], "cancel": [MANAGE],
        "set_installments": [MANAGE],
    }

    def get_permissions(self):
        if self.action == "me":
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_serializer_class(self):
        return InvoiceListSerializer if self.action == "list" else InvoiceSerializer

    def create(self, request, *args, **kwargs):
        # Invoices are only ever made through generate-invoices / generate-one-time-invoice
        # (every field here is read-only anyway); this guards a superuser, who bypasses
        # HasPermission, from an otherwise-empty create() blowing up on missing required fields.
        raise MethodNotAllowed("POST")

    @extend_schema(tags=[TAG], summary="Add an ad-hoc line (a discount, a fine, an adjustment)",
                   request=AddInvoiceItemSerializer, responses={201: InvoiceItemSerializer})
    @action(detail=True, methods=["post"], url_path="add-item")
    def add_item(self, request, pk=None):
        invoice = self.get_object()
        serializer = AddInvoiceItemSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        item = services.add_invoice_item(invoice, by=request.user, **serializer.validated_data)
        return Response(InvoiceItemSerializer(item).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Cancel an invoice", request=CancelInvoiceSerializer,
                   responses={200: InvoiceSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        invoice = self.get_object()
        serializer = CancelInvoiceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = services.cancel_invoice(invoice, serializer.validated_data["reason"], by=request.user)
        return Response(InvoiceSerializer(invoice).data)

    @extend_schema(tags=[TAG], summary="Split the total into due-dated installments",
                   description="Replaces any earlier schedule. Refused once a payment exists.",
                   request=SetInstallmentsSerializer, responses={200: InvoiceSerializer})
    @action(detail=True, methods=["post"], url_path="installments")
    def set_installments(self, request, pk=None):
        invoice = self.get_object()
        serializer = SetInstallmentsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = [services.InstallmentInput(**row) for row in serializer.validated_data["installments"]]
        services.set_installments(invoice, plan, by=request.user)
        invoice.refresh_from_db()
        return Response(InvoiceSerializer(invoice).data)

    @extend_schema(tags=[TAG], summary="My invoices (students), or a child's (parents)",
                   parameters=[OpenApiParameter("student", int, description="Parents: which child.")],
                   responses={200: None})
    @action(detail=False, methods=["get"])
    def me(self, request):
        return Response(selectors.student_statement(_own_student(request)))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
class FinanceReportViewSet(CampusScopedMixin, OrganizationScopedMixin, viewsets.GenericViewSet):
    queryset = Invoice.objects.all()
    campus_field = "campus"
    permission_classes = [HasPermission, IsSameOrganization]
    required_permissions = {"default": [VIEW]}
    pagination_class = None

    def _campus_ids(self):
        from core.permissions.selectors import campus_ids_with_permission

        return campus_ids_with_permission(self.request.user, VIEW)

    def _span(self, request):
        from datetime import timedelta

        end = _date(request, "to", timezone.localdate())
        start = _date(request, "from", end - timedelta(days=30))
        if start > end:
            raise ValidationError({"from": "Must not be after to."})
        return start, end

    @extend_schema(tags=[TAG], summary="One student's statement",
                   parameters=[OpenApiParameter("student", int, required=True),
                               OpenApiParameter("from", str), OpenApiParameter("to", str)],
                   responses={200: None})
    @action(detail=False, methods=["get"])
    def student(self, request):
        student = students_visible_to(request.user, VIEW).filter(pk=_id(request, "student")).first()
        if student is None:
            raise NotFound("No such student.")
        start = _date(request, "from", required=False)
        end = _date(request, "to", required=False)
        return Response(selectors.student_statement(student, start, end))

    @extend_schema(tags=[TAG], summary="Overdue invoices", parameters=[
        OpenApiParameter("program", int), OpenApiParameter("section", int)], responses={200: None})
    @action(detail=False, methods=["get"])
    def outstanding(self, request):
        from modules.academics.models import Program, Section

        org = request.user.organization_id
        program = Program.objects.filter(organization_id=org, pk=_id(request, "program", required=False)).first() \
            if request.query_params.get("program") else None
        section = Section.objects.filter(organization_id=org, pk=_id(request, "section", required=False)).first() \
            if request.query_params.get("section") else None
        if (request.query_params.get("program") and program is None) or (request.query_params.get("section") and section is None):
            raise NotFound("No such program or class.")
        return Response(selectors.outstanding(org, self._campus_ids(), program=program, section=section))

    @extend_schema(tags=[TAG], summary="Payments received in a span, by method (a day sheet)",
                   parameters=[OpenApiParameter("from", str), OpenApiParameter("to", str)], responses={200: None})
    @action(detail=False, methods=["get"])
    def collection(self, request):
        start, end = self._span(request)
        return Response(selectors.collection(request.user.organization_id, start, end, self._campus_ids()))


def _id(request, name, required=True):
    raw = request.query_params.get(name)
    if raw is None:
        if required:
            raise ValidationError({name: "Required."})
        return None
    if not raw.isdigit():
        raise ValidationError({name: "Must be an id."})
    return int(raw)


def _date(request, name, default=None, required=True):
    from django.utils.dateparse import parse_date

    raw = request.query_params.get(name)
    if raw is None:
        if required and default is None:
            raise ValidationError({name: "Give a date as YYYY-MM-DD."})
        return default
    value = parse_date(raw)
    if value is None:
        raise ValidationError({name: "Give a date as YYYY-MM-DD."})
    return value


# ---------------------------------------------------------------------------
# Payments, receipts, refunds
# ---------------------------------------------------------------------------
@_schema("payment", "list", "retrieve")
class PaymentViewSet(FinanceViewSet):
    http_method_names = ["get", "post", "head", "options"]
    queryset = Payment.objects.select_related("invoice__student").prefetch_related("refunds")
    serializer_class = PaymentSerializer
    filterset_fields = ["invoice", "method"]
    ordering_fields = ["paid_at"]
    required_permissions = {"list": [VIEW], "retrieve": [VIEW], "create": [COLLECT], "refund": [MANAGE]}

    def campus_of(self, validated_data):
        invoice = validated_data.get("invoice")
        return invoice.campus if invoice is not None else None

    @extend_schema(tags=[TAG], summary="Record a payment against an invoice, and issue a receipt",
                   request=RecordPaymentSerializer, responses={201: PaymentSerializer})
    def create(self, request, *args, **kwargs):
        serializer = RecordPaymentSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        invoice = data.pop("invoice")
        self.check_campus_allowed(invoice.campus_id)
        data.setdefault("paid_at", timezone.now())
        payment = services.record_payment(invoice, by=request.user, **data)
        return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Refund some or all of a payment", request=RequestRefundSerializer,
                   responses={201: RefundSerializer})
    @action(detail=True, methods=["post"])
    def refund(self, request, pk=None):
        payment = self.get_object()
        serializer = RequestRefundSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        refund = services.refund_payment(payment, by=request.user, **serializer.validated_data)
        return Response(RefundSerializer(refund).data, status=status.HTTP_201_CREATED)


@_schema("receipt", "list", "retrieve")
class ReceiptViewSet(FinanceViewSet):
    http_method_names = ["get", "head", "options"]
    queryset = Receipt.objects.select_related("payment__invoice__student")
    serializer_class = ReceiptSerializer
    campus_field = "payment__invoice__campus"
    filterset_fields = ["payment"]
    search_fields = ["receipt_number"]
    required_permissions = {"list": [VIEW], "retrieve": [VIEW]}


@_schema("refund", "list", "retrieve")
class RefundViewSet(FinanceViewSet):
    http_method_names = ["get", "head", "options"]
    queryset = Refund.objects.select_related("payment__invoice__student")
    serializer_class = RefundSerializer
    campus_field = "payment__invoice__campus"
    filterset_fields = ["payment"]
    required_permissions = {"list": [VIEW], "retrieve": [VIEW]}


class AssessLateFeesView(APIView):
    """A one-off bulk action, not tied to one model — see DevicePunchView for
    the same shape."""

    permission_classes = [HasPermission]
    required_permissions = {"default": [MANAGE]}

    @extend_schema(tags=[TAG], summary="Assess a late fee on every overdue invoice",
                   description="Once each: an invoice that already has a fine is skipped, so this is safe to "
                               "run again.", request=AssessLateFeesSerializer, responses={200: None})
    def post(self, request):
        serializer = AssessLateFeesSerializer(data=request.data, context={"view": self, "request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        campus = data.pop("campus", None)
        if campus is not None:
            services.ensure_campus_allowed(request.user, MANAGE, campus.pk)
        return Response(services.assess_late_fees(organization_id=request.user.organization_id, campus=campus,
                                                   by=request.user, **data))

    def get_target_organization_id(self):
        return self.request.user.organization_id
