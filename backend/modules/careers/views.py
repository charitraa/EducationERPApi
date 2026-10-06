from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.common.exceptions import ConflictError
from core.common.mixins import CampusScopedViewSet, OrganizationScopedMixin
from core.common.permissions import IsSameOrganization
from core.organizations.models import Organization
from modules.alumni.services import profile_for_user
from modules.applications import services as applications
from modules.applications.serializers import PublicLookupSerializer
from modules.staff.selectors import staff_member_for_user
from modules.students.selectors import student_for_user

from . import services
from .models import Audience, Candidacy, Interview, JobOffer, JobPosting, PostingStatus, Vacancy, VacancyStatus
from .serializers import (
    ApplyResultSerializer,
    ApplySerializer,
    CandidacyDetailSerializer,
    CandidacySerializer,
    InterviewSerializer,
    JobOfferSerializer,
    JobPostingSerializer,
    MakeOfferSerializer,
    OutcomeSerializer,
    PublicApplySerializer,
    PublicOfferSerializer,
    PublicRespondOfferSerializer,
    PublicVacancySerializer,
    CareersReasonSerializer,
    RescheduleSerializer,
    RespondOfferSerializer,
    ReviewPostingSerializer,
    ScheduleInterviewSerializer,
    ScreenSerializer,
    VacancySerializer,
)
from .services import BOARD, HIRE, MANAGE, VIEW

TAG = "careers"
PUBLIC_TAG = "public careers"
READ = {"list": [VIEW], "retrieve": [VIEW]}


def _accepting(qs):
    today = timezone.localdate()
    return (qs.filter(status=VacancyStatus.OPEN).filter(Q(opens_on__isnull=True) | Q(opens_on__lte=today))
            .filter(Q(closes_on__isnull=True) | Q(closes_on__gte=today)))


# ---------------------------------------------------------------------------
# Vacancies
# ---------------------------------------------------------------------------
@extend_schema_view(
    list=extend_schema(tags=[TAG], summary="List vacancies"),
    retrieve=extend_schema(tags=[TAG], summary="Retrieve a vacancy"),
    create=extend_schema(tags=[TAG], summary="Create a vacancy (as a draft)"),
    update=extend_schema(tags=[TAG], summary="Replace a vacancy"),
    partial_update=extend_schema(tags=[TAG], summary="Update a vacancy"),
    destroy=extend_schema(tags=[TAG], summary="Delete a draft vacancy nobody applied to"),
)
class VacancyViewSet(CampusScopedViewSet):
    queryset = Vacancy.objects.select_related("campus", "position", "department", "application_type")
    serializer_class = VacancySerializer
    audit_module = "careers"
    filterset_fields = ["campus", "status", "staff_type", "is_public", "department"]
    search_fields = ["code", "title"]
    required_permissions = {**READ, "create": [MANAGE], "update": [MANAGE], "partial_update": [MANAGE],
                            "destroy": [MANAGE], "open": [MANAGE], "close": [MANAGE]}

    def get_permissions(self):
        if self.action in ("current", "apply"):
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action in ("list", "retrieve"):
            # Explicit order: Django drops Meta.ordering from a GROUP BY query.
            qs = (qs.annotate(candidates=Count("candidacies", filter=Q(candidacies__deleted_at__isnull=True)))
                  .order_by(*Vacancy._meta.ordering))
        return qs

    def perform_destroy(self, instance):
        if instance.status != VacancyStatus.DRAFT or instance.candidacies.exists():
            raise ConflictError("Only a draft nobody applied to can be deleted; close it instead.", code="in_use")
        super().perform_destroy(instance)

    @extend_schema(tags=[TAG], summary="Open it for applications", request=None, responses={200: VacancySerializer})
    @action(detail=True, methods=["post"])
    def open(self, request, pk=None):
        return Response(VacancySerializer(services.open_vacancy(self.get_object(), by=request.user)).data)

    @extend_schema(tags=[TAG], summary="Stop taking applications", request=None, responses={200: VacancySerializer})
    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        return Response(VacancySerializer(services.close_vacancy(self.get_object(), by=request.user)).data)

    @extend_schema(tags=[TAG], summary="Vacancies I can apply to", responses={200: PublicVacancySerializer(many=True)})
    @action(detail=False, methods=["get"])
    def current(self, request):
        qs = _accepting(Vacancy.objects.filter(organization_id=request.user.organization_id)
                        .select_related("campus", "department", "application_type"))
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(PublicVacancySerializer(page, many=True).data)

    @extend_schema(tags=[TAG], summary="Apply (signed in; upload the résumé to /files/ first)",
                   request=ApplySerializer, responses={201: CandidacyDetailSerializer})
    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        vacancy = (Vacancy.objects.filter(pk=pk, organization_id=request.user.organization_id)
                   .select_related("campus", "application_type").first())
        if vacancy is None or not services.is_accepting(vacancy):
            raise NotFound()
        serializer = ApplySerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        candidacy, _ = services.apply(vacancy, raw_data=serializer.validated_data["data"], by=request.user,
                                      resume_file=serializer.validated_data.get("resume_file"))
        return Response(CandidacyDetailSerializer(candidacy).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------
@extend_schema_view(
    list=extend_schema(tags=[TAG], summary="List candidates"),
    retrieve=extend_schema(tags=[TAG], summary="A candidate with their application data"),
)
class CandidacyViewSet(CampusScopedViewSet):
    """Read-only: a candidacy is made by applying, and decided through its
    application (``/applications/{id}/approve|reject|send-back``)."""

    http_method_names = ["get", "post", "head", "options"]
    campus_field = "vacancy__campus"
    queryset = Candidacy.objects.select_related("vacancy", "application", "resume")
    serializer_class = CandidacySerializer
    audit_module = "careers"
    filterset_fields = ["vacancy", "application__status"]
    search_fields = ["full_name", "email", "phone", "application__number"]
    ordering_fields = ["created_at", "screening_score"]
    required_permissions = {**READ, "screen": [MANAGE]}

    def get_serializer_class(self):
        return CandidacyDetailSerializer if self.action == "retrieve" else CandidacySerializer

    def create(self, request, *args, **kwargs):
        # Superusers skip HasPermission; nobody creates a candidacy except by applying.
        from rest_framework.exceptions import MethodNotAllowed

        raise MethodNotAllowed("POST")

    @extend_schema(tags=[TAG], summary="Record a screening score and note", request=ScreenSerializer,
                   responses={200: CandidacySerializer})
    @action(detail=True, methods=["post"])
    def screen(self, request, pk=None):
        serializer = ScreenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        candidacy = services.screen(self.get_object(), by=request.user, **serializer.validated_data)
        return Response(CandidacySerializer(candidacy).data)


# ---------------------------------------------------------------------------
# Interviews
# ---------------------------------------------------------------------------
@extend_schema_view(
    list=extend_schema(tags=[TAG], summary="List interviews"),
    retrieve=extend_schema(tags=[TAG], summary="Retrieve an interview"),
    create=extend_schema(tags=[TAG], summary="Schedule an interview (the candidate and panel are told)",
                         request=ScheduleInterviewSerializer, responses={201: InterviewSerializer}),
)
class InterviewViewSet(CampusScopedViewSet):
    http_method_names = ["get", "post", "head", "options"]
    campus_field = "candidacy__vacancy__campus"
    queryset = Interview.objects.select_related("candidacy__vacancy").prefetch_related("panel")
    serializer_class = InterviewSerializer
    audit_module = "careers"
    service_audits_create = True
    filterset_fields = ["candidacy", "status", "mode"]
    required_permissions = {**READ, "create": [MANAGE], "reschedule": [MANAGE], "cancel": [MANAGE]}

    def get_permissions(self):
        if self.action in ("mine", "outcome"):
            return [IsAuthenticated()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        serializer = ScheduleInterviewSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        interview = services.schedule_interview(by=request.user, **serializer.validated_data)
        return Response(InterviewSerializer(interview).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Move it", request=RescheduleSerializer, responses={200: InterviewSerializer})
    @action(detail=True, methods=["post"])
    def reschedule(self, request, pk=None):
        serializer = RescheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        interview = services.reschedule_interview(self.get_object(), by=request.user, **serializer.validated_data)
        return Response(InterviewSerializer(interview).data)

    @extend_schema(tags=[TAG], summary="Cancel it (the candidate is told)", request=CareersReasonSerializer,
                   responses={200: InterviewSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        serializer = CareersReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        interview = services.cancel_interview(self.get_object(), reason=serializer.validated_data["reason"],
                                              by=request.user)
        return Response(InterviewSerializer(interview).data)

    def _visible_to_me(self, pk):
        """The panel reaches its own interviews without careers.view."""
        interview = (Interview.objects.filter(pk=pk, organization_id=self.request.user.organization_id)
                     .select_related("candidacy__vacancy").first())
        if interview is None or not (services.is_panelist(self.request.user, interview)
                                     or services.holds(self.request.user, VIEW,
                                                       interview.candidacy.vacancy.campus_id)):
            raise NotFound()
        return interview

    @extend_schema(tags=[TAG], summary="Record how it went (the panel, or the office)", request=OutcomeSerializer,
                   responses={200: InterviewSerializer})
    @action(detail=True, methods=["post"])
    def outcome(self, request, pk=None):
        interview = self._visible_to_me(pk)
        serializer = OutcomeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        interview = services.record_outcome(interview, by=request.user, **serializer.validated_data)
        return Response(InterviewSerializer(interview).data)

    @extend_schema(tags=[TAG], summary="Interviews I'm on the panel for", responses={200: InterviewSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def mine(self, request):
        staff = staff_member_for_user(request.user)
        qs = Interview.objects.none() if staff is None else (
            Interview.objects.filter(panel=staff).select_related("candidacy__vacancy").prefetch_related("panel"))
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(InterviewSerializer(page, many=True).data)


# ---------------------------------------------------------------------------
# Offers
# ---------------------------------------------------------------------------
@extend_schema_view(
    list=extend_schema(tags=[TAG], summary="List job offers"),
    retrieve=extend_schema(tags=[TAG], summary="Retrieve a job offer"),
    create=extend_schema(tags=[TAG], summary="Make an offer (at the last step; the candidate is told)",
                         request=MakeOfferSerializer, responses={201: JobOfferSerializer}),
)
class JobOfferViewSet(CampusScopedViewSet):
    http_method_names = ["get", "post", "head", "options"]
    campus_field = "candidacy__vacancy__campus"
    queryset = JobOffer.objects.select_related("candidacy__vacancy", "candidacy__application")
    serializer_class = JobOfferSerializer
    audit_module = "careers"
    service_audits_create = True
    filterset_fields = ["candidacy", "status"]
    required_permissions = {**READ, "create": [HIRE], "withdraw": [HIRE]}

    def get_permissions(self):
        if self.action in ("mine", "respond"):
            return [IsAuthenticated()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        serializer = MakeOfferSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        offer = services.make_offer(by=request.user, **serializer.validated_data)
        return Response(JobOfferSerializer(offer).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Withdraw the offer", request=CareersReasonSerializer,
                   responses={200: JobOfferSerializer})
    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        serializer = CareersReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        offer = services.withdraw_offer(self.get_object(), reason=serializer.validated_data["reason"],
                                        by=request.user)
        return Response(JobOfferSerializer(offer).data)

    @extend_schema(tags=[TAG], summary="Offers made to me", responses={200: JobOfferSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def mine(self, request):
        qs = (JobOffer.objects.filter(candidacy__application__applicant=request.user)
              .select_related("candidacy__vacancy", "candidacy__application"))
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(JobOfferSerializer(page, many=True).data)

    @extend_schema(tags=[TAG], summary="Accept or decline an offer made to me", request=RespondOfferSerializer,
                   responses={200: JobOfferSerializer})
    @action(detail=True, methods=["post"])
    def respond(self, request, pk=None):
        offer = (JobOffer.objects.filter(pk=pk, candidacy__application__applicant=request.user)
                 .select_related("candidacy").first())
        if offer is None:
            raise NotFound()
        serializer = RespondOfferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        offer = services.respond_offer(offer, **serializer.validated_data)
        return Response(JobOfferSerializer(offer).data)


# ---------------------------------------------------------------------------
# Job board
# ---------------------------------------------------------------------------
def board_audiences(user) -> list[str] | None:
    """Which postings ``user`` reads: ``None`` for all (staff and the
    office), else the audiences that fit (students, alumni)."""
    if services.holds_anywhere(user, BOARD) or staff_member_for_user(user) is not None:
        return None
    if profile_for_user(user) is not None:
        return [Audience.ALUMNI, Audience.BOTH]
    student = student_for_user(user)
    if student is not None and student.status == "active":
        return [Audience.STUDENTS, Audience.BOTH]
    return []


@extend_schema_view(
    list=extend_schema(tags=[TAG], summary="The job board (moderators also see pending and closed)"),
    retrieve=extend_schema(tags=[TAG], summary="A job posting"),
    create=extend_schema(tags=[TAG], summary="Post a job (alumni: waits for approval)"),
    update=extend_schema(tags=[TAG], summary="Replace a posting (its poster, while pending; or a moderator)"),
    partial_update=extend_schema(tags=[TAG], summary="Update a posting (its poster, while pending; or a moderator)"),
)
class JobPostingViewSet(OrganizationScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                        mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    queryset = JobPosting.objects.all()
    serializer_class = JobPostingSerializer
    permission_classes = [IsAuthenticated, IsSameOrganization]
    filterset_fields = ["kind", "audience", "status"]
    search_fields = ["title", "company", "location"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if self.action in ("review",) or services.holds_anywhere(user, BOARD):
            return qs
        mine = Q(posted_by=user)
        audiences = board_audiences(user)
        today = timezone.localdate()
        listed = Q(status=PostingStatus.APPROVED) & (Q(closes_on__isnull=True) | Q(closes_on__gte=today))
        if audiences is not None:
            listed &= Q(audience__in=audiences)
        return qs.filter(mine | listed)

    def perform_create(self, serializer):
        user = self.request.user
        moderator = services.holds_anywhere(user, BOARD)
        profile = profile_for_user(user)
        if not moderator and profile is None:
            raise PermissionDenied("Alumni and the careers office post jobs.")
        serializer.instance = services.post_job(by=user, can_moderate=moderator,
                                                campus_id=profile.campus_id if profile else None,
                                                **serializer.validated_data)

    def perform_update(self, serializer):
        user = self.request.user
        posting = serializer.instance
        if not services.holds_anywhere(user, BOARD) and not (
                posting.posted_by_id == user.pk and posting.status == PostingStatus.PENDING):
            raise PermissionDenied("Only its poster, while it waits for approval, or a moderator can change it.")
        from core.audit.services import log_update, snapshot

        before = snapshot(posting)
        serializer.save()
        log_update(self.request, posting, before=before, module="careers")

    @extend_schema(tags=[TAG], summary="Approve or reject (moderator)", request=ReviewPostingSerializer,
                   responses={200: JobPostingSerializer})
    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        if not services.holds_anywhere(request.user, BOARD):
            raise PermissionDenied()
        serializer = ReviewPostingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        posting = services.review_posting(self.get_object(), by=request.user, **serializer.validated_data)
        return Response(JobPostingSerializer(posting).data)

    @extend_schema(tags=[TAG], summary="Take it down (its poster or a moderator)", request=None,
                   responses={200: JobPostingSerializer})
    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        posting = self.get_object()
        if posting.posted_by_id != request.user.pk and not services.holds_anywhere(request.user, BOARD):
            raise PermissionDenied("Only its poster or a moderator can take it down.")
        return Response(JobPostingSerializer(services.close_posting(posting, by=request.user)).data)

    @extend_schema(tags=[TAG], summary="Postings I made", responses={200: JobPostingSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def mine(self, request):
        page = self.paginate_queryset(JobPosting.objects.filter(posted_by=request.user))
        return self.get_paginated_response(JobPostingSerializer(page, many=True).data)


# ---------------------------------------------------------------------------
# Public (no account)
# ---------------------------------------------------------------------------
class PublicView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "public_applications"

    def organization(self, code):
        organization = Organization.objects.filter(code=str(code).lower(), is_active=True).first()
        if organization is None:
            raise NotFound()
        return organization

    def vacancies(self, organization):
        return _accepting(Vacancy.objects.filter(organization=organization, is_public=True)
                          .select_related("campus", "department", "application_type"))


class PublicVacanciesView(PublicView):
    throttle_scope = "public_read"

    @extend_schema(tags=[PUBLIC_TAG], summary="Open vacancies", responses={200: PublicVacancySerializer(many=True)})
    def get(self, request, code):
        return Response(PublicVacancySerializer(self.vacancies(self.organization(code)), many=True).data)


class PublicVacancyView(PublicView):
    throttle_scope = "public_read"

    @extend_schema(tags=[PUBLIC_TAG], summary="One open vacancy", responses={200: PublicVacancySerializer})
    def get(self, request, code, pk):
        vacancy = self.vacancies(self.organization(code)).filter(pk=pk).first()
        if vacancy is None:
            raise NotFound()
        return Response(PublicVacancySerializer(vacancy).data)


class PublicApplyView(PublicView):
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    @extend_schema(tags=[PUBLIC_TAG],
                   summary="Apply (returns your reference and a secret token: keep it to check on it)",
                   request={"application/json": PublicApplySerializer, "multipart/form-data": PublicApplySerializer},
                   responses={201: ApplyResultSerializer})
    def post(self, request, code, pk):
        vacancy = self.vacancies(self.organization(code)).filter(pk=pk).first()
        if vacancy is None:
            raise NotFound()
        serializer = PublicApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        candidacy, token = services.apply(vacancy, raw_data=serializer.validated_data["data"],
                                          upload=serializer.validated_data.get("resume"), public=True)
        return Response({"number": candidacy.application.number, "token": token,
                         "status": candidacy.application.status}, status=status.HTTP_201_CREATED)


class PublicOfferView(PublicView):
    def offer(self, code, data):
        application = applications.find_public(self.organization(code), data["number"], data["token"])
        offer = None
        if application is not None:
            offer = (JobOffer.objects.filter(candidacy__application=application)
                     .select_related("candidacy__vacancy", "candidacy__application").order_by("-pk").first())
        if offer is None:
            raise NotFound("No offer matches that number and token.")
        return offer

    @extend_schema(tags=[PUBLIC_TAG], summary="See your job offer", request=PublicLookupSerializer,
                   responses={200: PublicOfferSerializer})
    def post(self, request, code):
        serializer = PublicLookupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(PublicOfferSerializer(self.offer(code, serializer.validated_data)).data)


class PublicOfferRespondView(PublicOfferView):
    @extend_schema(tags=[PUBLIC_TAG], summary="Accept or decline your job offer",
                   request=PublicRespondOfferSerializer, responses={200: PublicOfferSerializer})
    def post(self, request, code):
        serializer = PublicRespondOfferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        offer = services.respond_offer(self.offer(code, data), accept=data["accept"], note=data["note"])
        return Response(PublicOfferSerializer(offer).data)

