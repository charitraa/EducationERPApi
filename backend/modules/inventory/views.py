from django.db.models import F, Sum
from django.db.models.functions import Coalesce
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.common.exceptions import ConflictError
from core.common.mixins import CampusScopedMixin, CampusScopedViewSet, OrganizationScopedMixin, OrganizationScopedViewSet
from core.common.permissions import HasPermission, IsSameOrganization

from . import selectors, services
from .models import (
    Asset,
    AssetAssignment,
    Disposal,
    Item,
    ItemCategory,
    MaintenanceRecord,
    PurchaseLine,
    PurchaseOrder,
    StockIssue,
    StockIssueLine,
    StockLevel,
    StockMovement,
    StockTransfer,
    Store,
    Supplier,
)
from .serializers import (
    AdjustStockSerializer,
    AssetAssignmentSerializer,
    AssetSerializer,
    AssetUpdateSerializer,
    AssignAssetSerializer,
    CancelReasonSerializer,
    CompleteMaintenanceSerializer,
    CreatePurchaseSerializer,
    CreateTransferSerializer,
    DisposalSerializer,
    DisposeAssetSerializer,
    IssueStockSerializer,
    ItemCategorySerializer,
    ItemSerializer,
    MaintenanceSerializer,
    MoveAssetSerializer,
    PurchaseOrderSerializer,
    ReceivePurchaseSerializer,
    ReturnAssetSerializer,
    ScheduleMaintenanceSerializer,
    StockIssueSerializer,
    StockLevelSerializer,
    StockMovementSerializer,
    StockTransferSerializer,
    StoreSerializer,
    SupplierSerializer,
    store_in_use,
)
from .services import MANAGE, STOCK, VIEW

TAG = "inventory"
READ = {"list": [VIEW], "retrieve": [VIEW]}
WRITE = {"create": [MANAGE], "update": [MANAGE], "partial_update": [MANAGE], "destroy": [MANAGE]}
CRUD_DOCS = dict(list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]), create=extend_schema(tags=[TAG]),
                 update=extend_schema(tags=[TAG]), partial_update=extend_schema(tags=[TAG]),
                 destroy=extend_schema(tags=[TAG]))
READ_DOCS = dict(list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]))


class ReadOnlyCampusViewSet(CampusScopedMixin, OrganizationScopedMixin, mixins.ListModelMixin,
                            mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """List/retrieve only — no create route exists at all, so there is nothing
    for a superuser (who skips ``HasPermission``) to reach either."""

    permission_classes = [HasPermission, IsSameOrganization]
    required_permissions = READ


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
@extend_schema_view(**CRUD_DOCS)
class ItemCategoryViewSet(OrganizationScopedViewSet):
    queryset = ItemCategory.objects.all()
    serializer_class = ItemCategorySerializer
    audit_module = "inventory"
    search_fields = ["name", "code"]
    required_permissions = {**READ, **WRITE}

    def perform_destroy(self, instance):
        if instance.items.exists():
            raise ConflictError("Items still belong to this category.", code="in_use")
        super().perform_destroy(instance)


@extend_schema_view(**CRUD_DOCS)
class SupplierViewSet(OrganizationScopedViewSet):
    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer
    audit_module = "inventory"
    filterset_fields = ["is_active"]
    search_fields = ["name", "contact_person", "phone"]
    required_permissions = {**READ, **WRITE}

    def perform_destroy(self, instance):
        # A soft delete skips the database's PROTECT, so the check lives here.
        if instance.orders.exists() or MaintenanceRecord.objects.filter(supplier=instance).exists():
            raise ConflictError("This supplier has orders or maintenance jobs; deactivate it instead.",
                                code="in_use")
        super().perform_destroy(instance)


@extend_schema_view(**CRUD_DOCS)
class ItemViewSet(OrganizationScopedViewSet):
    queryset = Item.objects.select_related("category")
    serializer_class = ItemSerializer
    audit_module = "inventory"
    filterset_fields = ["category", "kind", "is_active"]
    search_fields = ["name", "code"]
    required_permissions = {**READ, **WRITE}

    def get_queryset(self):
        # Explicit order: Django drops Meta.ordering from a GROUP BY query, which would page unstably.
        return (super().get_queryset().annotate(total_stock=Coalesce(Sum("levels__quantity"), 0))
                .order_by(*Item._meta.ordering))

    def perform_destroy(self, instance):
        if (instance.levels.exists() or instance.movements.exists() or instance.assets.exists()
                or PurchaseLine.objects.filter(item=instance).exists()
                or StockIssueLine.objects.filter(item=instance).exists()):
            raise ConflictError("This item has stock, assets or orders; deactivate it instead.", code="in_use")
        super().perform_destroy(instance)


@extend_schema_view(**CRUD_DOCS)
class StoreViewSet(CampusScopedViewSet):
    queryset = Store.objects.select_related("campus")
    serializer_class = StoreSerializer
    audit_module = "inventory"
    filterset_fields = ["campus", "is_active"]
    search_fields = ["name", "code"]
    required_permissions = {**READ, **WRITE}

    def perform_destroy(self, instance):
        if store_in_use(instance):
            raise ConflictError("This store holds stock, assets or orders; deactivate it instead.", code="in_use")
        super().perform_destroy(instance)


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------
@extend_schema_view(**READ_DOCS)
class StockLevelViewSet(ReadOnlyCampusViewSet):
    campus_field = "store__campus"
    queryset = StockLevel.objects.select_related("item", "store")
    serializer_class = StockLevelSerializer
    filterset_fields = ["item", "store"]
    search_fields = ["item__name", "item__code"]
    required_permissions = {**READ, "adjust": [STOCK]}

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("low") in ("1", "true", "True"):
            qs = qs.filter(item__reorder_level__gt=0, quantity__lte=F("item__reorder_level"))
        return qs

    @extend_schema(tags=[TAG], summary="Correct stock (stock-take, damage, write-off)",
                   request=AdjustStockSerializer, responses={201: StockMovementSerializer})
    @action(detail=False, methods=["post"])
    def adjust(self, request):
        serializer = AdjustStockSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        self.check_campus_allowed(data["store"].campus)
        movement = services.adjust_stock(by=request.user, **data)
        return Response(StockMovementSerializer(movement).data, status=status.HTTP_201_CREATED)


@extend_schema_view(**READ_DOCS)
class StockMovementViewSet(ReadOnlyCampusViewSet):
    campus_field = "store__campus"
    queryset = StockMovement.objects.select_related("item", "store")
    serializer_class = StockMovementSerializer
    filterset_fields = ["item", "store", "kind", "purchase_line", "stock_issue", "transfer"]


@extend_schema_view(list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]),
                    create=extend_schema(tags=[TAG], summary="Move stock between stores",
                                         request=CreateTransferSerializer, responses={201: StockTransferSerializer}))
class StockTransferViewSet(CampusScopedViewSet):
    http_method_names = ["get", "post", "head", "options"]
    campus_field = "from_store__campus"
    queryset = StockTransfer.objects.select_related("item", "from_store", "to_store")
    serializer_class = StockTransferSerializer
    audit_module = "inventory"
    service_audits_create = True
    filterset_fields = ["item", "from_store", "to_store"]
    required_permissions = {**READ, "create": [STOCK]}

    def create(self, request, *args, **kwargs):
        serializer = CreateTransferSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        self.check_campus_allowed(data["from_store"].campus)
        transfer = services.transfer_stock(by=request.user, **data)
        return Response(StockTransferSerializer(transfer).data, status=status.HTTP_201_CREATED)


@extend_schema_view(list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]),
                    create=extend_schema(tags=[TAG], summary="Issue consumables to staff or a department",
                                         request=IssueStockSerializer, responses={201: StockIssueSerializer}))
class StockIssueViewSet(CampusScopedViewSet):
    http_method_names = ["get", "post", "head", "options"]
    campus_field = "store__campus"
    queryset = StockIssue.objects.select_related("store", "staff", "department").prefetch_related("lines__item")
    serializer_class = StockIssueSerializer
    audit_module = "inventory"
    service_audits_create = True
    filterset_fields = ["store", "staff", "department"]
    search_fields = ["number", "purpose"]
    required_permissions = {**READ, "create": [STOCK]}

    def create(self, request, *args, **kwargs):
        serializer = IssueStockSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        self.check_campus_allowed(data["store"].campus)
        issue = services.issue_stock(
            store=data["store"], staff=data.get("staff"), department=data.get("department"),
            purpose=data["purpose"], issued_on=data.get("issued_on"),
            lines=[(line["item"], line["quantity"]) for line in data["lines"]], by=request.user)
        return Response(StockIssueSerializer(issue).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Purchasing
# ---------------------------------------------------------------------------
@extend_schema_view(list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]),
                    create=extend_schema(tags=[TAG], summary="Draft a purchase order",
                                         request=CreatePurchaseSerializer, responses={201: PurchaseOrderSerializer}))
class PurchaseOrderViewSet(CampusScopedViewSet):
    http_method_names = ["get", "post", "head", "options"]
    queryset = PurchaseOrder.objects.select_related("supplier", "store").prefetch_related("lines__item")
    serializer_class = PurchaseOrderSerializer
    audit_module = "inventory"
    service_audits_create = True
    filterset_fields = ["supplier", "store", "campus", "status"]
    search_fields = ["number", "supplier__name"]
    required_permissions = {**READ, "create": [MANAGE], "place": [MANAGE], "cancel": [MANAGE], "receive": [STOCK]}

    def create(self, request, *args, **kwargs):
        serializer = CreatePurchaseSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        self.check_campus_allowed(data["store"].campus)
        order = services.create_purchase(
            supplier=data["supplier"], store=data["store"], expected_on=data.get("expected_on"), note=data["note"],
            lines=[(l["item"], l["quantity"], l["unit_price"]) for l in data["lines"]], by=request.user)
        return Response(PurchaseOrderSerializer(order).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Place a draft order with the supplier", request=None,
                   responses={200: PurchaseOrderSerializer})
    @action(detail=True, methods=["post"])
    def place(self, request, pk=None):
        return Response(PurchaseOrderSerializer(services.place_order(self.get_object(), by=request.user)).data)

    @extend_schema(tags=[TAG], summary="Cancel an order with nothing received", request=CancelReasonSerializer,
                   responses={200: PurchaseOrderSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        serializer = CancelReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = services.cancel_order(self.get_object(), serializer.validated_data["reason"], by=request.user)
        return Response(PurchaseOrderSerializer(order).data)

    @extend_schema(tags=[TAG], summary="Book a delivery (whole or part)", request=ReceivePurchaseSerializer,
                   responses={200: PurchaseOrderSerializer})
    @action(detail=True, methods=["post"])
    def receive(self, request, pk=None):
        order = self.get_object()
        serializer = ReceivePurchaseSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        receipts = [(item["line"], item["quantity"]) for item in serializer.validated_data["lines"]]
        order = services.receive_purchase(order, receipts, by=request.user)
        return Response(PurchaseOrderSerializer(order).data)


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------
@extend_schema_view(
    list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]),
    create=extend_schema(tags=[TAG], summary="Register an asset (donation / opening register)"),
    partial_update=extend_schema(tags=[TAG], summary="Edit an asset's descriptive fields"),
)
class AssetViewSet(CampusScopedViewSet):
    # No PUT and no DELETE: status/location move only through the actions, and
    # an asset leaves the register by being disposed of, never by deletion.
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = Asset.objects.select_related("item", "store").prefetch_related("assignments")
    serializer_class = AssetSerializer
    audit_module = "inventory"
    service_audits_create = True
    filterset_fields = ["item", "store", "campus", "status", "condition"]
    search_fields = ["tag", "serial_number", "item__name"]
    required_permissions = {**READ, "create": [MANAGE], "partial_update": [MANAGE], "assign": [MANAGE],
                            "return_asset": [MANAGE], "move": [MANAGE], "dispose": [MANAGE]}

    def get_permissions(self):
        if self.action == "me":
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_serializer_class(self):
        return AssetUpdateSerializer if self.action == "partial_update" else AssetSerializer

    def create(self, request, *args, **kwargs):
        serializer = AssetSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        self.check_campus_allowed(data["store"].campus)
        asset = services.create_asset(item=data.pop("item"), store=data.pop("store"), by=request.user, **data)
        return Response(AssetSerializer(asset).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Assets currently assigned to me", responses={200: AssetSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def me(self, request):
        qs = selectors.assets_held_by(request.user).prefetch_related("assignments")
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(AssetSerializer(page, many=True).data)

    @extend_schema(tags=[TAG], summary="Give the asset to a staff member, student, room or department",
                   request=AssignAssetSerializer, responses={201: AssetAssignmentSerializer})
    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        asset = self.get_object()
        serializer = AssignAssetSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        assignment = services.assign_asset(asset, by=request.user, **serializer.validated_data)
        return Response(AssetAssignmentSerializer(assignment).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Take the asset back from its holder", request=ReturnAssetSerializer,
                   responses={200: AssetAssignmentSerializer})
    @action(detail=True, methods=["post"], url_path="return")
    def return_asset(self, request, pk=None):
        asset = self.get_object()
        serializer = ReturnAssetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assignment = services.return_asset(asset, by=request.user, **serializer.validated_data)
        return Response(AssetAssignmentSerializer(assignment).data)

    @extend_schema(tags=[TAG], summary="Move the asset to another store (any campus)", request=MoveAssetSerializer,
                   responses={200: AssetSerializer})
    @action(detail=True, methods=["post"])
    def move(self, request, pk=None):
        asset = self.get_object()
        serializer = MoveAssetSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        self.check_campus_allowed(serializer.validated_data["store"].campus)
        asset = services.move_asset(asset, by=request.user, **serializer.validated_data)
        return Response(AssetSerializer(asset).data)

    @extend_schema(tags=[TAG], summary="Dispose of the asset (final)", request=DisposeAssetSerializer,
                   responses={201: DisposalSerializer})
    @action(detail=True, methods=["post"])
    def dispose(self, request, pk=None):
        asset = self.get_object()
        serializer = DisposeAssetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        disposal = services.dispose_asset(asset, by=request.user, **serializer.validated_data)
        return Response(DisposalSerializer(disposal).data, status=status.HTTP_201_CREATED)


@extend_schema_view(**READ_DOCS)
class AssetAssignmentViewSet(ReadOnlyCampusViewSet):
    campus_field = "asset__campus"
    queryset = AssetAssignment.objects.select_related("asset__item", "staff", "student", "room", "department")
    serializer_class = AssetAssignmentSerializer
    filterset_fields = ["asset", "staff", "student", "room", "department"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("active") in ("1", "true", "True"):
            qs = qs.filter(returned_on__isnull=True)
        return qs


@extend_schema_view(list=extend_schema(tags=[TAG]), retrieve=extend_schema(tags=[TAG]),
                    create=extend_schema(tags=[TAG], summary="Schedule maintenance on an asset",
                                         request=ScheduleMaintenanceSerializer, responses={201: MaintenanceSerializer}))
class MaintenanceViewSet(CampusScopedViewSet):
    http_method_names = ["get", "post", "head", "options"]
    campus_field = "asset__campus"
    queryset = MaintenanceRecord.objects.select_related("asset")
    serializer_class = MaintenanceSerializer
    audit_module = "inventory"
    service_audits_create = True
    filterset_fields = ["asset", "kind", "status", "supplier"]
    required_permissions = {**READ, "create": [MANAGE], "start": [MANAGE], "complete": [MANAGE], "cancel": [MANAGE]}

    def create(self, request, *args, **kwargs):
        serializer = ScheduleMaintenanceSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        self.check_campus_allowed(data["asset"].campus)
        record = services.schedule_maintenance(by=request.user, **data)
        return Response(MaintenanceSerializer(record).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=[TAG], summary="Start the job (asset goes under maintenance)", request=None,
                   responses={200: MaintenanceSerializer})
    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        return Response(MaintenanceSerializer(services.start_maintenance(self.get_object(), by=request.user)).data)

    @extend_schema(tags=[TAG], summary="Finish the job (asset returns to store)",
                   request=CompleteMaintenanceSerializer, responses={200: MaintenanceSerializer})
    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        record = self.get_object()
        serializer = CompleteMaintenanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(MaintenanceSerializer(
            services.complete_maintenance(record, by=request.user, **serializer.validated_data)).data)

    @extend_schema(tags=[TAG], summary="Cancel the job", request=CancelReasonSerializer,
                   responses={200: MaintenanceSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        record = self.get_object()
        serializer = CancelReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(MaintenanceSerializer(
            services.cancel_maintenance(record, serializer.validated_data["reason"], by=request.user)).data)


@extend_schema_view(**READ_DOCS)
class DisposalViewSet(ReadOnlyCampusViewSet):
    campus_field = "asset__campus"
    queryset = Disposal.objects.select_related("asset")
    serializer_class = DisposalSerializer
    filterset_fields = ["method", "asset"]
