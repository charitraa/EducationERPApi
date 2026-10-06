from django.db.models import F, Q
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.common.exceptions import ConflictError
from core.common.mixins import CampusScopedViewSet, OrganizationScopedMixin, OrganizationScopedViewSet
from core.common.permissions import HasPermission, IsSameOrganization
from core.organizations.models import Campus, Organization
from core.permissions.selectors import campus_ids_with_permission
from modules.staff.selectors import staff_member_for_user
from modules.students.selectors import student_for_user
from integrations.captcha import base as captcha

from . import services
from .kinds import KINDS
from .models import Application, ApplicationType, Certificate, Status
from .serializers import (
    ApplicationListSerializer,
    ApplicationSerializer,
    ApplicationTypeSerializer,
    AvailableTypeSerializer,
    CertificateSerializer,
    DecideSerializer,
    IssueCertificateSerializer,
    NoteSerializer,
    PublicLookupSerializer,
    PublicResubmitSerializer,
    PublicStatusSerializer,
    PublicSubmitSerializer,
    ResubmitSerializer,
    RevokeSerializer,
    SubmitApplicationSerializer,
    WithdrawSerializer,
)
from .services import CERTIFY, MANAGE, VIEW

TAG = "applications"
# Kinds submitted through the generic forms; a job goes through careers.
DIRECT_KINDS = [kind for kind, spec in KINDS.items() if spec.direct]
PUBLIC_TAG = "public applications"


def _schema(noun: str, *actions):
    summaries = {"list": f"List {noun}s", "retrieve": f"Retrieve a {noun}",
                 "create": f"Create a {noun}", "update": f"Replace a {noun}",
                 "partial_update": f"Update a {noun}", "destroy": f"Delete a {noun}"}
    return extend_schema_view(**{a: extend_schema(tags=[TAG], summary=summaries[a])
                                 for a in actions or summaries})


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
@_schema("application type")
class ApplicationTypeViewSet(OrganizationScopedViewSet):
    """Forms and their approval chains. A type for one campus can be set up
    by that campus's office; a type for every campus needs an
    organization-wide role."""

    queryset = ApplicationType.objects.select_related("campus").prefetch_related("steps")
    serializer_class = ApplicationTypeSerializer
    audit_module = "applications"
    filterset_fields = ["kind", "campus", "is_active", "is_public"]
    search_fields = ["code", "name"]
    required_permissions = {"list": [VIEW], "retrieve": [VIEW], "create": [MANAGE], "update": [MANAGE],
                            "partial_update": [MANAGE], "destroy": [MANAGE]}

    def get_permissions(self):
        if self.action == "available":
            return [IsAuthenticated()]
        return super().get_permissions()

    def _check_scope(self, campus):
        ids = campus_ids_with_permission(self.request.user, MANAGE)
        if ids is None:
            return
        if campus is None or campus.pk not in ids:
            raise PermissionDenied("Your role covers only some campuses; a form for every campus, or another "
                                   "campus, needs an organization-wide role.")

    def perform_create(self, serializer):
        self._check_scope(serializer.validated_data.get("campus"))
        super().perform_create(serializer)

    def perform_update(self, serializer):
        self._check_scope(serializer.instance.campus)
        self._check_scope(serializer.validated_data.get("campus", serializer.instance.campus))
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        self._check_scope(instance.campus)
        if instance.applications.exists():
            raise ConflictError("Applications of this type exist; deactivate it instead.", code="in_use")
        super().perform_destroy(instance)

    @extend_schema(tags=[TAG], summary="Forms I can fill in", responses={200: AvailableTypeSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def available(self, request):
        qs = (ApplicationType.objects.filter(organization_id=request.user.organization_id, is_active=True)
              .filter(kind__in=DIRECT_KINDS).prefetch_related("steps"))
        return Response(AvailableTypeSerializer(qs, many=True).data)


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
SELF_SERVICE = ("create", "retrieve", "me", "pending", "approve", "reject", "send_back", "resubmit", "withdraw")


@extend_schema_view(
    list=extend_schema(tags=[TAG], summary="List applications (office)",
                       responses={200: ApplicationListSerializer(many=True)}),
    retrieve=extend_schema(tags=[TAG], summary="An application with its history"),
    create=extend_schema(tags=[TAG], summary="Submit an application", request=SubmitApplicationSerializer,
                         responses={201: ApplicationSerializer}),
)
class ApplicationViewSet(OrganizationScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                         viewsets.GenericViewSet):
    """Applicants, the people deciding each step, and the office all use
    these. Who may open one: the office (``applications.view`` for its
    campus), whoever decides its current step, and the applicant (or the
    student's parent). Anyone else gets 404, as for a missing id."""

    queryset = Application.objects.select_related(
        "application_type", "campus", "student", "staff", "applicant").prefetch_related(
        "application_type__steps", "events__by")
    serializer_class = ApplicationSerializer
    permission_classes = [HasPermission, IsSameOrganization]
    required_permissions = {"list": [VIEW]}
    filterset_fields = ["application_type", "application_type__kind", "status", "campus", "student", "staff"]
    search_fields = ["number", "contact_name", "student__first_name", "student__last_name",
                     "staff__first_name", "staff__last_name"]

    def get_permissions(self):
        if self.action in SELF_SERVICE:
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_serializer_class(self):
        return ApplicationListSerializer if self.action in ("list", "me", "pending") else ApplicationSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action == "list":
            ids = campus_ids_with_permission(self.request.user, VIEW)
            if ids is not None:
                qs = qs.filter(campus_id__in=ids)
        return qs

    def get_object(self):
        application = super().get_object()
        user = self.request.user
        # The office, the applicant, whoever decides it now, and whoever decided an earlier step.
        if not (services.holds(user, VIEW, application.campus_id) or services.is_party(user, application)
                or services.can_decide(user, application) or application.events.filter(by=user).exists()):
            raise NotFound()
        return application

    # -- submitting ----------------------------------------------------------
    def _subject(self, application_type, student, staff, campus):
        """Who it's about, checked against who's asking."""
        user = self.request.user
        subject = KINDS[application_type.kind].subject
        if subject == "none":
            if campus is None:
                raise ValidationError({"campus": "Say which campus you're applying to."})
            return None, None, campus
        if student is None and staff is None and subject in ("student", "any"):
            student = student_for_user(user)
        if student is None and staff is None and subject in ("staff", "any"):
            staff = staff_member_for_user(user)
        if student is not None:
            if subject == "staff":
                raise ValidationError({"student": "This form is for staff."})
            mine = student.user_id == user.pk or student.pk in {s.pk for s in services.children_of(user)}
            if not mine and not services.holds(user, MANAGE, student.campus_id):
                raise PermissionDenied("You can apply only for yourself or your own children.")
            return student, None, student.campus
        if staff is not None:
            if subject == "student":
                raise ValidationError({"staff": "This form is for students."})
            if staff.user_id != user.pk and not services.holds(user, MANAGE, staff.campus_id):
                raise PermissionDenied("You can apply only for yourself.")
            return None, staff, staff.campus
        if subject == "any" and campus is not None:
            return None, None, campus
        raise ValidationError("Say who this application is for.")

    def create(self, request, *args, **kwargs):
        serializer = SubmitApplicationSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        application_type = data["application_type"]
        if not KINDS[application_type.kind].direct:
            raise ValidationError({"application_type": "Apply for a job through its vacancy (careers)."})
        student, staff, campus = self._subject(application_type, data.get("student"), data.get("staff"),
                                               data.get("campus"))
        application, _ = services.submit(application_type=application_type, campus=campus, raw_data=data["data"],
                                         by=request.user, student=student, staff=staff)
        return Response(ApplicationSerializer(self.get_queryset().get(pk=application.pk)).data,
                        status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Applications I sent, or that are about me or my children",
                   responses={200: ApplicationListSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def me(self, request):
        user = request.user
        children = [s.pk for s in services.children_of(user)]
        qs = self.get_queryset().filter(Q(applicant=user) | Q(student__user=user) | Q(staff__user=user)
                                        | Q(student_id__in=children))
        page = self.paginate_queryset(self.filter_queryset(qs))
        return self.get_paginated_response(ApplicationListSerializer(page, many=True).data)

    @extend_schema(tags=[TAG], summary="Applications waiting for my decision",
                   responses={200: ApplicationListSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def pending(self, request):
        user = request.user
        waiting = self.get_queryset().filter(status=Status.IN_REVIEW)
        steps = waiting.filter(application_type__steps__sequence=F("step"))
        mine = Q(pk__in=[])
        for code in set(steps.values_list("application_type__steps__permission", flat=True)):
            ids = campus_ids_with_permission(user, code)
            if ids is None:
                mine |= Q(application_type__steps__sequence=F("step"), application_type__steps__permission=code)
            elif ids:
                mine |= Q(application_type__steps__sequence=F("step"), application_type__steps__permission=code,
                          campus_id__in=ids)
        rows = [a for a in waiting.filter(mine).distinct() if not services.is_party(user, a)]
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(ApplicationListSerializer(page, many=True).data)

    # -- deciding ------------------------------------------------------------
    @extend_schema(tags=[TAG], summary="Approve the current step (the last one carries the request out)",
                   request=DecideSerializer, responses={200: ApplicationSerializer})
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        application = self.get_object()
        serializer = DecideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.approve(application, by=request.user, note=serializer.validated_data["note"],
                         decision=serializer.validated_data["decision"])
        return Response(ApplicationSerializer(self.get_queryset().get(pk=application.pk)).data)

    @extend_schema(tags=[TAG], summary="Reject (a reason is required)", request=NoteSerializer,
                   responses={200: ApplicationSerializer})
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        application = self.get_object()
        serializer = NoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.reject(application, by=request.user, note=serializer.validated_data["note"])
        return Response(ApplicationSerializer(self.get_queryset().get(pk=application.pk)).data)

    @extend_schema(tags=[TAG], summary="Send back to the applicant to change", request=NoteSerializer,
                   responses={200: ApplicationSerializer})
    @action(detail=True, methods=["post"], url_path="send-back")
    def send_back(self, request, pk=None):
        application = self.get_object()
        serializer = NoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.send_back(application, by=request.user, note=serializer.validated_data["note"])
        return Response(ApplicationSerializer(self.get_queryset().get(pk=application.pk)).data)

    # -- the applicant ---------------------------------------------------------
    def _own(self, application):
        if not services.is_party(self.request.user, application):
            raise PermissionDenied("Only the applicant can do this.")

    @extend_schema(tags=[TAG], summary="Resubmit after it was sent back", request=ResubmitSerializer,
                   responses={200: ApplicationSerializer})
    @action(detail=True, methods=["post"])
    def resubmit(self, request, pk=None):
        application = self.get_object()
        self._own(application)
        serializer = ResubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.resubmit(application, raw_data=serializer.validated_data["data"], by=request.user,
                          note=serializer.validated_data["note"])
        return Response(ApplicationSerializer(self.get_queryset().get(pk=application.pk)).data)

    @extend_schema(tags=[TAG], summary="Withdraw while it's open", request=WithdrawSerializer,
                   responses={200: ApplicationSerializer})
    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        application = self.get_object()
        if not (services.is_party(request.user, application)
                or services.holds(request.user, MANAGE, application.campus_id)):
            raise PermissionDenied("Only the applicant or the office can withdraw it.")
        serializer = WithdrawSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.withdraw(application, by=request.user, note=serializer.validated_data["note"])
        return Response(ApplicationSerializer(self.get_queryset().get(pk=application.pk)).data)


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------
@extend_schema_view(
    list=extend_schema(tags=[TAG], summary="List certificates"),
    retrieve=extend_schema(tags=[TAG], summary="Retrieve a certificate"),
    create=extend_schema(tags=[TAG], summary="Issue a certificate directly", request=IssueCertificateSerializer,
                         responses={201: CertificateSerializer}),
)
class CertificateViewSet(CampusScopedViewSet):
    # Issued, then revoked if need be — never edited or deleted.
    http_method_names = ["get", "post", "head", "options"]
    campus_field = "student__campus"
    queryset = Certificate.objects.select_related("student")
    serializer_class = CertificateSerializer
    audit_module = "applications"
    service_audits_create = True
    filterset_fields = ["student", "title", "application"]
    search_fields = ["number", "title", "student__first_name", "student__last_name"]
    required_permissions = {"list": [VIEW], "retrieve": [VIEW], "create": [CERTIFY], "revoke": [CERTIFY]}

    def get_permissions(self):
        if self.action == "me":
            return [IsAuthenticated()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        serializer = IssueCertificateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        certificate = services.issue_certificate(by=request.user, **serializer.validated_data)
        return Response(CertificateSerializer(certificate).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Revoke (it stays on record)", request=RevokeSerializer,
                   responses={200: CertificateSerializer})
    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        certificate = self.get_object()
        serializer = RevokeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        certificate = services.revoke_certificate(certificate, by=request.user, **serializer.validated_data)
        return Response(CertificateSerializer(certificate).data)

    @extend_schema(tags=[TAG], summary="My (or my children's) certificates",
                   responses={200: CertificateSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def me(self, request):
        user = request.user
        children = [s.pk for s in services.children_of(user)]
        qs = Certificate.objects.filter(organization_id=user.organization_id).filter(
            Q(student__user=user) | Q(student_id__in=children)).select_related("student")
        return Response(CertificateSerializer(qs, many=True).data)


# ---------------------------------------------------------------------------
# Public forms (no account)
# ---------------------------------------------------------------------------
class PublicView(APIView):
    """No sign-in; limited per address by the ``public_applications`` rate.
    The organization is named by its code in the URL."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "public_applications"

    def organization(self, code):
        organization = Organization.objects.filter(code=str(code).lower(), is_active=True).first()
        if organization is None:
            raise NotFound()
        return organization

    def lookup(self, organization, data):
        application = services.find_public(organization, data["number"], data["token"])
        if application is None:
            raise NotFound("No application matches that number and token.")
        return application


class PublicTypesView(PublicView):
    # Loading the form is a page view, not a submission or a token lookup.
    throttle_scope = "public_read"

    @extend_schema(tags=[PUBLIC_TAG], summary="Forms open to the public",
                   responses={200: AvailableTypeSerializer(many=True)})
    def get(self, request, code):
        organization = self.organization(code)
        qs = ApplicationType.objects.filter(organization=organization, is_public=True, is_active=True,
                                            kind__in=DIRECT_KINDS).prefetch_related("steps")
        campuses = Campus.objects.filter(organization=organization, is_active=True).values("id", "name")
        return Response({"organization": organization.name, "campuses": list(campuses),
                         "forms": AvailableTypeSerializer(qs, many=True).data})


class PublicSubmitView(PublicView):
    @extend_schema(tags=[PUBLIC_TAG], summary="Apply (returns your reference and a secret token — keep it)",
                   request=PublicSubmitSerializer, responses={201: dict})
    def post(self, request, code):
        organization = self.organization(code)
        serializer = PublicSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        captcha.require(request, data.get("captcha_token", ""))
        application_type = ApplicationType.objects.filter(organization=organization, pk=data["application_type"],
                                                          is_public=True, is_active=True,
                                                          kind__in=DIRECT_KINDS).first()
        if application_type is None:
            raise ValidationError({"application_type": "Unknown form."})
        campus = Campus.objects.filter(organization=organization, pk=data["campus"], is_active=True).first()
        if campus is None:
            raise ValidationError({"campus": "Unknown campus."})
        application, token = services.submit(application_type=application_type, campus=campus,
                                             raw_data=data["data"], contact=data["contact"], public=True)
        return Response({"number": application.number, "token": token, "status": application.status},
                        status=status.HTTP_201_CREATED)


class PublicStatusView(PublicView):
    @extend_schema(tags=[PUBLIC_TAG], summary="Check an application", request=PublicLookupSerializer,
                   responses={200: PublicStatusSerializer})
    def post(self, request, code):
        serializer = PublicLookupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        application = self.lookup(self.organization(code), serializer.validated_data)
        return Response(PublicStatusSerializer(application).data)


class PublicResubmitView(PublicView):
    @extend_schema(tags=[PUBLIC_TAG], summary="Resubmit after it was sent back", request=PublicResubmitSerializer,
                   responses={200: PublicStatusSerializer})
    def post(self, request, code):
        serializer = PublicResubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        application = self.lookup(self.organization(code), serializer.validated_data)
        application = services.resubmit(application, raw_data=serializer.validated_data["data"])
        return Response(PublicStatusSerializer(application).data)


class PublicWithdrawView(PublicView):
    @extend_schema(tags=[PUBLIC_TAG], summary="Withdraw an application", request=PublicLookupSerializer,
                   responses={200: PublicStatusSerializer})
    def post(self, request, code):
        serializer = PublicLookupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        application = self.lookup(self.organization(code), serializer.validated_data)
        application = services.withdraw(application)
        return Response(PublicStatusSerializer(application).data)
