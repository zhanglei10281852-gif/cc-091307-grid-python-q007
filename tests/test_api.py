"""HTTP API 端到端测试：真实起服务、走网络请求。"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

from src.server import make_server
from src.service import HousingSafetyService

NEXT_WEEK = (date.today() + timedelta(days=7)).isoformat()
NEXT_YEAR = (date.today() + timedelta(days=365)).isoformat()

GRID_EAST = {"X-Operator-Id": "grid-east-01", "X-Operator-Role": "grid",
             "X-Operator-Zones": urllib.parse.quote("城东片区")}
GRID_WEST = {"X-Operator-Id": "grid-west-01", "X-Operator-Role": "grid",
             "X-Operator-Zones": urllib.parse.quote("城西片区")}
ADMIN = {"X-Operator-Id": "admin-01", "X-Operator-Role": "admin"}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="housing-api-")
        cls.svc = HousingSafetyService(os.path.join(cls.tmpdir, "api.db"))
        cls.server = make_server(cls.svc, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.svc.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def api(self, method, path, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self):
        status, body = self.api("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_full_workflow_over_http(self):
        # 登记（带幂等键，重放返回同一记录）
        payload = {"zone": "城东片区", "address": "幸福小区5栋302室", "owner_name": "李房主"}
        headers = {**GRID_EAST, "Idempotency-Key": "api-prop-1"}
        status, prop = self.api("POST", "/api/properties", payload, headers)
        self.assertEqual(status, 200, prop)
        status, replay = self.api("POST", "/api/properties", payload, headers)
        self.assertEqual(replay["id"], prop["id"])

        # 中介缺少授权 → 422 字段级原因
        status, err = self.api("POST", f"/api/properties/{prop['id']}/parties", {
            "role": "agent", "name": "某中介", "phone": "13911112222",
            "id_card": "11010119900307772X", "cert_expiry": NEXT_YEAR,
        }, GRID_EAST)
        self.assertEqual(status, 422)
        self.assertIn("authorization_no", err["error"]["fields"])

        # 证件过期 → 422
        status, err = self.api("POST", f"/api/properties/{prop['id']}/parties", {
            "role": "landlord", "name": "张三", "phone": "13812345678",
            "id_card": "11010119900307771X", "cert_expiry": "2020-01-01",
        }, GRID_EAST)
        self.assertEqual(status, 422)
        self.assertIn("cert_expiry", err["error"]["fields"])

        # 正常登记房东
        status, party = self.api("POST", f"/api/properties/{prop['id']}/parties", {
            "role": "landlord", "name": "张三", "phone": "13812345678",
            "id_card": "11010119900307771X", "cert_expiry": NEXT_YEAR,
        }, GRID_EAST)
        self.assertEqual(status, 200, party)

        # 地址冲突 → 422
        status, err = self.api("POST", "/api/properties",
                               {"zone": "城西片区", "address": "幸福小区 5栋 302室",
                                "owner_name": "别人"}, GRID_WEST)
        self.assertEqual(status, 422)
        self.assertIn("地址冲突", err["error"]["fields"]["address"])

        # 居住单元 + 用途变更
        status, unit = self.api("POST", f"/api/properties/{prop['id']}/units",
                                {"label": "北屋", "usage": "客厅"}, GRID_EAST)
        self.assertEqual(status, 200, unit)
        status, unit = self.api("PUT", f"/api/units/{unit['id']}",
                                {"usage": "隔断间", "expected_version": 1}, GRID_EAST)
        self.assertEqual(unit["version"], 2)

        # 核查发现隐患
        status, insp = self.api("POST", "/api/inspections", {
            "property_id": prop["id"], "unit_id": unit["id"],
            "items": [{"item": "隔断住人", "result": "fail"}], "risk_level": "high",
        }, GRID_EAST)
        self.assertEqual(insp["status"], "OPEN")

        # 并行修改冲突 → 409 返回当前版本
        status, err = self.api("PUT", f"/api/properties/{prop['id']}",
                               {"owner_name": "改名", "expected_version": 99}, GRID_WEST)
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "VERSION_CONFLICT")
        self.assertEqual(err["error"]["current_version"], 1)

        # 限期整改
        status, insp = self.api("POST", f"/api/inspections/{insp['id']}/rectify",
                                {"deadline": NEXT_WEEK, "expected_version": 1}, GRID_EAST)
        self.assertEqual(insp["status"], "RECTIFYING")

        # 跨片区查询联系方式脱敏
        status, detail = self.api("GET", f"/api/properties/{prop['id']}", headers=GRID_WEST)
        self.assertEqual(detail["parties"][0]["phone"], "138****5678")
        status, detail = self.api("GET", f"/api/properties/{prop['id']}", headers=GRID_EAST)
        self.assertEqual(detail["parties"][0]["phone"], "13812345678")

        # 管理端总览
        status, overview = self.api("GET", "/api/admin/risk-overview", headers=ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(len(overview["risks"]), 1)
        self.assertEqual(overview["risks"][0]["responsible"]["name"], "张三")
        status, err = self.api("GET", "/api/admin/risk-overview", headers=GRID_EAST)
        self.assertEqual(status, 403)

        # 复核销号 → 归档
        status, closed = self.api("POST", f"/api/inspections/{insp['id']}/close",
                                  {"review_note": "隔断已拆除，复核通过", "expected_version": 2},
                                  GRID_EAST)
        self.assertEqual(closed["status"], "CLOSED")
        self.assertTrue(closed["archived"])
        self.assertIsNotNone(closed["closed_at"])

        # 归档记录不可被新版本覆盖
        status, err = self.api("POST", f"/api/inspections/{insp['id']}/close",
                               {"review_note": "再次销号", "expected_version": 3}, GRID_EAST)
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "ARCHIVED_IMMUTABLE")

        # 历史变更完整
        status, history = self.api("GET", f"/api/properties/{prop['id']}/history", headers=ADMIN)
        change_types = [r["change_type"] for r in history["revisions"]]
        self.assertIn("register", change_types)
        self.assertIn("inspect", change_types)
        self.assertIn("rectify", change_types)
        self.assertIn("close", change_types)

        # 总览已清空
        status, overview = self.api("GET", "/api/admin/risk-overview", headers=ADMIN)
        self.assertEqual(overview["risks"], [])

    def test_unknown_route(self):
        status, body = self.api("GET", "/api/nope")
        self.assertEqual(status, 404)

    def test_bad_json(self):
        url = f"http://127.0.0.1:{self.port}/api/properties"
        req = urllib.request.Request(url, data=b"{not json", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req)
            self.fail("应返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
