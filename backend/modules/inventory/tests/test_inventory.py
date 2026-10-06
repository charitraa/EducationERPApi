from datetime import date
from decimal import Decimal

from core.common.exceptions import ConflictError
from modules.academics.models import Department
from modules.inventory import services
from modules.inventory.models import (
    Asset,
    AssetStatus,
    Item,
    ItemCategory,
    ItemKind,
    PurchaseStatus,
    StockLevel,
    StockMovement,
    Store,
    Supplier,
)
from modules.notifications.models import Notification
from tests.base import APITestCaseBase
from tests.factories import (
    create_campus,
    create_organization,
    create_role,
    create_room,
    create_staff_member,
    create_student,
    create_user,
    grant,
    user_with_permissions,
    user_with_system_role,
)

API = "/api/v1/inventory"


class InventoryTestCase(APITestCaseBase):
    def setUp(self):
        self.org = create_organization(code="kmc")
        self.campus = create_campus(self.org, code="main")
        self.branch = create_campus(self.org, code="branch")
        self.office = user_with_system_role(self.org, "campus-admin", email="office@kmc.test", campus=self.campus)
        self.keeper = user_with_permissions(self.org, ["inventory.stock", "inventory.view"], email="keeper@kmc.test")
        self.viewer = user_with_permissions(self.org, ["inventory.view"], email="viewer@kmc.test")
        self.branch_office = user_with_system_role(self.org, "campus-admin", email="branch@kmc.test",
                                                   campus=self.branch)

        self.staff_user = create_user(self.org, email="teacher@kmc.test", user_type="staff")
        self.staff = create_staff_member(self.campus, employee_number="E-1", first_name="Tara", user=self.staff_user)
        self.student_user = create_user(self.org, email="ram@kmc.test", user_type="student")
        self.student = create_student(self.campus, student_number="S-1", first_name="Ram", user=self.student_user)
        self.dept = Department.objects.create(organization=self.org, code="sci", name="Science")
        self.room = create_room(self.campus)

        self.category = ItemCategory.objects.create(organization=self.org, code="stationery", name="Stationery")
        self.chalk = Item.objects.create(organization=self.org, category=self.category, code="chalk", name="Chalk",
                                         unit="box", reorder_level=5)
        self.pens = Item.objects.create(organization=self.org, code="pen", name="Pen", unit="pcs")
        self.laptop = Item.objects.create(organization=self.org, code="laptop", name="Laptop", kind=ItemKind.ASSET)
        self.store = Store.objects.create(organization=self.org, campus=self.campus, code="main", name="Main store")
        self.lab = Store.objects.create(organization=self.org, campus=self.campus, code="lab", name="Lab")
        self.far = Store.objects.create(organization=self.org, campus=self.branch, code="far", name="Branch store")
        self.supplier = Supplier.objects.create(organization=self.org, name="Stationers Ltd")

    def login(self, who):
        self.logout()
        self.authenticate(who)

    def assertError(self, response, status, code):
        self.assertEqual(response.status_code, status, response.data)
        self.assertEqual(response.data["error"]["code"], code, response.data)

    def stock(self, item, store, quantity):
        return services.apply_movement(item=item, store=store, kind="receipt", delta=quantity)

    def level(self, item, store):
        row = StockLevel.objects.filter(item=item, store=store).first()
        return row.quantity if row else 0


class CatalogTests(InventoryTestCase):
    def test_a_student_cannot_see_the_catalog(self):
        self.login(self.student_user)
        self.assertEqual(self.client.get(f"{API}/items/").status_code, 403)

    def test_a_view_only_user_can_read_but_not_write(self):
        self.login(self.viewer)
        self.assertEqual(self.client.get(f"{API}/items/").status_code, 200)
        self.assertEqual(self.client.post(f"{API}/items/", {"code": "x", "name": "X"}).status_code, 403)

    def test_item_codes_are_unique_per_organization(self):
        self.login(self.office)
        r = self.client.post(f"{API}/items/", {"code": "chalk", "name": "Another"})
        self.assertEqual(r.status_code, 400)

    def test_an_items_kind_is_locked_once_it_has_stock(self):
        self.stock(self.chalk, self.store, 10)
        self.login(self.office)
        r = self.client.patch(f"{API}/items/{self.chalk.pk}/", {"kind": "asset"})
        self.assertEqual(r.status_code, 400)

    def test_an_item_shows_its_total_stock(self):
        self.stock(self.chalk, self.store, 10)
        self.stock(self.chalk, self.lab, 4)
        self.login(self.office)
        self.assertEqual(self.client.get(f"{API}/items/{self.chalk.pk}/").data["total_stock"], 14)

    def test_a_store_code_is_unique_within_its_campus(self):
        self.login(self.office)
        r = self.client.post(f"{API}/stores/", {"campus": self.campus.pk, "code": "main", "name": "Dup"})
        self.assertEqual(r.status_code, 400)

    def test_nothing_in_use_can_be_deleted(self):
        # DELETE is a soft delete, which the database's PROTECT never sees.
        self.stock(self.chalk, self.store, 3)
        Asset.objects.create(organization=self.org, item=self.laptop, store=self.lab, campus=self.campus, tag="T1")
        services.create_purchase(supplier=self.supplier, store=self.store, lines=[(self.pens, 1, Decimal("1"))])
        self.login(self.office)
        for url in (f"{API}/items/{self.chalk.pk}/", f"{API}/items/{self.laptop.pk}/", f"{API}/items/{self.pens.pk}/",
                    f"{API}/stores/{self.store.pk}/", f"{API}/stores/{self.lab.pk}/",
                    f"{API}/categories/{self.category.pk}/", f"{API}/suppliers/{self.supplier.pk}/"):
            self.assertError(self.client.delete(url), 409, "in_use")
        unused = Item.objects.create(organization=self.org, code="glue", name="Glue")
        self.assertEqual(self.client.delete(f"{API}/items/{unused.pk}/").status_code, 204)

    def test_a_stores_campus_is_locked_once_it_holds_anything(self):
        self.login(self.office)
        empty = self.client.post(f"{API}/stores/", {"campus": self.campus.pk, "code": "new", "name": "New"}).data
        self.stock(self.chalk, self.store, 1)
        manager = user_with_permissions(self.org, ["inventory.view", "inventory.manage"], email="mgr@kmc.test")
        self.login(manager)
        r = self.client.patch(f"{API}/stores/{self.store.pk}/", {"campus": self.branch.pk})
        self.assertEqual(r.status_code, 400)
        self.assertIn("campus", r.data["error"]["details"])
        ok = self.client.patch(f"{API}/stores/{empty['id']}/", {"campus": self.branch.pk})
        self.assertEqual(ok.status_code, 200, ok.data)

    def test_an_items_kind_is_locked_once_it_is_on_an_order(self):
        services.create_purchase(supplier=self.supplier, store=self.store, lines=[(self.pens, 1, Decimal("1"))])
        self.login(self.office)
        self.assertEqual(self.client.patch(f"{API}/items/{self.pens.pk}/", {"kind": "asset"}).status_code, 400)


class LedgerTests(InventoryTestCase):
    def test_adjustment_needs_a_reason_and_moves_the_level(self):
        self.login(self.keeper)
        payload = {"item": self.chalk.pk, "store": self.store.pk, "delta": 12}
        self.assertEqual(self.client.post(f"{API}/stock-levels/adjust/", payload).status_code, 400)
        r = self.client.post(f"{API}/stock-levels/adjust/", {**payload, "reason": "opening count"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["balance_after"], 12)
        self.assertEqual(self.level(self.chalk, self.store), 12)

    def test_stock_can_never_go_below_zero(self):
        self.stock(self.chalk, self.store, 3)
        self.login(self.keeper)
        r = self.client.post(f"{API}/stock-levels/adjust/", {"item": self.chalk.pk, "store": self.store.pk,
                                                             "delta": -4, "reason": "damage"})
        self.assertError(r, 409, "insufficient_stock")
        self.assertEqual(self.level(self.chalk, self.store), 3)

    def test_a_zero_adjustment_is_refused(self):
        self.login(self.keeper)
        r = self.client.post(f"{API}/stock-levels/adjust/", {"item": self.chalk.pk, "store": self.store.pk,
                                                             "delta": 0, "reason": "x"})
        self.assertEqual(r.status_code, 400)

    def test_an_asset_item_has_no_quantity(self):
        self.login(self.keeper)
        r = self.client.post(f"{API}/stock-levels/adjust/", {"item": self.laptop.pk, "store": self.store.pk,
                                                             "delta": 1, "reason": "x"})
        self.assertError(r, 400, "not_consumable")

    def test_each_movement_records_the_balance_after_it(self):
        self.stock(self.chalk, self.store, 10)
        services.apply_movement(item=self.chalk, store=self.store, kind="adjustment", delta=-3, note="x")
        self.assertEqual(list(StockMovement.objects.order_by("pk").values_list("balance_after", flat=True)), [10, 7])

    def test_the_ledger_and_levels_are_read_only_over_the_api(self):
        self.stock(self.chalk, self.store, 10)
        self.login(self.office)
        level = StockLevel.objects.get()
        move = StockMovement.objects.get()
        self.assertIn(self.client.patch(f"{API}/stock-levels/{level.pk}/", {"quantity": 999}).status_code, (403, 405))
        self.assertIn(self.client.delete(f"{API}/stock-movements/{move.pk}/").status_code, (403, 405))
        self.assertIn(self.client.post(f"{API}/stock-levels/", {}).status_code, (403, 405))
        # A platform superuser skips the permission check, so there the route itself must not exist.
        from tests.factories import create_superuser

        self.login(create_superuser())
        self.assertEqual(self.client.patch(f"{API}/stock-levels/{level.pk}/", {"quantity": 999}).status_code, 405)
        self.assertEqual(self.client.delete(f"{API}/stock-movements/{move.pk}/").status_code, 405)
        self.assertEqual(self.client.post(f"{API}/stock-levels/", {}).status_code, 405)
        self.assertEqual(self.client.post(f"{API}/stock-movements/", {}).status_code, 405)
        level.refresh_from_db()
        self.assertEqual(level.quantity, 10)

    def test_a_campus_admin_elsewhere_cannot_adjust_this_campus(self):
        self.login(self.branch_office)
        r = self.client.post(f"{API}/stock-levels/adjust/", {"item": self.chalk.pk, "store": self.store.pk,
                                                             "delta": 5, "reason": "x"})
        self.assertIn(r.status_code, (403, 404))
        self.assertEqual(self.level(self.chalk, self.store), 0)

    def test_low_stock_alerts_once_when_the_level_crosses_the_reorder_line(self):
        self.stock(self.chalk, self.store, 8)
        self.login(self.keeper)
        adjust = lambda d: self.client.post(f"{API}/stock-levels/adjust/", {
            "item": self.chalk.pk, "store": self.store.pk, "delta": d, "reason": "use"})
        adjust(-2)  # 6: still above 5
        self.assertEqual(Notification.objects.filter(event_type="inventory.low_stock").count(), 0)
        adjust(-2)  # 4: crossed
        first = Notification.objects.filter(event_type="inventory.low_stock").count()
        self.assertGreaterEqual(first, 1)
        adjust(-1)  # 3: already low, no repeat
        self.assertEqual(Notification.objects.filter(event_type="inventory.low_stock").count(), first)

    def test_the_low_filter_lists_only_low_levels(self):
        self.stock(self.chalk, self.store, 2)
        self.stock(self.chalk, self.lab, 50)
        self.login(self.viewer)
        r = self.client.get(f"{API}/stock-levels/?low=true")
        self.assertEqual([row["store"] for row in r.data["results"]], [self.store.pk])


class TransferTests(InventoryTestCase):
    def test_a_transfer_moves_stock_and_writes_two_movements(self):
        self.stock(self.chalk, self.store, 10)
        self.login(self.keeper)
        r = self.client.post(f"{API}/stock-transfers/", {"item": self.chalk.pk, "from_store": self.store.pk,
                                                         "to_store": self.lab.pk, "quantity": 4})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((self.level(self.chalk, self.store), self.level(self.chalk, self.lab)), (6, 4))
        self.assertEqual(StockMovement.objects.filter(transfer_id=r.data["id"]).count(), 2)

    def test_the_same_store_and_too_much_are_refused(self):
        self.stock(self.chalk, self.store, 3)
        self.login(self.keeper)
        same = self.client.post(f"{API}/stock-transfers/", {"item": self.chalk.pk, "from_store": self.store.pk,
                                                            "to_store": self.store.pk, "quantity": 1})
        self.assertEqual(same.status_code, 400)
        over = self.client.post(f"{API}/stock-transfers/", {"item": self.chalk.pk, "from_store": self.store.pk,
                                                            "to_store": self.lab.pk, "quantity": 9})
        self.assertError(over, 409, "insufficient_stock")
        self.assertEqual(self.level(self.chalk, self.store), 3)

    def test_you_need_the_stock_permission_at_both_ends(self):
        self.stock(self.chalk, self.store, 10)
        self.login(self.office)  # campus-admin at main only
        r = self.client.post(f"{API}/stock-transfers/", {"item": self.chalk.pk, "from_store": self.store.pk,
                                                         "to_store": self.far.pk, "quantity": 1})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.level(self.chalk, self.store), 10)


class IssueTests(InventoryTestCase):
    def payload(self, **extra):
        return {"store": self.store.pk, "staff": self.staff.pk, "purpose": "Term start",
                "lines": [{"item": self.chalk.pk, "quantity": 2}, {"item": self.pens.pk, "quantity": 5}], **extra}

    def test_a_voucher_takes_every_line_out_of_the_store(self):
        self.stock(self.chalk, self.store, 10)
        self.stock(self.pens, self.store, 10)
        self.login(self.keeper)
        r = self.client.post(f"{API}/stock-issues/", self.payload())
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(r.data["number"].startswith("SI-"))
        self.assertEqual((self.level(self.chalk, self.store), self.level(self.pens, self.store)), (8, 5))
        self.assertEqual(r.data["recipient_name"], self.staff.full_name)

    def test_one_short_line_rolls_the_whole_voucher_back(self):
        self.stock(self.chalk, self.store, 10)
        self.stock(self.pens, self.store, 1)
        self.login(self.keeper)
        r = self.client.post(f"{API}/stock-issues/", self.payload())
        self.assertError(r, 409, "insufficient_stock")
        self.assertEqual(self.level(self.chalk, self.store), 10)
        self.assertEqual(self.client.get(f"{API}/stock-issues/").data["count"], 0)

    def test_exactly_one_recipient(self):
        self.stock(self.chalk, self.store, 10)
        self.login(self.keeper)
        none = self.client.post(f"{API}/stock-issues/", {"store": self.store.pk,
                                                         "lines": [{"item": self.chalk.pk, "quantity": 1}]})
        both = self.client.post(f"{API}/stock-issues/", {"store": self.store.pk, "staff": self.staff.pk,
                                                         "department": self.dept.pk,
                                                         "lines": [{"item": self.chalk.pk, "quantity": 1}]})
        self.assertError(none, 400, "bad_recipient")
        self.assertError(both, 400, "bad_recipient")

    def test_an_issue_to_a_department_works_and_duplicates_are_refused(self):
        self.stock(self.chalk, self.store, 10)
        self.login(self.keeper)
        ok = self.client.post(f"{API}/stock-issues/", {"store": self.store.pk, "department": self.dept.pk,
                                                       "lines": [{"item": self.chalk.pk, "quantity": 1}]})
        self.assertEqual(ok.status_code, 201, ok.data)
        dup = self.client.post(f"{API}/stock-issues/", {"store": self.store.pk, "department": self.dept.pk,
                                                        "lines": [{"item": self.chalk.pk, "quantity": 1},
                                                                  {"item": self.chalk.pk, "quantity": 1}]})
        self.assertError(dup, 400, "duplicate_item")

    def test_vouchers_cannot_be_edited_or_deleted(self):
        self.stock(self.chalk, self.store, 10)
        self.login(self.keeper)
        pk = self.client.post(f"{API}/stock-issues/", self.payload(lines=[{"item": self.chalk.pk, "quantity": 1}])
                              ).data["id"]
        self.assertIn(self.client.patch(f"{API}/stock-issues/{pk}/", {"purpose": "x"}).status_code, (403, 405))
        self.assertIn(self.client.delete(f"{API}/stock-issues/{pk}/").status_code, (403, 405))


class PurchaseTests(InventoryTestCase):
    def draft(self, **extra):
        self.login(self.office)
        payload = {"supplier": self.supplier.pk, "store": self.store.pk,
                   "lines": [{"item": self.chalk.pk, "quantity": 10, "unit_price": "120.00"},
                             {"item": self.laptop.pk, "quantity": 2, "unit_price": "55000.00"}], **extra}
        r = self.client.post(f"{API}/purchase-orders/", payload)
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_a_draft_cannot_receive_goods(self):
        order = self.draft()
        self.login(self.keeper)
        r = self.client.post(f"{API}/purchase-orders/{order['id']}/receive/",
                             {"lines": [{"line": order["lines"][0]["id"], "quantity": 1}]})
        self.assertError(r, 409, "not_receivable")

    def test_order_total_and_number(self):
        order = self.draft()
        self.assertTrue(order["number"].startswith("PO-"))
        self.assertEqual(Decimal(order["total"]), Decimal("111200.00"))

    def test_receiving_in_two_deliveries_completes_the_order(self):
        order = self.draft()
        self.client.post(f"{API}/purchase-orders/{order['id']}/place/")
        chalk_line, laptop_line = [l["id"] for l in order["lines"]]
        self.login(self.keeper)
        first = self.client.post(f"{API}/purchase-orders/{order['id']}/receive/", {"lines": [
            {"line": chalk_line, "quantity": 6}, {"line": laptop_line, "quantity": 1}]})
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["status"], "partial")
        self.assertEqual(self.level(self.chalk, self.store), 6)
        self.assertEqual(Asset.objects.count(), 1)
        second = self.client.post(f"{API}/purchase-orders/{order['id']}/receive/", {"lines": [
            {"line": chalk_line, "quantity": 4}, {"line": laptop_line, "quantity": 1}]})
        self.assertEqual(second.data["status"], "received")
        self.assertEqual(self.level(self.chalk, self.store), 10)
        self.assertEqual(Asset.objects.count(), 2)
        tags = list(Asset.objects.order_by("tag").values_list("tag", flat=True))
        self.assertEqual(len(set(tags)), 2)

    def test_over_receipt_is_refused_and_changes_nothing(self):
        order = self.draft()
        self.client.post(f"{API}/purchase-orders/{order['id']}/place/")
        self.login(self.keeper)
        r = self.client.post(f"{API}/purchase-orders/{order['id']}/receive/", {"lines": [
            {"line": order["lines"][0]["id"], "quantity": 4}, {"line": order["lines"][1]["id"], "quantity": 3}]})
        self.assertError(r, 409, "over_receipt")
        self.assertEqual(self.level(self.chalk, self.store), 0)
        self.assertEqual(Asset.objects.count(), 0)

    def test_receipt_movements_carry_the_unit_cost(self):
        order = self.draft()
        self.client.post(f"{API}/purchase-orders/{order['id']}/place/")
        self.login(self.keeper)
        self.client.post(f"{API}/purchase-orders/{order['id']}/receive/",
                         {"lines": [{"line": order["lines"][0]["id"], "quantity": 10}]})
        move = StockMovement.objects.get(kind="receipt")
        self.assertEqual(move.unit_cost, Decimal("120.00"))

    def test_cancelling_after_a_delivery_closes_the_order_short(self):
        order = self.draft()
        self.assertEqual(self.client.post(f"{API}/purchase-orders/{order['id']}/cancel/", {}).status_code, 400)
        self.client.post(f"{API}/purchase-orders/{order['id']}/place/")
        self.login(self.keeper)
        self.client.post(f"{API}/purchase-orders/{order['id']}/receive/",
                         {"lines": [{"line": order["lines"][0]["id"], "quantity": 1}]})
        self.login(self.office)
        r = self.client.post(f"{API}/purchase-orders/{order['id']}/cancel/", {"reason": "supplier out of stock"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], PurchaseStatus.CLOSED)
        self.assertEqual(self.level(self.chalk, self.store), 1)
        self.login(self.keeper)
        late = self.client.post(f"{API}/purchase-orders/{order['id']}/receive/",
                                {"lines": [{"line": order["lines"][0]["id"], "quantity": 1}]})
        self.assertError(late, 409, "not_receivable")
        self.login(self.office)
        again = self.client.post(f"{API}/purchase-orders/{order['id']}/cancel/", {"reason": "x"})
        self.assertError(again, 409, "not_cancellable")

    def test_a_placed_order_can_be_cancelled_with_a_reason(self):
        order = self.draft()
        self.client.post(f"{API}/purchase-orders/{order['id']}/place/")
        r = self.client.post(f"{API}/purchase-orders/{order['id']}/cancel/", {"reason": "supplier closed"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], PurchaseStatus.CANCELLED)

    def test_an_order_cannot_be_placed_twice(self):
        order = self.draft()
        self.client.post(f"{API}/purchase-orders/{order['id']}/place/")
        self.assertError(self.client.post(f"{API}/purchase-orders/{order['id']}/place/"), 409, "not_draft")

    def test_lines_need_positive_quantities_and_unique_items(self):
        self.login(self.office)
        dup = self.client.post(f"{API}/purchase-orders/", {"supplier": self.supplier.pk, "store": self.store.pk, "lines": [
            {"item": self.chalk.pk, "quantity": 1, "unit_price": "1"}, {"item": self.chalk.pk, "quantity": 1,
                                                                        "unit_price": "1"}]})
        self.assertError(dup, 400, "duplicate_item")
        zero = self.client.post(f"{API}/purchase-orders/", {"supplier": self.supplier.pk, "store": self.store.pk,
                                                            "lines": [{"item": self.chalk.pk, "quantity": 0,
                                                                       "unit_price": "1"}]})
        self.assertEqual(zero.status_code, 400)

    def test_an_inactive_supplier_is_refused(self):
        self.supplier.is_active = False
        self.supplier.save()
        self.login(self.office)
        r = self.client.post(f"{API}/purchase-orders/", {"supplier": self.supplier.pk, "store": self.store.pk,
                                                         "lines": [{"item": self.chalk.pk, "quantity": 1,
                                                                    "unit_price": "1"}]})
        self.assertError(r, 409, "inactive_supplier")

    def test_the_keeper_cannot_draft_but_the_office_can_receive(self):
        self.login(self.keeper)
        r = self.client.post(f"{API}/purchase-orders/", {"supplier": self.supplier.pk, "store": self.store.pk,
                                                         "lines": [{"item": self.chalk.pk, "quantity": 1,
                                                                    "unit_price": "1"}]})
        self.assertEqual(r.status_code, 403)


class AssetTests(InventoryTestCase):
    def make_asset(self, **extra):
        self.login(self.office)
        r = self.client.post(f"{API}/assets/", {"item": self.laptop.pk, "store": self.store.pk,
                                                "serial_number": "SN1", "cost": "50000.00", **extra})
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_registering_gives_a_unique_tag_and_the_stores_campus(self):
        a, b = self.make_asset(), self.make_asset()
        self.assertNotEqual(a["tag"], b["tag"])
        self.assertEqual(a["campus"], self.campus.pk)
        self.assertEqual(a["status"], "in_store")

    def test_only_asset_kind_items_can_be_registered(self):
        self.login(self.office)
        r = self.client.post(f"{API}/assets/", {"item": self.chalk.pk, "store": self.store.pk})
        self.assertEqual(r.status_code, 400)

    def test_status_and_location_cannot_be_edited_directly(self):
        asset = self.make_asset()
        r = self.client.patch(f"{API}/assets/{asset['id']}/", {"status": "disposed", "store": self.lab.pk,
                                                               "condition": "fair"})
        self.assertEqual(r.status_code, 200)
        refreshed = Asset.objects.get(pk=asset["id"])
        self.assertEqual((refreshed.status, refreshed.store_id, refreshed.condition),
                         ("in_store", self.store.pk, "fair"))

    def test_assets_cannot_be_deleted_or_replaced(self):
        asset = self.make_asset()
        self.assertIn(self.client.delete(f"{API}/assets/{asset['id']}/").status_code, (403, 405))
        self.assertIn(self.client.put(f"{API}/assets/{asset['id']}/", {}).status_code, (403, 405))
        from tests.factories import create_superuser

        self.login(create_superuser())
        self.assertEqual(self.client.delete(f"{API}/assets/{asset['id']}/").status_code, 405)
        self.assertTrue(Asset.objects.filter(pk=asset["id"]).exists())

    def test_assign_then_return_records_the_history(self):
        asset = self.make_asset()
        r = self.client.post(f"{API}/assets/{asset['id']}/assign/", {"staff": self.staff.pk})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.client.get(f"{API}/assets/{asset['id']}/").data["status"], "assigned")
        self.assertEqual(self.client.get(f"{API}/assets/{asset['id']}/").data["holder"], self.staff.full_name)
        back = self.client.post(f"{API}/assets/{asset['id']}/return/", {"condition": "fair"})
        self.assertEqual(back.status_code, 200, back.data)
        got = self.client.get(f"{API}/assets/{asset['id']}/").data
        self.assertEqual((got["status"], got["condition"], got["holder"]), ("in_store", "fair", None))
        history = self.client.get(f"{API}/asset-assignments/?asset={asset['id']}").data
        self.assertEqual(history["count"], 1)
        self.assertEqual(history["results"][0]["asset_name"], asset["item_name"])     # what it is, not just the tag

    def test_an_assigned_asset_cannot_be_assigned_again(self):
        asset = self.make_asset()
        self.client.post(f"{API}/assets/{asset['id']}/assign/", {"staff": self.staff.pk})
        r = self.client.post(f"{API}/assets/{asset['id']}/assign/", {"student": self.student.pk})
        self.assertError(r, 409, "not_in_store")

    def test_exactly_one_holder_and_room_must_be_at_the_assets_campus(self):
        asset = self.make_asset()
        url = f"{API}/assets/{asset['id']}/assign/"
        self.assertError(self.client.post(url, {}), 400, "bad_holder")
        self.assertError(self.client.post(url, {"staff": self.staff.pk, "room": self.room.pk}), 400, "bad_holder")
        branch_room = create_room(self.branch, code="b1")
        self.assertError(self.client.post(url, {"room": branch_room.pk}), 400, "wrong_campus")
        self.assertEqual(self.client.post(url, {"room": self.room.pk}).status_code, 201)

    def test_a_student_can_hold_an_asset_and_sees_it_under_me(self):
        asset = self.make_asset()
        self.client.post(f"{API}/assets/{asset['id']}/assign/", {"student": self.student.pk})
        self.login(self.student_user)
        r = self.client.get(f"{API}/assets/me/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([a["id"] for a in r.data["results"]], [asset["id"]])
        self.assertEqual(self.client.get(f"{API}/assets/").status_code, 403)

    def test_returning_an_unassigned_asset_is_refused(self):
        asset = self.make_asset()
        self.assertError(self.client.post(f"{API}/assets/{asset['id']}/return/", {}), 409, "not_assigned")

    def test_maintenance_takes_the_asset_out_and_back(self):
        asset = self.make_asset()
        job = self.client.post(f"{API}/maintenance/", {"asset": asset["id"], "kind": "repair",
                                                       "description": "Screen flicker",
                                                       "supplier": self.supplier.pk}).data
        self.assertEqual(job["status"], "scheduled")
        self.assertEqual(self.client.post(f"{API}/maintenance/{job['id']}/start/").status_code, 200)
        self.assertEqual(self.client.get(f"{API}/assets/{asset['id']}/").data["status"], "maintenance")
        blocked = self.client.post(f"{API}/assets/{asset['id']}/assign/", {"staff": self.staff.pk})
        self.assertError(blocked, 409, "not_in_store")
        done = self.client.post(f"{API}/maintenance/{job['id']}/complete/", {"cost": "1500.00",
                                                                             "outcome": "Panel replaced",
                                                                             "condition": "good"})
        self.assertEqual(done.status_code, 200, done.data)
        after = self.client.get(f"{API}/assets/{asset['id']}/").data
        self.assertEqual((after["status"], after["condition"]), ("in_store", "good"))

    def test_an_assigned_asset_must_come_back_before_maintenance_starts(self):
        asset = self.make_asset()
        self.client.post(f"{API}/assets/{asset['id']}/assign/", {"staff": self.staff.pk})
        job = self.client.post(f"{API}/maintenance/", {"asset": asset["id"], "kind": "inspection",
                                                       "description": "Yearly"}).data
        self.assertError(self.client.post(f"{API}/maintenance/{job['id']}/start/"), 409, "return_first")

    def test_cancelling_a_running_job_frees_the_asset(self):
        asset = self.make_asset()
        job = self.client.post(f"{API}/maintenance/", {"asset": asset["id"], "kind": "repair",
                                                       "description": "x"}).data
        self.client.post(f"{API}/maintenance/{job['id']}/start/")
        self.assertEqual(self.client.post(f"{API}/maintenance/{job['id']}/cancel/", {}).status_code, 400)
        self.assertEqual(self.client.post(f"{API}/maintenance/{job['id']}/cancel/", {"reason": "parts n/a"}
                                          ).status_code, 200)
        self.assertEqual(self.client.get(f"{API}/assets/{asset['id']}/").data["status"], "in_store")

    def test_disposal_is_final(self):
        asset = self.make_asset()
        r = self.client.post(f"{API}/assets/{asset['id']}/dispose/", {"method": "scrapped", "reason": "Dead"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.client.get(f"{API}/assets/{asset['id']}/").data["status"], "disposed")
        again = self.client.post(f"{API}/assets/{asset['id']}/dispose/", {"method": "sold", "reason": "x"})
        self.assertError(again, 409, "already_disposed")
        self.assertEqual(self.client.patch(f"{API}/assets/{asset['id']}/", {"note": "x"}).status_code, 400)
        self.assertError(self.client.post(f"{API}/assets/{asset['id']}/assign/", {"staff": self.staff.pk}),
                         409, "not_in_store")
        self.assertEqual(self.client.get(f"{API}/disposals/").data["count"], 1)

    def test_an_assigned_asset_cannot_be_disposed_and_a_reason_is_required(self):
        asset = self.make_asset()
        self.assertError(self.client.post(f"{API}/assets/{asset['id']}/dispose/", {"method": "sold", "reason": " "}),
                         400, "invalid")
        self.assertEqual(self.client.post(f"{API}/assets/{asset['id']}/dispose/", {"method": "sold"}).status_code, 400)
        self.client.post(f"{API}/assets/{asset['id']}/assign/", {"staff": self.staff.pk})
        r = self.client.post(f"{API}/assets/{asset['id']}/dispose/", {"method": "sold", "reason": "old"})
        self.assertError(r, 409, "not_in_store")

    def test_disposal_cancels_scheduled_maintenance(self):
        asset = self.make_asset()
        job = self.client.post(f"{API}/maintenance/", {"asset": asset["id"], "kind": "repair",
                                                       "description": "x"}).data
        self.client.post(f"{API}/assets/{asset['id']}/dispose/", {"method": "scrapped", "reason": "gone"})
        self.assertEqual(self.client.get(f"{API}/maintenance/{job['id']}/").data["status"], "cancelled")

    def test_a_branch_admin_cannot_touch_this_campuss_assets(self):
        asset = self.make_asset()
        self.login(self.branch_office)
        self.assertEqual(self.client.get(f"{API}/assets/{asset['id']}/").status_code, 404)
        self.assertEqual(self.client.post(f"{API}/assets/{asset['id']}/assign/", {"staff": self.staff.pk}
                                          ).status_code, 404)

    def test_an_asset_moves_between_stores_and_campuses(self):
        asset = self.make_asset()
        r = self.client.post(f"{API}/assets/{asset['id']}/move/", {"store": self.lab.pk, "note": "to lab"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data["store"], r.data["campus"]), (self.lab.pk, self.campus.pk))
        # The office only covers the main campus, so it can't send the asset to the branch.
        self.assertEqual(self.client.post(f"{API}/assets/{asset['id']}/move/", {"store": self.far.pk}
                                          ).status_code, 403)
        manager = user_with_permissions(self.org, ["inventory.view", "inventory.manage"], email="mgr@kmc.test")
        self.login(manager)
        far = self.client.post(f"{API}/assets/{asset['id']}/move/", {"store": self.far.pk})
        self.assertEqual((far.data["store"], far.data["campus"]), (self.far.pk, self.branch.pk))
        self.assertError(self.client.post(f"{API}/assets/{asset['id']}/move/", {"store": self.far.pk}),
                         400, "same_store")

    def test_an_asset_out_with_a_holder_does_not_move(self):
        asset = self.make_asset()
        self.client.post(f"{API}/assets/{asset['id']}/assign/", {"staff": self.staff.pk})
        self.assertError(self.client.post(f"{API}/assets/{asset['id']}/move/", {"store": self.lab.pk}),
                         409, "not_in_store")

    def test_nothing_is_received_into_an_inactive_store(self):
        self.lab.is_active = False
        self.lab.save()
        self.stock(self.chalk, self.store, 5)
        self.login(self.office)
        self.assertError(self.client.post(f"{API}/assets/", {"item": self.laptop.pk, "store": self.lab.pk}),
                         409, "inactive_store")
        asset = self.make_asset()
        self.assertError(self.client.post(f"{API}/assets/{asset['id']}/move/", {"store": self.lab.pk}),
                         409, "inactive_store")
        self.login(self.keeper)
        self.assertError(self.client.post(f"{API}/stock-transfers/", {
            "item": self.chalk.pk, "from_store": self.store.pk, "to_store": self.lab.pk, "quantity": 1}),
            409, "inactive_store")

    def test_dates_cannot_run_backwards(self):
        asset = self.make_asset()
        self.client.post(f"{API}/assets/{asset['id']}/assign/", {"staff": self.staff.pk,
                                                                 "assigned_on": "2026-09-01"})
        self.assertError(self.client.post(f"{API}/assets/{asset['id']}/return/", {"returned_on": "2026-08-01"}),
                         400, "bad_date")
        self.client.post(f"{API}/assets/{asset['id']}/return/", {"returned_on": "2026-09-02"})
        job = self.client.post(f"{API}/maintenance/", {"asset": asset["id"], "kind": "repair",
                                                       "description": "x"}).data
        self.client.post(f"{API}/maintenance/{job['id']}/start/")
        self.assertError(self.client.post(f"{API}/maintenance/{job['id']}/complete/",
                                          {"completed_on": "2000-01-01"}), 400, "bad_date")

    def test_concurrent_style_double_assignment_is_stopped_by_the_database_too(self):
        from django.db import IntegrityError, transaction

        from modules.inventory.models import AssetAssignment

        asset = Asset.objects.create(organization=self.org, item=self.laptop, store=self.store, campus=self.campus,
                                     tag="AST-X")
        AssetAssignment.objects.create(organization=self.org, asset=asset, staff=self.staff,
                                       assigned_on=date.today())
        with self.assertRaises(IntegrityError), transaction.atomic():
            AssetAssignment.objects.create(organization=self.org, asset=asset, student=self.student,
                                           assigned_on=date.today())
