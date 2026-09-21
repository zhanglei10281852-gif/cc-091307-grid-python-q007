"""领域服务层测试：覆盖需求中的每一项约束。"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from src.errors import (
    ArchivedImmutable,
    IdempotencyKeyReused,
    NotFound,
    PartyMaterialMismatch,
    PermissionDenied,
    ValidationFailed,
    VersionConflict,
)
from src.service import HousingSafetyService, Operator

TODAY = date.today()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()
NEXT_WEEK = (TODAY + timedelta(days=7)).isoformat()
NEXT_YEAR = (TODAY + timedelta(days=365)).isoformat()

PHONE = "13812345678"
ID_CARD = "11010119900307771X"
ZONE_EAST = "城东片区"
ZONE_WEST = "城西片区"


def party_material(**overrides):
    data = {
        "role": "landlord",
        "name": "张三",
        "phone": PHONE,
        "id_card": ID_CARD,
        "cert_expiry": NEXT_YEAR,
    }
    data.update(overrides)
    return data


def hazard_items():
    return [
        {"item": "私拉电线", "result": "fail", "detail": "客厅私拉插线板给电动车充电"},
        {"item": "消防通道", "result": "pass"},
    ]


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="housing-test-")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.svc = HousingSafetyService(self.db_path)
        self.admin = Operator("admin-01", "admin")
        self.grid_east = Operator("grid-east-01", "grid", [ZONE_EAST])
        self.grid_west = Operator("grid-west-01", "grid", [ZONE_WEST])

    def tearDown(self):
        self.svc.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _register_property(self, address="幸福小区3栋2单元501室", zone=ZONE_EAST, **kw):
        data = {"zone": zone, "address": address, "owner_name": "李房主"}
        data.update(kw)
        return self.svc.register_property(data, self.grid_east)


class TestRegistrationAndVersioning(ServiceTestCase):
    def test_register_property_creates_version_1(self):
        prop = self._register_property()
        self.assertTrue(prop["id"].startswith("prop_"))
        self.assertEqual(prop["version"], 1)
        self.assertEqual(prop["status"], "active")

    def test_update_property_bumps_version_and_keeps_history(self):
        prop = self._register_property()
        updated = self.svc.update_property(
            prop["id"], {"owner_name": "王房主"}, expected_version=1, operator=self.grid_east)
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["owner_name"], "王房主")

        history = self.svc.property_history(prop["id"], self.admin)
        prop_revisions = [r for r in history["revisions"] if r["entity_type"] == "property"]
        self.assertEqual(len(prop_revisions), 2)
        self.assertEqual(prop_revisions[0]["change_type"], "register")
        self.assertEqual(prop_revisions[1]["change_type"], "update")
        self.assertEqual(prop_revisions[1]["changes"],
                         {"owner_name": {"from": "李房主", "to": "王房主"}})

    def test_unit_usage_change_is_tracked(self):
        """房间用途变化必须留下历史（隔断间/储藏室改卧室等群租风险）。"""
        prop = self._register_property()
        unit = self.svc.register_unit(prop["id"], {"label": "南卧", "usage": "储藏室"}, self.grid_east)
        self.svc.update_unit(unit["id"], {"usage": "卧室"}, expected_version=1, operator=self.grid_east)

        history = self.svc.property_history(prop["id"], self.admin)
        unit_revisions = [r for r in history["revisions"] if r["entity_type"] == "unit"]
        self.assertEqual(len(unit_revisions), 2)
        self.assertEqual(unit_revisions[1]["changes"],
                         {"usage": {"from": "储藏室", "to": "卧室"}})


class TestFieldLevelRejection(ServiceTestCase):
    def test_expired_certificate_rejected(self):
        prop = self._register_property()
        with self.assertRaises(ValidationFailed) as ctx:
            self.svc.register_party(prop["id"], party_material(cert_expiry=YESTERDAY), self.grid_east)
        self.assertIn("cert_expiry", ctx.exception.fields)
        self.assertIn("过期", ctx.exception.fields["cert_expiry"])

    def test_agent_without_authorization_rejected(self):
        prop = self._register_property()
        with self.assertRaises(ValidationFailed) as ctx:
            self.svc.register_party(
                prop["id"],
                party_material(role="agent", name="某中介", id_card="11010119900307772X",
                               authorization_no=None),
                self.grid_east)
        self.assertIn("authorization_no", ctx.exception.fields)
        self.assertIn("授权", ctx.exception.fields["authorization_no"])

    def test_address_conflict_rejected(self):
        """同一套房屋被房东/中介/租客重复登记 → 地址冲突拒绝并给出字段原因。"""
        self._register_property(address="幸福小区 3栋 2单元 501室")
        with self.assertRaises(ValidationFailed) as ctx:
            # 全角空格、换行、大小写差异归一化后仍命中同一地址
            self._register_property(address="幸福小区3栋2单元501室", owner_name="中介小王")
        self.assertIn("address", ctx.exception.fields)
        self.assertIn("地址冲突", ctx.exception.fields["address"])

    def test_multiple_field_errors_returned_together(self):
        prop = self._register_property()
        with self.assertRaises(ValidationFailed) as ctx:
            self.svc.register_party(
                prop["id"],
                {"role": "agent", "name": "", "phone": "123", "id_card": "abc",
                 "cert_expiry": YESTERDAY},
                self.grid_east)
        fields = ctx.exception.fields
        for key in ("name", "phone", "id_card", "cert_expiry", "authorization_no"):
            self.assertIn(key, fields)

    def test_update_with_expired_certificate_rejected(self):
        prop = self._register_property()
        party = self.svc.register_party(prop["id"], party_material(), self.grid_east)
        with self.assertRaises(ValidationFailed) as ctx:
            self.svc.update_party(party["id"], {"cert_expiry": YESTERDAY},
                                  expected_version=1, operator=self.grid_east)
        self.assertIn("cert_expiry", ctx.exception.fields)

    def test_rectify_deadline_must_be_future(self):
        prop = self._register_property()
        insp = self.svc.create_inspection(
            {"property_id": prop["id"], "items": hazard_items()}, self.grid_east)
        with self.assertRaises(ValidationFailed) as ctx:
            self.svc.rectify_inspection(insp["id"], YESTERDAY, expected_version=1,
                                        operator=self.grid_east)
        self.assertIn("deadline", ctx.exception.fields)

    def test_close_requires_require_review_note(self):
        prop = self._register_property()
        insp = self.svc.create_inspection(
            {"property_id": prop["id"], "items": hazard_items()}, self.grid_east)
        with self.assertRaises(ValidationFailed) as ctx:
            self.svc.close_inspection(insp["id"], "", expected_version=1, operator=self.grid_east)
        self.assertIn("review_note", ctx.exception.fields)


class TestArchivedImmutability(ServiceTestCase):
    def _closed_inspection(self):
        prop = self._register_property()
        insp = self.svc.create_inspection(
            {"property_id": prop["id"], "items": hazard_items()}, self.grid_east)
        insp = self.svc.rectify_inspection(insp["id"], NEXT_WEEK, expected_version=1,
                                           operator=self.grid_east)
        return self.svc.close_inspection(insp["id"], "现场复核合格，同意销号",
                                         expected_version=2, operator=self.grid_east)

    def test_closed_inspection_is_archived_with_close_time(self):
        closed = self._closed_inspection()
        self.assertEqual(closed["status"], "CLOSED")
        self.assertTrue(closed["archived"])
        self.assertIsNotNone(closed["closed_at"])  # 销号时间即隐患解除证明
        self.assertEqual(closed["review_note"], "现场复核合格，同意销号")

    def test_archived_record_rejects_new_versions(self):
        closed = self._closed_inspection()
        with self.assertRaises(ArchivedImmutable):
            self.svc.rectify_inspection(closed["id"], NEXT_WEEK, expected_version=3,
                                        operator=self.grid_east)
        with self.assertRaises(ArchivedImmutable):
            self.svc.close_inspection(closed["id"], "重复销号", expected_version=3,
                                      operator=self.grid_east)

    def test_database_trigger_blocks_direct_overwrite(self):
        """即使绕过服务层直接写库，触发器也拒绝覆盖归档记录。"""
        closed = self._closed_inspection()
        with self.assertRaises(sqlite3.IntegrityError):
            self.svc.db._conn.execute(
                "UPDATE inspections SET risk_level = 'high' WHERE id = ?", (closed["id"],))
        # 记录保持原样
        current = self.svc.get_inspection(closed["id"], self.admin)
        self.assertEqual(current["risk_level"], closed["risk_level"])

    def test_pass_inspection_archived_immediately(self):
        prop = self._register_property()
        insp = self.svc.create_inspection(
            {"property_id": prop["id"],
             "items": [{"item": "消防通道", "result": "pass"}]}, self.grid_east)
        self.assertEqual(insp["status"], "CLOSED")
        self.assertTrue(insp["archived"])


class TestConcurrencyConflict(ServiceTestCase):
    def test_parallel_updates_return_conflict_version(self):
        """两个网格员基于同一版本并行修改：先写者胜，后者收到冲突版本与当前快照。"""
        prop = self._register_property()
        first = self.svc.update_property(prop["id"], {"owner_name": "网格员A改"},
                                         expected_version=1, operator=self.grid_east)
        self.assertEqual(first["version"], 2)
        with self.assertRaises(VersionConflict) as ctx:
            self.svc.update_property(prop["id"], {"owner_name": "网格员B改"},
                                     expected_version=1, operator=self.grid_west)
        err = ctx.exception.to_dict()["error"]
        self.assertEqual(err["current_version"], 2)
        self.assertEqual(err["current"]["owner_name"], "网格员A改")

    def test_missing_expected_version_rejected(self):
        prop = self._register_property()
        with self.assertRaises(ValidationFailed) as ctx:
            self.svc.update_property(prop["id"], {"owner_name": "无版本"}, None, self.grid_east)
        self.assertIn("expected_version", ctx.exception.fields)

    def test_rectify_conflict_between_workers(self):
        prop = self._register_property()
        insp = self.svc.create_inspection(
            {"property_id": prop["id"], "items": hazard_items()}, self.grid_east)
        self.svc.rectify_inspection(insp["id"], NEXT_WEEK, expected_version=1,
                                    operator=self.grid_east)
        with self.assertRaises(VersionConflict):
            self.svc.rectify_inspection(insp["id"], NEXT_WEEK, expected_version=1,
                                        operator=self.grid_west)


class TestIdempotency(ServiceTestCase):
    def test_same_idempotency_key_replays_first_response(self):
        data = {"zone": ZONE_EAST, "address": "重复提交路1号", "owner_name": "李房主"}
        first = self.svc.register_property(data, self.grid_east, idempotency_key="req-001")
        second = self.svc.register_property(data, self.grid_east, idempotency_key="req-001")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.svc.list_properties(self.admin)), 1)

    def test_same_key_with_different_payload_rejected(self):
        self.svc.register_property(
            {"zone": ZONE_EAST, "address": "甲路1号", "owner_name": "李房主"},
            self.grid_east, idempotency_key="req-002")
        with self.assertRaises(IdempotencyKeyReused):
            self.svc.register_property(
                {"zone": ZONE_EAST, "address": "乙路2号", "owner_name": "李房主"},
                self.grid_east, idempotency_key="req-002")

    def test_same_party_material_deduplicated(self):
        """重复提交相同责任人材料：返回既有记录，不产生重复数据。"""
        prop = self._register_property()
        first = self.svc.register_party(prop["id"], party_material(), self.grid_east)
        again = self.svc.register_party(prop["id"], party_material(), self.grid_east)
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["deduplicated"])
        detail = self.svc.get_property(prop["id"], self.admin)
        self.assertEqual(len(detail["parties"]), 1)

    def test_same_certificate_different_material_conflicts(self):
        prop = self._register_property()
        self.svc.register_party(prop["id"], party_material(), self.grid_east)
        with self.assertRaises(PartyMaterialMismatch):
            self.svc.register_party(prop["id"], party_material(phone="13900001111"),
                                    self.grid_east)

    def test_idempotent_inspection_creation(self):
        prop = self._register_property()
        payload = {"property_id": prop["id"], "items": hazard_items()}
        first = self.svc.create_inspection(payload, self.grid_east, idempotency_key="insp-1")
        second = self.svc.create_inspection(payload, self.grid_east, idempotency_key="insp-1")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.svc.list_inspections(self.admin, property_id=prop["id"])), 1)


class TestZoneMasking(ServiceTestCase):
    def setUp(self):
        super().setUp()
        prop = self._register_property()
        self.pid = prop["id"]
        self.svc.register_party(self.pid, party_material(), self.grid_east)

    def test_same_zone_worker_sees_full_contact(self):
        detail = self.svc.get_property(self.pid, self.grid_east)
        self.assertEqual(detail["parties"][0]["phone"], PHONE)
        self.assertEqual(detail["parties"][0]["id_card"], ID_CARD)

    def test_cross_zone_worker_sees_masked_contact(self):
        detail = self.svc.get_property(self.pid, self.grid_west)
        party = detail["parties"][0]
        self.assertEqual(party["phone"], "138****5678")
        self.assertEqual(party["id_card"], "1101************1X")
        self.assertNotIn(PHONE, str(detail))

    def test_admin_sees_full_contact(self):
        detail = self.svc.get_property(self.pid, self.admin)
        self.assertEqual(detail["parties"][0]["phone"], PHONE)

    def test_history_snapshots_masked_for_cross_zone(self):
        history = self.svc.property_history(self.pid, self.grid_west)
        party_revisions = [r for r in history["revisions"] if r["entity_type"] == "party"]
        self.assertTrue(party_revisions)
        self.assertEqual(party_revisions[0]["snapshot"]["phone"], "138****5678")


class TestRectificationFlowAndAdminView(ServiceTestCase):
    def test_full_lifecycle_and_admin_overview(self):
        prop = self._register_property()
        self.svc.register_party(prop["id"], party_material(), self.grid_east)

        # 1. 核查发现隐患 → OPEN
        insp = self.svc.create_inspection(
            {"property_id": prop["id"], "items": hazard_items(), "risk_level": "high"},
            self.grid_east)
        self.assertEqual(insp["status"], "OPEN")

        overview = self.svc.risk_overview(self.admin)
        self.assertEqual(overview["summary"]["open"], 1)
        self.assertEqual(overview["risks"][0]["risk_level"], "high")
        self.assertIsNone(overview["risks"][0]["deadline"])

        # 2. 限期整改 → RECTIFYING，管理端可见期限与责任人
        insp = self.svc.rectify_inspection(insp["id"], NEXT_WEEK, expected_version=1,
                                           operator=self.grid_east)
        self.assertEqual(insp["status"], "RECTIFYING")
        self.assertEqual(insp["deadline"], NEXT_WEEK)

        overview = self.svc.risk_overview(self.admin)
        risk = overview["risks"][0]
        self.assertEqual(risk["status"], "RECTIFYING")
        self.assertEqual(risk["deadline"], NEXT_WEEK)
        self.assertEqual(risk["days_remaining"], 7)
        self.assertFalse(risk["overdue"])
        self.assertEqual(risk["responsible"]["name"], "张三")
        self.assertEqual(risk["responsible"]["phone"], PHONE)  # 管理端不脱敏
        self.assertEqual(risk["responsible"]["role_label"], "房东")

        # 3. 复核销号 → CLOSED + 归档，总览清空
        closed = self.svc.close_inspection(insp["id"], "复查合格", expected_version=2,
                                           operator=self.grid_east)
        self.assertEqual(closed["status"], "CLOSED")
        self.assertTrue(closed["archived"])
        overview = self.svc.risk_overview(self.admin)
        self.assertEqual(overview["risks"], [])

        # 4. 历史变更完整可查：核查 → 整改 → 销号
        history = self.svc.property_history(prop["id"], self.admin)
        insp_changes = [r["change_type"] for r in history["revisions"]
                        if r["entity_type"] == "inspection"]
        self.assertEqual(insp_changes, ["inspect", "rectify", "close"])

    def test_overview_requires_admin(self):
        with self.assertRaises(PermissionDenied):
            self.svc.risk_overview(self.grid_east)

    def test_overdue_rectification_flagged(self):
        prop = self._register_property()
        insp = self.svc.create_inspection(
            {"property_id": prop["id"], "items": hazard_items()}, self.grid_east)
        self.svc.rectify_inspection(insp["id"], NEXT_WEEK, expected_version=1,
                                    operator=self.grid_east)
        # 直接改库模拟期限已过（未归档，允许服务层外的维护操作）
        self.svc.db._conn.execute(
            "UPDATE inspections SET deadline = ? WHERE id = ?", (YESTERDAY, insp["id"]))
        overview = self.svc.risk_overview(self.admin)
        self.assertTrue(overview["risks"][0]["overdue"])
        self.assertEqual(overview["summary"]["overdue"], 1)


class TestRestartPersistence(ServiceTestCase):
    def test_archived_and_open_records_survive_restart(self):
        """服务重启后：归档记录与未完成整改继续可查。"""
        prop = self._register_property()
        # 一条已归档（核查通过）
        archived = self.svc.create_inspection(
            {"property_id": prop["id"],
             "items": [{"item": "燃气软管", "result": "pass"}]}, self.grid_east)
        # 一条未完成整改
        prop2 = self._register_property(address="幸福小区9栋101室")
        open_insp = self.svc.create_inspection(
            {"property_id": prop2["id"], "items": hazard_items()}, self.grid_east)
        self.svc.rectify_inspection(open_insp["id"], NEXT_WEEK, expected_version=1,
                                    operator=self.grid_east)

        # 模拟服务重启：关闭后基于同一数据文件重新打开
        self.svc.close()
        self.svc = HousingSafetyService(self.db_path)

        closed = self.svc.list_inspections(self.admin, status="CLOSED")
        self.assertEqual([i["id"] for i in closed], [archived["id"]])
        self.assertTrue(closed[0]["archived"])

        overview = self.svc.risk_overview(self.admin)
        self.assertEqual(len(overview["risks"]), 1)
        self.assertEqual(overview["risks"][0]["inspection_id"], open_insp["id"])
        self.assertEqual(overview["risks"][0]["status"], "RECTIFYING")

        history = self.svc.property_history(prop2["id"], self.admin)
        self.assertEqual([r["change_type"] for r in history["revisions"]
                          if r["entity_type"] == "inspection"], ["inspect", "rectify"])


class TestNotFound(ServiceTestCase):
    def test_missing_property(self):
        with self.assertRaises(NotFound):
            self.svc.get_property("prop_missing", self.admin)

    def test_missing_inspection(self):
        with self.assertRaises(NotFound):
            self.svc.get_inspection("insp_missing", self.admin)


if __name__ == "__main__":
    unittest.main()
