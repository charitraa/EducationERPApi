from django.db.models import Sum
from rest_framework import serializers

from core.common.serializers import ensure_unique_in_organization, ensure_unique_together, target_organization_id
from modules.academics.models import Department, Room
from modules.staff.models import StaffMember
from modules.students.models import Student

from .models import (
    Asset,
    AssetAssignment,
    AssetCondition,
    Disposal,
    DisposalMethod,
    Item,
    ItemCategory,
    ItemKind,
    MaintenanceKind,
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


class OwnedSerializer(serializers.ModelSerializer):
    def own(self, value, label):
        if value is not None and value.organization_id != target_organization_id(self):
            raise serializers.ValidationError(f"Unknown {label}.")
        return value


class OwnedInput(serializers.Serializer):
    """A plain input serializer that can check a related row's tenant."""

    def own(self, value, label):
        if value is not None and value.organization_id != target_organization_id(self):
            raise serializers.ValidationError(f"Unknown {label}.")
        return value


class OwnedLine(serializers.Serializer):
    """A nested line; tenant checks go through the root serializer's view."""

    def own(self, value, label):
        if value is not None and value.organization_id != self.context["view"].get_target_organization_id():
            raise serializers.ValidationError(f"Unknown {label}.")
        return value


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
class ItemCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ItemCategory
        fields = ["id", "organization", "code", "name", "created_at", "updated_at"]
        read_only_fields = ["id", "organization", "created_at", "updated_at"]

    def validate_code(self, value):
        ensure_unique_in_organization(self, "code", value)
        return value


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = ["id", "organization", "name", "contact_person", "phone", "email", "address", "tax_number",
                  "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "organization", "created_at", "updated_at"]


class ItemSerializer(OwnedSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    total_stock = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = ["id", "organization", "category", "category_name", "code", "name", "kind", "unit",
                  "reorder_level", "description", "is_active", "total_stock", "created_at", "updated_at"]
        read_only_fields = ["id", "organization", "created_at", "updated_at"]

    def get_total_stock(self, obj) -> int:
        cached = getattr(obj, "total_stock", None)
        if cached is not None:
            return cached
        return obj.levels.aggregate(n=Sum("quantity"))["n"] or 0

    def validate_category(self, value):
        return self.own(value, "category")

    def validate_code(self, value):
        ensure_unique_in_organization(self, "code", value)
        return value

    def validate(self, attrs):
        if self.instance is not None and attrs.get("kind", self.instance.kind) != self.instance.kind:
            if (self.instance.levels.exists() or self.instance.assets.exists()
                    or PurchaseLine.objects.filter(item=self.instance).exists()):
                raise serializers.ValidationError(
                    {"kind": "This item already has stock, assets or orders, so its kind can't change."})
        return attrs


class StoreSerializer(OwnedSerializer):
    campus_name = serializers.CharField(source="campus.name", read_only=True)

    class Meta:
        model = Store
        fields = ["id", "organization", "campus", "campus_name", "code", "name", "is_active", "created_at",
                  "updated_at"]
        read_only_fields = ["id", "organization", "created_at", "updated_at"]

    def validate_campus(self, value):
        return self.own(value, "campus")

    def validate(self, attrs):
        ensure_unique_together(self, attrs, ["campus", "code"], "This campus already has a store with that code.",
                               extra={"organization_id": target_organization_id(self)})
        # Assets and orders copy their store's campus; moving a store that
        # holds anything would leave them pointing at the old one.
        if (self.instance is not None and "campus" in attrs and attrs["campus"].pk != self.instance.campus_id
                and store_in_use(self.instance)):
            raise serializers.ValidationError(
                {"campus": "This store already holds stock, assets or orders, so its campus can't change."})
        return attrs


def store_in_use(store) -> bool:
    return (store.levels.exists() or store.movements.exists() or store.assets.exists()
            or store.orders.exists() or store.issues.exists())


# ---------------------------------------------------------------------------
# Stock (read models)
# ---------------------------------------------------------------------------
class StockLevelSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    item_code = serializers.CharField(source="item.code", read_only=True)
    unit = serializers.CharField(source="item.unit", read_only=True)
    reorder_level = serializers.IntegerField(source="item.reorder_level", read_only=True)
    store_name = serializers.CharField(source="store.name", read_only=True)
    campus = serializers.IntegerField(source="store.campus_id", read_only=True)
    is_low = serializers.SerializerMethodField()

    class Meta:
        model = StockLevel
        fields = ["id", "organization", "item", "item_name", "item_code", "unit", "reorder_level", "store",
                  "store_name", "campus", "quantity", "is_low", "updated_at"]
        read_only_fields = fields

    def get_is_low(self, obj) -> bool:
        return bool(obj.item.reorder_level) and obj.quantity <= obj.item.reorder_level


class StockMovementSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    store_name = serializers.CharField(source="store.name", read_only=True)

    class Meta:
        model = StockMovement
        fields = ["id", "organization", "item", "item_name", "store", "store_name", "kind", "delta",
                  "balance_after", "unit_cost", "note", "purchase_line", "stock_issue", "transfer", "created_by",
                  "created_at"]
        read_only_fields = fields


class AdjustStockSerializer(OwnedInput):
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    store = serializers.PrimaryKeyRelatedField(queryset=Store.objects.all())
    delta = serializers.IntegerField()
    reason = serializers.CharField(max_length=255)

    def validate_item(self, value):
        return self.own(value, "item")

    def validate_store(self, value):
        return self.own(value, "store")

    def validate_delta(self, value):
        if value == 0:
            raise serializers.ValidationError("Must not be zero.")
        return value


class StockTransferSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)

    class Meta:
        model = StockTransfer
        fields = ["id", "organization", "item", "item_name", "from_store", "to_store", "quantity", "note",
                  "created_by", "created_at"]
        read_only_fields = fields


class CreateTransferSerializer(OwnedInput):
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    from_store = serializers.PrimaryKeyRelatedField(queryset=Store.objects.all())
    to_store = serializers.PrimaryKeyRelatedField(queryset=Store.objects.all())
    quantity = serializers.IntegerField(min_value=1)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")

    def validate_item(self, value):
        return self.own(value, "item")

    def validate_from_store(self, value):
        return self.own(value, "store")

    def validate_to_store(self, value):
        return self.own(value, "store")


# ---------------------------------------------------------------------------
# Stock issue vouchers
# ---------------------------------------------------------------------------
class StockIssueLineSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)

    class Meta:
        model = StockIssueLine
        fields = ["id", "item", "item_name", "quantity"]
        read_only_fields = fields


class StockIssueSerializer(serializers.ModelSerializer):
    lines = StockIssueLineSerializer(many=True, read_only=True)
    store_name = serializers.CharField(source="store.name", read_only=True)
    recipient_name = serializers.SerializerMethodField()

    class Meta:
        model = StockIssue
        fields = ["id", "organization", "number", "store", "store_name", "staff", "department", "recipient_name",
                  "purpose", "issued_on", "issued_by", "lines", "created_at"]
        read_only_fields = fields

    def get_recipient_name(self, obj) -> str:
        return obj.staff.full_name if obj.staff_id else obj.department.name


class IssueLineInput(OwnedLine):
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    quantity = serializers.IntegerField(min_value=1)

    def validate_item(self, value):
        return self.own(value, "item")


class IssueStockSerializer(OwnedInput):
    store = serializers.PrimaryKeyRelatedField(queryset=Store.objects.all())
    staff = serializers.PrimaryKeyRelatedField(queryset=StaffMember.objects.all(), required=False, allow_null=True)
    department = serializers.PrimaryKeyRelatedField(queryset=Department.objects.all(), required=False,
                                                    allow_null=True)
    purpose = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    issued_on = serializers.DateField(required=False)
    lines = IssueLineInput(many=True, allow_empty=False)

    def validate_store(self, value):
        return self.own(value, "store")

    def validate_staff(self, value):
        return self.own(value, "staff member")

    def validate_department(self, value):
        return self.own(value, "department")


# ---------------------------------------------------------------------------
# Purchasing
# ---------------------------------------------------------------------------
class PurchaseLineSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    outstanding = serializers.IntegerField(read_only=True)

    class Meta:
        model = PurchaseLine
        fields = ["id", "item", "item_name", "quantity", "unit_price", "received_quantity", "outstanding"]
        read_only_fields = fields


class PurchaseOrderSerializer(serializers.ModelSerializer):
    lines = PurchaseLineSerializer(many=True, read_only=True)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)
    store_name = serializers.CharField(source="store.name", read_only=True)
    total = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrder
        fields = ["id", "organization", "number", "supplier", "supplier_name", "store", "store_name", "campus",
                  "status", "ordered_on", "expected_on", "note", "cancelled_reason", "total", "lines",
                  "created_by", "created_at", "updated_at"]
        read_only_fields = fields

    def get_total(self, obj) -> str:
        return str(sum((line.quantity * line.unit_price for line in obj.lines.all()), 0))


class PurchaseLineInput(OwnedLine):
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)

    def validate_item(self, value):
        return self.own(value, "item")


class CreatePurchaseSerializer(OwnedInput):
    supplier = serializers.PrimaryKeyRelatedField(queryset=Supplier.objects.all())
    store = serializers.PrimaryKeyRelatedField(queryset=Store.objects.all())
    expected_on = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    lines = PurchaseLineInput(many=True, allow_empty=False)

    def validate_supplier(self, value):
        return self.own(value, "supplier")

    def validate_store(self, value):
        return self.own(value, "store")


class CancelReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


class ReceiptLineInput(OwnedLine):
    line = serializers.PrimaryKeyRelatedField(queryset=PurchaseLine.objects.all())
    quantity = serializers.IntegerField(min_value=1)

    def validate_line(self, value):
        return self.own(value, "line")


class ReceivePurchaseSerializer(serializers.Serializer):
    lines = ReceiptLineInput(many=True, allow_empty=False)


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------
class AssetSerializer(OwnedSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    store_name = serializers.CharField(source="store.name", read_only=True)
    holder = serializers.SerializerMethodField()

    class Meta:
        model = Asset
        fields = ["id", "organization", "item", "item_name", "store", "store_name", "campus", "tag",
                  "serial_number", "status", "condition", "cost", "purchased_on", "warranty_until",
                  "purchase_line", "note", "holder", "created_at", "updated_at"]
        read_only_fields = ["id", "organization", "campus", "tag", "status", "purchase_line", "created_at",
                            "updated_at"]

    def get_holder(self, obj) -> str | None:
        current = next((a for a in obj.assignments.all() if a.returned_on is None), None)
        return current.holder_name if current else None

    def validate_item(self, value):
        value = self.own(value, "item")
        if value.kind != ItemKind.ASSET:
            raise serializers.ValidationError("Only fixed-asset items get an asset record.")
        return value

    def validate_store(self, value):
        return self.own(value, "store")


class AssetUpdateSerializer(serializers.ModelSerializer):
    """What can change on a live asset. Where it is, who has it and its status
    move only through the assign / return / maintenance / dispose actions."""

    class Meta:
        model = Asset
        fields = ["serial_number", "condition", "cost", "purchased_on", "warranty_until", "note"]

    def validate(self, attrs):
        if self.instance.status == "disposed":
            raise serializers.ValidationError("A disposed asset can't be edited.")
        return attrs


class AssetAssignmentSerializer(serializers.ModelSerializer):
    asset_tag = serializers.CharField(source="asset.tag", read_only=True)
    asset_name = serializers.CharField(source="asset.item.name", read_only=True)
    holder_name = serializers.CharField(read_only=True)

    class Meta:
        model = AssetAssignment
        fields = ["id", "organization", "asset", "asset_tag", "asset_name", "staff", "student", "room", "department",
                  "holder_name", "assigned_on", "returned_on", "returned_condition", "note", "assigned_by",
                  "created_at"]
        read_only_fields = fields


class AssignAssetSerializer(OwnedInput):
    staff = serializers.PrimaryKeyRelatedField(queryset=StaffMember.objects.all(), required=False, allow_null=True)
    student = serializers.PrimaryKeyRelatedField(queryset=Student.objects.all(), required=False, allow_null=True)
    room = serializers.PrimaryKeyRelatedField(queryset=Room.objects.all(), required=False, allow_null=True)
    department = serializers.PrimaryKeyRelatedField(queryset=Department.objects.all(), required=False,
                                                    allow_null=True)
    assigned_on = serializers.DateField(required=False)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")

    def validate_staff(self, value):
        return self.own(value, "staff member")

    def validate_student(self, value):
        return self.own(value, "student")

    def validate_room(self, value):
        return self.own(value, "room")

    def validate_department(self, value):
        return self.own(value, "department")


class MoveAssetSerializer(OwnedInput):
    store = serializers.PrimaryKeyRelatedField(queryset=Store.objects.all())
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")

    def validate_store(self, value):
        return self.own(value, "store")


class ReturnAssetSerializer(serializers.Serializer):
    condition = serializers.ChoiceField(choices=AssetCondition.choices, required=False, allow_blank=True, default="")
    returned_on = serializers.DateField(required=False)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class MaintenanceSerializer(serializers.ModelSerializer):
    asset_tag = serializers.CharField(source="asset.tag", read_only=True)

    class Meta:
        model = MaintenanceRecord
        fields = ["id", "organization", "asset", "asset_tag", "kind", "status", "description", "supplier",
                  "scheduled_on", "started_on", "completed_on", "cost", "outcome", "created_by", "created_at",
                  "updated_at"]
        read_only_fields = fields


class ScheduleMaintenanceSerializer(OwnedInput):
    asset = serializers.PrimaryKeyRelatedField(queryset=Asset.objects.all())
    kind = serializers.ChoiceField(choices=MaintenanceKind.choices)
    description = serializers.CharField(max_length=255)
    supplier = serializers.PrimaryKeyRelatedField(queryset=Supplier.objects.all(), required=False, allow_null=True)
    scheduled_on = serializers.DateField(required=False, allow_null=True)

    def validate_asset(self, value):
        return self.own(value, "asset")

    def validate_supplier(self, value):
        return self.own(value, "supplier")


class CompleteMaintenanceSerializer(serializers.Serializer):
    cost = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True)
    outcome = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    condition = serializers.ChoiceField(choices=AssetCondition.choices, required=False, allow_blank=True, default="")
    completed_on = serializers.DateField(required=False)


class DisposalSerializer(serializers.ModelSerializer):
    asset_tag = serializers.CharField(source="asset.tag", read_only=True)

    class Meta:
        model = Disposal
        fields = ["id", "organization", "asset", "asset_tag", "method", "disposed_on", "proceeds", "reason",
                  "recorded_by", "created_at"]
        read_only_fields = fields


class DisposeAssetSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=DisposalMethod.choices)
    reason = serializers.CharField(max_length=255)
    disposed_on = serializers.DateField(required=False)
    proceeds = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False,
                                        allow_null=True)
