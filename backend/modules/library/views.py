from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.common.exceptions import ConflictError
from core.common.mixins import CampusScopedViewSet, OrganizationScopedViewSet
from core.common.permissions import IsSameOrganization
from core.permissions.selectors import campus_ids_with_permission

from . import selectors, services
from .models import Author, Book, Category, Copy, Fine, Issue, IssueStatus, Member, Publisher, Reservation, Shelf
from .serializers import (
    AuthorSerializer,
    BookSerializer,
    CancelReservationSerializer,
    CategorySerializer,
    CopySerializer,
    FineSerializer,
    IssueBookSerializer,
    IssueSerializer,
    MemberSerializer,
    PublisherSerializer,
    ReserveBookSerializer,
    ReservationSerializer,
    ReturnBookSerializer,
    ShelfSerializer,
    WaiveFineSerializer,
)
from .services import CIRCULATE, MANAGE

TAG = "library"


def _own_member(request) -> Member | None:
    return selectors.member_for_user(request.user)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
@extend_schema_view(
    list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]), create=extend_schema(tags=[TAG]),
    update=extend_schema(tags=[TAG]), partial_update=extend_schema(tags=[TAG]), destroy=extend_schema(tags=[TAG]),
)
class AuthorViewSet(OrganizationScopedViewSet):
    queryset = Author.objects.all()
    serializer_class = AuthorSerializer
    audit_module = "library"
    search_fields = ["name"]
    required_permissions = {"create": [MANAGE], "update": [MANAGE], "partial_update": [MANAGE],
                            "destroy": [MANAGE]}

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsSameOrganization()]
        return super().get_permissions()

    def perform_destroy(self, instance):
        # A soft delete skips the database's PROTECT, so the check lives here.
        if Book.objects.filter(authors=instance).exists():
            raise ConflictError("Books in the catalog list this author.", code="in_use")
        super().perform_destroy(instance)


@extend_schema_view(
    list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]), create=extend_schema(tags=[TAG]),
    update=extend_schema(tags=[TAG]), partial_update=extend_schema(tags=[TAG]), destroy=extend_schema(tags=[TAG]),
)
class CategoryViewSet(OrganizationScopedViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    audit_module = "library"
    search_fields = ["name"]
    required_permissions = {"create": [MANAGE], "update": [MANAGE], "partial_update": [MANAGE],
                            "destroy": [MANAGE]}

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsSameOrganization()]
        return super().get_permissions()

    def perform_destroy(self, instance):
        if Book.objects.filter(category=instance).exists():
            raise ConflictError("Books in the catalog use this category.", code="in_use")
        super().perform_destroy(instance)


@extend_schema_view(
    list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]), create=extend_schema(tags=[TAG]),
    update=extend_schema(tags=[TAG]), partial_update=extend_schema(tags=[TAG]), destroy=extend_schema(tags=[TAG]),
)
class PublisherViewSet(OrganizationScopedViewSet):
    queryset = Publisher.objects.all()
    serializer_class = PublisherSerializer
    audit_module = "library"
    search_fields = ["name"]
    required_permissions = {"create": [MANAGE], "update": [MANAGE], "partial_update": [MANAGE],
                            "destroy": [MANAGE]}

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsSameOrganization()]
        return super().get_permissions()

    def perform_destroy(self, instance):
        if Book.objects.filter(publisher=instance).exists():
            raise ConflictError("Books in the catalog name this publisher.", code="in_use")
        super().perform_destroy(instance)


@extend_schema_view(
    list=extend_schema(tags=[TAG], summary="Browse the catalog"),
    retrieve=extend_schema(tags=[TAG]), create=extend_schema(tags=[TAG]), update=extend_schema(tags=[TAG]),
    partial_update=extend_schema(tags=[TAG]), destroy=extend_schema(tags=[TAG]),
)
class BookViewSet(OrganizationScopedViewSet):
    queryset = Book.objects.select_related("category", "publisher").prefetch_related("authors")
    serializer_class = BookSerializer
    audit_module = "library"
    filterset_fields = ["category", "publisher", "language"]
    search_fields = ["title", "isbn"]
    required_permissions = {"create": [MANAGE], "update": [MANAGE], "partial_update": [MANAGE],
                            "destroy": [MANAGE]}

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsSameOrganization()]
        return super().get_permissions()

    def perform_destroy(self, instance):
        if Copy.objects.filter(book=instance).exists() or Reservation.objects.filter(book=instance).exists():
            raise ConflictError("This book has copies or reservations.", code="in_use")
        super().perform_destroy(instance)


@extend_schema_view(
    list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]), create=extend_schema(tags=[TAG]),
    update=extend_schema(tags=[TAG]), partial_update=extend_schema(tags=[TAG]), destroy=extend_schema(tags=[TAG]),
)
class ShelfViewSet(CampusScopedViewSet):
    queryset = Shelf.objects.select_related("campus")
    serializer_class = ShelfSerializer
    audit_module = "library"
    filterset_fields = ["campus"]
    required_permissions = {"list": [MANAGE], "retrieve": [MANAGE], "create": [MANAGE], "update": [MANAGE],
                            "partial_update": [MANAGE], "destroy": [MANAGE]}

    def perform_destroy(self, instance):
        if Copy.objects.filter(shelf=instance).exists():
            raise ConflictError("Copies are on this shelf; move them first.", code="in_use")
        super().perform_destroy(instance)


@extend_schema_view(
    list=extend_schema(tags=[TAG], summary="Check availability"),
    retrieve=extend_schema(tags=[TAG]), create=extend_schema(tags=[TAG]), update=extend_schema(tags=[TAG]),
    partial_update=extend_schema(tags=[TAG]), destroy=extend_schema(tags=[TAG]),
)
class CopyViewSet(CampusScopedViewSet):
    queryset = Copy.objects.select_related("book", "campus", "shelf")
    serializer_class = CopySerializer
    audit_module = "library"
    service_audits_create = True
    filterset_fields = ["book", "campus", "shelf", "status"]
    # The desk finds a copy by the accession number on its label.
    search_fields = ["accession_number", "book__title", "book__isbn"]
    required_permissions = {"create": [MANAGE], "update": [MANAGE], "partial_update": [MANAGE],
                            "destroy": [MANAGE], "withdraw": [MANAGE]}

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsSameOrganization()]
        return super().get_permissions()

    def perform_destroy(self, instance):
        # Its loans, fines and reservations would point at a copy nobody can
        # see. Withdrawing takes it out of circulation and keeps the history.
        if Issue.objects.filter(copy=instance).exists() or Reservation.objects.filter(copy=instance).exists():
            raise ConflictError("This copy has been lent or held; withdraw it instead.", code="in_use")
        super().perform_destroy(instance)

    def create(self, request, *args, **kwargs):
        serializer = CopySerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        self.check_campus_allowed(data["campus"])
        copy = services.add_copy(by=request.user, **data)
        return Response(CopySerializer(copy).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Withdraw the copy from circulation", request=None,
                   responses={200: CopySerializer})
    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        copy = services.withdraw_copy(self.get_object(), by=request.user)
        return Response(CopySerializer(copy).data)


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------
@extend_schema_view(
    list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]), create=extend_schema(tags=[TAG]),
    update=extend_schema(tags=[TAG]), partial_update=extend_schema(tags=[TAG]),
)
class MemberViewSet(CampusScopedViewSet):
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = Member.objects.select_related("campus", "student", "staff")
    serializer_class = MemberSerializer
    audit_module = "library"
    service_audits_create = True
    filterset_fields = ["campus", "membership_type", "is_active"]
    search_fields = ["member_number", "student__first_name", "student__last_name", "student__student_number",
                     "staff__first_name", "staff__last_name", "staff__employee_number"]
    required_permissions = {"list": [MANAGE], "retrieve": [MANAGE], "create": [MANAGE], "update": [MANAGE],
                            "partial_update": [MANAGE], "deactivate": [MANAGE]}

    def get_permissions(self):
        if self.action == "me":
            return [IsAuthenticated()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        serializer = MemberSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        self.check_campus_allowed(data["campus"])
        member = services.create_member(by=request.user, **data)
        return Response(MemberSerializer(member).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Deactivate the membership", request=None, responses={200: MemberSerializer})
    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        member = services.deactivate_member(self.get_object(), by=request.user)
        return Response(MemberSerializer(member).data)

    @extend_schema(tags=[TAG], summary="My own library membership", responses={200: MemberSerializer})
    @action(detail=False, methods=["get"])
    def me(self, request):
        member = _own_member(request)
        if member is None:
            raise NotFound("You have no library membership.")
        return Response(MemberSerializer(member).data)


# ---------------------------------------------------------------------------
# Circulation
# ---------------------------------------------------------------------------
def _campus_scoped_own(qs, request, campus_field, own_q):
    user = request.user
    if user.is_platform_admin:
        return qs
    campus_ids = campus_ids_with_permission(user, CIRCULATE)
    if campus_ids is None:
        return qs
    if campus_ids:
        return qs.filter(Q(**{f"{campus_field}__in": campus_ids}) | own_q(user))
    return qs.filter(own_q(user))


@extend_schema_view(
    list=extend_schema(tags=[TAG], parameters=[
        OpenApiParameter("overdue", bool, description="Only loans still out past their due time.")]),
    retrieve=extend_schema(tags=[TAG]),
    create=extend_schema(tags=[TAG], summary="Issue a copy", request=IssueBookSerializer,
                         responses={201: IssueSerializer}),
)
class IssueViewSet(OrganizationScopedViewSet):
    http_method_names = ["get", "post", "head", "options"]
    queryset = Issue.objects.select_related("copy__book", "member")
    serializer_class = IssueSerializer
    audit_module = "library"
    service_audits_create = True
    filterset_fields = ["copy", "member", "status"]
    search_fields = ["copy__accession_number", "copy__book__title", "member__member_number"]
    required_permissions = {"create": [CIRCULATE], "return_copy": [CIRCULATE]}

    def get_permissions(self):
        if self.action in ("list", "retrieve", "me"):
            return [IsAuthenticated(), IsSameOrganization()]
        return super().get_permissions()

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("overdue") in ("1", "true", "True"):
            qs = qs.filter(status=IssueStatus.ISSUED, due_at__lt=timezone.now())
        return _campus_scoped_own(qs, self.request, "copy__campus", lambda u: Q(member__student__user=u) | Q(member__staff__user=u))

    def create(self, request, *args, **kwargs):
        serializer = IssueBookSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.ensure_can_circulate(request.user, data["copy"].campus_id)
        issue = services.issue_book(by=request.user, **data)
        return Response(IssueSerializer(issue).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Return, or report damaged/lost", request=ReturnBookSerializer,
                   responses={200: IssueSerializer})
    @action(detail=True, methods=["post"], url_path="return")
    def return_copy(self, request, pk=None):
        issue = self.get_object()
        services.ensure_can_circulate(request.user, issue.copy.campus_id)
        serializer = ReturnBookSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        issue = services.return_book(issue=issue, by=request.user, **serializer.validated_data)
        return Response(IssueSerializer(issue).data)

    @extend_schema(tags=[TAG], summary="My own borrowing history", responses={200: IssueSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def me(self, request):
        member = _own_member(request)
        if member is None:
            raise NotFound("You have no library membership.")
        return Response(IssueSerializer(member.issues.select_related("copy__book"), many=True).data)


@extend_schema_view(
    list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]),
    create=extend_schema(tags=[TAG], summary="Reserve a book with nothing available right now",
                         request=ReserveBookSerializer, responses={201: ReservationSerializer}),
)
class ReservationViewSet(OrganizationScopedViewSet):
    http_method_names = ["get", "post", "head", "options"]
    queryset = Reservation.objects.select_related("book", "member", "copy")
    serializer_class = ReservationSerializer
    audit_module = "library"
    service_audits_create = True
    filterset_fields = ["book", "member", "status"]
    required_permissions = {"expire_stale": [MANAGE], "fulfil": [CIRCULATE]}

    def get_permissions(self):
        if self.action in ("expire_stale", "fulfil"):
            return super().get_permissions()
        return [IsAuthenticated(), IsSameOrganization()]

    def get_queryset(self):
        qs = super().get_queryset()
        return _campus_scoped_own(qs, self.request, "member__campus", lambda u: Q(member__student__user=u) | Q(member__staff__user=u))

    def create(self, request, *args, **kwargs):
        serializer = ReserveBookSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.ensure_self_or_circulate(request.user, data["member"])
        reservation = services.reserve_book(by=request.user, **data)
        return Response(ReservationSerializer(reservation).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Cancel the reservation", request=CancelReservationSerializer,
                   responses={200: ReservationSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        reservation = self.get_object()
        services.ensure_self_or_circulate(request.user, reservation.member)
        serializer = CancelReservationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reservation = services.cancel_reservation(reservation, serializer.validated_data["reason"], by=request.user)
        return Response(ReservationSerializer(reservation).data)

    @extend_schema(tags=[TAG], summary="Collect the copy being held (the desk hands it over)",
                   request=None, responses={200: IssueSerializer})
    @action(detail=True, methods=["post"])
    def fulfil(self, request, pk=None):
        reservation = self.get_object()
        services.ensure_can_circulate(request.user, reservation.member.campus_id)
        issue = services.fulfil_reservation(reservation, by=request.user)
        return Response(IssueSerializer(issue).data)

    @extend_schema(tags=[TAG], summary="Sweep reservations held past their pickup window",
                   description="Idempotent, like finance's assess-late-fees — not a cron.",
                   request=None, responses={200: None})
    @action(detail=False, methods=["post"], url_path="expire-stale")
    def expire_stale(self, request):
        return Response(services.expire_stale_reservations(by=request.user))

    @extend_schema(tags=[TAG], summary="My own reservations", responses={200: ReservationSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def me(self, request):
        member = _own_member(request)
        if member is None:
            raise NotFound("You have no library membership.")
        return Response(ReservationSerializer(member.reservations.select_related("book"), many=True).data)


@extend_schema_view(list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]))
class FineViewSet(OrganizationScopedViewSet):
    http_method_names = ["get", "post", "head", "options"]
    queryset = Fine.objects.select_related("member", "issue__copy__book")
    serializer_class = FineSerializer
    audit_module = "library"
    filterset_fields = ["member", "status", "category"]
    required_permissions = {"pay": [CIRCULATE], "waive": [CIRCULATE]}

    def get_permissions(self):
        if self.action in ("list", "retrieve", "me"):
            return [IsAuthenticated(), IsSameOrganization()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        # Fines are raised by returns only. POST is here for pay/waive; this
        # stops a superuser (who skips the permission check) reaching create.
        raise MethodNotAllowed("POST")

    def get_queryset(self):
        qs = super().get_queryset()
        return _campus_scoped_own(qs, self.request, "member__campus", lambda u: Q(member__student__user=u) | Q(member__staff__user=u))

    @extend_schema(tags=[TAG], summary="Record payment of the fine", request=None, responses={200: FineSerializer})
    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        fine = self.get_object()
        services.ensure_can_circulate(request.user, fine.member.campus_id)
        fine = services.pay_fine(fine, by=request.user)
        return Response(FineSerializer(fine).data)

    @extend_schema(tags=[TAG], summary="Waive the fine", request=WaiveFineSerializer, responses={200: FineSerializer})
    @action(detail=True, methods=["post"])
    def waive(self, request, pk=None):
        fine = self.get_object()
        services.ensure_can_circulate(request.user, fine.member.campus_id)
        serializer = WaiveFineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        fine = services.waive_fine(fine, serializer.validated_data["reason"], by=request.user)
        return Response(FineSerializer(fine).data)

    @extend_schema(tags=[TAG], summary="My own fines", responses={200: FineSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def me(self, request):
        member = _own_member(request)
        if member is None:
            raise NotFound("You have no library membership.")
        return Response(FineSerializer(member.fines.all(), many=True).data)
