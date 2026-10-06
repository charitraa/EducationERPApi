from django.db.models import Count, Prefetch, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.common.exceptions import ConflictError
from core.common.mixins import CampusScopedViewSet, OrganizationScopedMixin, OrganizationScopedViewSet
from core.common.permissions import HasPermission, IsSameOrganization
from core.permissions.selectors import campus_ids_with_permission
from modules.students.selectors import student_for_user

from . import services
from .models import (
    Achievement,
    AlumniEvent,
    AlumniProfile,
    Campaign,
    Donation,
    Employment,
    EventStatus,
    HigherStudy,
    Mentorship,
    MentorshipStatus,
    Rsvp,
)
from .serializers import (
    AchievementSerializer,
    AlumniEventSerializer,
    AlumniProfileSerializer,
    CampaignSerializer,
    DirectoryEntrySerializer,
    DonationSerializer,
    EmploymentSerializer,
    GraduateSerializer,
    GraduationResultSerializer,
    HigherStudySerializer,
    MentorCardSerializer,
    MentorshipSerializer,
    MyProfileSerializer,
    MentorshipNoteSerializer,
    OpenCampaignSerializer,
    CancelAlumniEventSerializer,
    RecordDonationSerializer,
    RefundDonationSerializer,
    RequestMentorshipSerializer,
    RsvpInputSerializer,
    RsvpSerializer,
    UpcomingEventSerializer,
)
from .services import DONATIONS, MANAGE, VIEW

TAG = "alumni"
READ = {"list": [VIEW], "retrieve": [VIEW]}
CRUD = {**READ, "create": [MANAGE], "update": [MANAGE], "partial_update": [MANAGE], "destroy": [MANAGE]}


def _schema(noun: str, *actions):
    summaries = {"list": f"List {noun}s", "retrieve": f"Retrieve a {noun}",
                 "create": f"Create a {noun}", "update": f"Replace a {noun}",
                 "partial_update": f"Update a {noun}", "destroy": f"Delete a {noun}"}
    return extend_schema_view(**{a: extend_schema(tags=[TAG], summary=summaries[a])
                                 for a in actions or summaries})


def _public_filters(request, qs):
    """Directory and mentor filters: by name, program, year or campus only,
    never by contact details people didn't choose to show."""
    params = request.query_params
    for field in ("program", "campus"):
        value = params.get(field)
        if value:
            qs = qs.filter(**{f"{field}_id": value}) if value.isdigit() else qs.none()
    if params.get("academic_year"):
        qs = qs.filter(academic_year=params["academic_year"])
    term = (params.get("search") or "").strip()
    if term:
        qs = qs.filter(Q(first_name__icontains=term) | Q(last_name__icontains=term)
                       | Q(program_name__icontains=term) | Q(mentor_topics__icontains=term))
    return qs


def _my_profile(request) -> AlumniProfile:
    profile = services.profile_for_user(request.user)
    if profile is None:
        raise NotFound("You have no alumni profile.")
    return profile


class SharedCampusMixin:
    """For rows whose campus may be empty (shared by every campus): a
    campus-scoped role sees its campuses' rows and the shared ones, but
    only an organization-wide role writes a shared one. CampusScopedMixin's
    plain ``campus__in`` would hide the shared rows altogether."""

    def get_queryset(self):
        qs = super().get_queryset()
        for code in HasPermission().get_required_permissions(self.request, self):
            campus_ids = campus_ids_with_permission(self.request.user, code)
            if campus_ids is not None:
                qs = qs.filter(Q(campus_id__in=campus_ids) | Q(campus__isnull=True))
                if self.action not in ("list", "retrieve"):
                    qs = qs.filter(campus__isnull=False)
        return qs

    def check_scope(self, campus):
        campus_ids = campus_ids_with_permission(self.request.user, MANAGE)
        if campus_ids is None:
            return
        if campus is None:
            raise PermissionDenied("Only an organization-wide role can set this up for every campus.")
        if campus.pk not in campus_ids:
            raise PermissionDenied("Your role does not cover this campus for this action.")

    def perform_create(self, serializer):
        self.check_scope(serializer.validated_data.get("campus"))
        super().perform_create(serializer)

    def perform_update(self, serializer):
        self.check_scope(serializer.validated_data.get("campus", serializer.instance.campus))
        super().perform_update(serializer)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
@_schema("alumni profile")
class AlumniProfileViewSet(CampusScopedViewSet):
    """The office's alumni register. Graduating a student makes their
    profile; ``create`` is for alumni from before the system."""

    queryset = AlumniProfile.objects.select_related("campus", "student").prefetch_related("employments")
    serializer_class = AlumniProfileSerializer
    audit_module = "alumni"
    filterset_fields = ["campus", "program", "academic_year", "is_mentor", "directory_visible"]
    search_fields = ["first_name", "last_name", "email", "phone", "student__student_number"]
    required_permissions = {**CRUD, "graduate": [MANAGE]}

    def get_permissions(self):
        if self.action in ("me", "directory", "mentors"):
            return [IsAuthenticated()]
        return super().get_permissions()

    def perform_destroy(self, instance):
        # A soft delete skips PROTECT, so the checks live here.
        if instance.student_id:
            raise ConflictError("This profile is a graduate's record; it stays.", code="in_use")
        if (instance.donations.exists() or instance.mentees.exists() or instance.mentors.exists()):
            raise ConflictError("Gifts or mentoring are recorded for this person.", code="in_use")
        super().perform_destroy(instance)

    @extend_schema(tags=[TAG], summary="My alumni profile", methods=["GET"], responses={200: MyProfileSerializer})
    @extend_schema(tags=[TAG], summary="Update my alumni profile", methods=["PATCH"], request=MyProfileSerializer,
                   responses={200: MyProfileSerializer})
    @action(detail=False, methods=["get", "patch"])
    def me(self, request):
        profile = _my_profile(request)
        if request.method == "GET":
            return Response(MyProfileSerializer(profile).data)
        serializer = MyProfileSerializer(profile, data=request.data, partial=True,
                                         context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        from core.audit.services import log_update, snapshot

        before = snapshot(profile)
        serializer.save()
        log_update(request, profile, before=before, module="alumni")
        return Response(MyProfileSerializer(profile).data)

    @extend_schema(tags=[TAG], summary="Graduate a class or a list of students", request=GraduateSerializer,
                   responses={200: GraduationResultSerializer})
    @action(detail=False, methods=["post"])
    def graduate(self, request):
        serializer = GraduateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        students = (services.students_in_section(data["section"], data["on_date"]) if data.get("section")
                    else data["students"])
        result = services.graduate(list(students), on_date=data["on_date"], reason=data["reason"], by=request.user)
        return Response(result)

    @extend_schema(tags=[TAG], summary="Alumni directory (those who chose to be listed)",
                   responses={200: DirectoryEntrySerializer(many=True)})
    @action(detail=False, methods=["get"])
    def directory(self, request):
        user = request.user
        if services.profile_for_user(user) is None and not services.holds_anywhere(user, VIEW):
            raise PermissionDenied("The directory is for alumni.")
        qs = (AlumniProfile.objects.filter(organization_id=user.organization_id, directory_visible=True)
              .select_related("campus").prefetch_related("employments"))
        qs = _public_filters(request, qs)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(DirectoryEntrySerializer(page, many=True).data)

    @extend_schema(tags=[TAG], summary="Alumni taking mentees", responses={200: MentorCardSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def mentors(self, request):
        user = request.user
        if (student_for_user(user) is None and services.profile_for_user(user) is None
                and not services.holds_anywhere(user, VIEW)):
            raise PermissionDenied("Mentors are listed for students and alumni.")
        # Without an account nobody could answer a request, so they aren't offered.
        qs = (AlumniProfile.objects.filter(organization_id=user.organization_id, is_mentor=True, user__isnull=False)
              .annotate(active_mentees=Count("mentees", filter=Q(mentees__status=MentorshipStatus.ACCEPTED,
                                                                 mentees__deleted_at__isnull=True)))
              .order_by(*AlumniProfile._meta.ordering).prefetch_related("employments"))
        qs = _public_filters(request, qs)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(MentorCardSerializer(page, many=True).data)


# ---------------------------------------------------------------------------
# A graduate's own history: the graduate, or the office
# ---------------------------------------------------------------------------
class OwnOrOfficeViewSet(OrganizationScopedViewSet):
    """Rows hanging off a profile. The graduate keeps their own; the office
    (``alumni.view`` / ``alumni.manage`` for the profile's campus) sees and
    keeps everyone's in its campuses."""

    permission_classes = [IsAuthenticated, IsSameOrganization]
    audit_module = "alumni"

    def get_queryset(self):
        qs = super().get_queryset().select_related("profile")
        user = self.request.user
        campus_ids = campus_ids_with_permission(user, VIEW)
        if user.is_superuser or campus_ids is None:
            return qs
        own = Q(profile__user=user)
        return qs.filter(own | Q(profile__campus_id__in=campus_ids))

    def _may_write(self, profile) -> None:
        user = self.request.user
        if profile.user_id == user.pk or services.holds(user, MANAGE, profile.campus_id):
            return
        raise PermissionDenied("You can keep only your own record.")

    def perform_create(self, serializer):
        profile = serializer.validated_data.get("profile")
        if profile is None:
            profile = services.profile_for_user(self.request.user)
            if profile is None:
                raise ValidationError({"profile": "Say whose record this is."})
        self._may_write(profile)
        serializer.validated_data["profile"] = profile
        super().perform_create(serializer)

    def perform_update(self, serializer):
        self._may_write(serializer.instance.profile)
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        self._may_write(instance.profile)
        super().perform_destroy(instance)


@_schema("employment record")
class EmploymentViewSet(OwnOrOfficeViewSet):
    queryset = Employment.objects.all()
    serializer_class = EmploymentSerializer
    filterset_fields = ["profile"]
    search_fields = ["employer", "title"]


@_schema("higher study record")
class HigherStudyViewSet(OwnOrOfficeViewSet):
    queryset = HigherStudy.objects.all()
    serializer_class = HigherStudySerializer
    filterset_fields = ["profile", "status"]
    search_fields = ["institution", "qualification", "field"]


@_schema("achievement")
class AchievementViewSet(OwnOrOfficeViewSet):
    queryset = Achievement.objects.all()
    serializer_class = AchievementSerializer
    filterset_fields = ["profile"]
    search_fields = ["title"]


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@_schema("alumni event")
class AlumniEventViewSet(SharedCampusMixin, OrganizationScopedViewSet):
    queryset = AlumniEvent.objects.select_related("campus")
    serializer_class = AlumniEventSerializer
    audit_module = "alumni"
    filterset_fields = ["campus", "status"]
    search_fields = ["title"]
    required_permissions = {**CRUD, "publish": [MANAGE], "cancel": [MANAGE], "rsvps": [VIEW]}

    def get_permissions(self):
        if self.action in ("upcoming", "rsvp"):
            return [IsAuthenticated()]
        return super().get_permissions()

    def perform_destroy(self, instance):
        if instance.status != EventStatus.DRAFT:
            raise ConflictError("Only a draft can be deleted; cancel it instead.", code="not_draft")
        super().perform_destroy(instance)

    @extend_schema(tags=[TAG], summary="Publish the event", request=None, responses={200: AlumniEventSerializer})
    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        event = services.publish_event(self.get_object(), by=request.user)
        return Response(AlumniEventSerializer(event).data)

    @extend_schema(tags=[TAG], summary="Cancel the event (those coming are told)", request=CancelAlumniEventSerializer,
                   responses={200: AlumniEventSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        serializer = CancelAlumniEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        event = services.cancel_event(self.get_object(), reason=serializer.validated_data["reason"],
                                      by=request.user)
        return Response(AlumniEventSerializer(event).data)

    @extend_schema(tags=[TAG], summary="Who is coming", responses={200: RsvpSerializer(many=True)})
    @action(detail=True, methods=["get"])
    def rsvps(self, request, pk=None):
        event = self.get_object()
        return Response(RsvpSerializer(event.rsvps.select_related("profile"), many=True).data)

    @extend_schema(tags=[TAG], summary="Events I can come to", responses={200: UpcomingEventSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def upcoming(self, request):
        profile = _my_profile(request)
        qs = (AlumniEvent.objects.filter(organization_id=profile.organization_id, status=EventStatus.PUBLISHED,
                                         starts_at__gt=timezone.now())
              .filter(Q(campus__isnull=True) | Q(campus_id=profile.campus_id))
              .select_related("campus").prefetch_related(Prefetch("rsvps", queryset=Rsvp.objects.all()))
              .order_by("starts_at"))
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(
            UpcomingEventSerializer(page, many=True, context={"profile": profile}).data)

    @extend_schema(tags=[TAG], summary="Say whether I'm coming", request=RsvpInputSerializer,
                   responses={200: RsvpSerializer})
    @action(detail=True, methods=["post"])
    def rsvp(self, request, pk=None):
        profile = _my_profile(request)
        event = AlumniEvent.objects.filter(pk=pk, organization_id=profile.organization_id).first()
        if event is None or not services.event_open_to(event, profile):
            raise NotFound()
        serializer = RsvpInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = services.rsvp(event, profile, **serializer.validated_data)
        return Response(RsvpSerializer(row).data)


# ---------------------------------------------------------------------------
# Mentoring
# ---------------------------------------------------------------------------
@extend_schema_view(
    list=extend_schema(tags=[TAG], summary="Mentoring I'm part of (the office: everyone's at its campuses)"),
    retrieve=extend_schema(tags=[TAG], summary="A mentoring request"),
    create=extend_schema(tags=[TAG], summary="Ask an alumnus to mentor me", request=RequestMentorshipSerializer,
                         responses={201: MentorshipSerializer}),
)
class MentorshipViewSet(OrganizationScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                        mixins.CreateModelMixin, viewsets.GenericViewSet):
    queryset = Mentorship.objects.select_related("mentor__user", "mentee__user", "student__user")
    serializer_class = MentorshipSerializer
    permission_classes = [IsAuthenticated, IsSameOrganization]
    filterset_fields = ["status", "mentor"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        campus_ids = campus_ids_with_permission(user, VIEW)
        if user.is_superuser or campus_ids is None:
            return qs
        mine = Q(mentor__user=user) | Q(mentee__user=user) | Q(student__user=user)
        return qs.filter(mine | Q(mentor__campus_id__in=campus_ids))

    def create(self, request, *args, **kwargs):
        serializer = RequestMentorshipSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        student = student_for_user(request.user)
        mentee = services.profile_for_user(request.user)
        if mentee is not None:
            student = None  # a graduate asks as a graduate
        elif student is None or student.status != "active":
            raise PermissionDenied("Mentoring is for students and alumni.")
        row = services.request_mentorship(student=student, mentee=mentee, **serializer.validated_data)
        return Response(MentorshipSerializer(row).data, status=status.HTTP_201_CREATED)

    def _as_mentor(self, row):
        if row.mentor.user_id != self.request.user.pk:
            raise PermissionDenied("Only the mentor can answer this.")

    def _party(self, row) -> str:
        user = self.request.user
        if row.mentor.user_id == user.pk:
            return "mentor"
        if (row.mentee_id and row.mentee.user_id == user.pk) or (row.student_id and row.student.user_id == user.pk):
            return "mentee"
        raise PermissionDenied("Only the mentor or the mentee can do this.")

    @extend_schema(tags=[TAG], summary="Accept (mentor)", request=MentorshipNoteSerializer, responses={200: MentorshipSerializer})
    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        row = self.get_object()
        self._as_mentor(row)
        serializer = MentorshipNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = services.respond_mentorship(row, accept=True, note=serializer.validated_data["note"])
        return Response(MentorshipSerializer(row).data)

    @extend_schema(tags=[TAG], summary="Decline (mentor)", request=MentorshipNoteSerializer, responses={200: MentorshipSerializer})
    @action(detail=True, methods=["post"])
    def decline(self, request, pk=None):
        row = self.get_object()
        self._as_mentor(row)
        serializer = MentorshipNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = services.respond_mentorship(row, accept=False, note=serializer.validated_data["note"])
        return Response(MentorshipSerializer(row).data)

    @extend_schema(tags=[TAG], summary="Withdraw a request, or end a mentorship (either side)",
                   request=MentorshipNoteSerializer, responses={200: MentorshipSerializer})
    @action(detail=True, methods=["post"])
    def end(self, request, pk=None):
        row = self.get_object()
        side = self._party(row)
        serializer = MentorshipNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = services.close_mentorship(row, by_mentee=side == "mentee", note=serializer.validated_data["note"])
        return Response(MentorshipSerializer(row).data)


# ---------------------------------------------------------------------------
# Giving
# ---------------------------------------------------------------------------
@_schema("donation campaign")
class CampaignViewSet(SharedCampusMixin, OrganizationScopedViewSet):
    queryset = Campaign.objects.select_related("campus")
    serializer_class = CampaignSerializer
    audit_module = "alumni"
    filterset_fields = ["campus", "is_active"]
    search_fields = ["code", "name"]
    required_permissions = CRUD

    def get_permissions(self):
        if self.action == "open":
            return [IsAuthenticated()]
        return super().get_permissions()

    def perform_destroy(self, instance):
        if instance.donations.exists():
            raise ConflictError("Gifts are recorded against this campaign; close it instead.", code="in_use")
        super().perform_destroy(instance)

    @extend_schema(tags=[TAG], summary="Campaigns open for giving", responses={200: OpenCampaignSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def open(self, request):
        today = timezone.localdate()
        qs = (Campaign.objects.filter(organization_id=request.user.organization_id, is_active=True,
                                      starts_on__lte=today)
              .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=today)))
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(OpenCampaignSerializer(page, many=True).data)


@extend_schema_view(
    list=extend_schema(tags=[TAG], summary="List donations"),
    retrieve=extend_schema(tags=[TAG], summary="A donation with its refunds"),
    create=extend_schema(tags=[TAG], summary="Record a donation (a receipt number is issued)",
                         request=RecordDonationSerializer, responses={201: DonationSerializer}),
)
class DonationViewSet(CampusScopedViewSet):
    # Recorded, then refunded if need be — never edited or deleted.
    http_method_names = ["get", "post", "head", "options"]
    queryset = Donation.objects.select_related("campaign").prefetch_related("refunds")
    serializer_class = DonationSerializer
    audit_module = "alumni"
    service_audits_create = True
    filterset_fields = ["campus", "campaign", "donor", "method"]
    search_fields = ["receipt_number", "donor_name", "donor_email", "reference"]
    required_permissions = {"list": [VIEW], "retrieve": [VIEW], "create": [DONATIONS], "refund": [DONATIONS]}

    def get_permissions(self):
        if self.action == "me":
            return [IsAuthenticated()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        serializer = RecordDonationSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        donation = services.record_donation(by=request.user, **serializer.validated_data)
        return Response(DonationSerializer(donation).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Refund part or all of a gift (a new row; the gift stays)",
                   request=RefundDonationSerializer, responses={200: DonationSerializer})
    @action(detail=True, methods=["post"])
    def refund(self, request, pk=None):
        donation = self.get_object()
        serializer = RefundDonationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.refund_donation(donation, by=request.user, **serializer.validated_data)
        return Response(DonationSerializer(self.get_queryset().get(pk=donation.pk)).data)

    @extend_schema(tags=[TAG], summary="My gifts", responses={200: DonationSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def me(self, request):
        profile = _my_profile(request)
        qs = Donation.objects.filter(donor=profile).select_related("campaign").prefetch_related("refunds")
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(DonationSerializer(page, many=True).data)

