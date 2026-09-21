"""端到端演示：群租房排查全流程。

运行：python3 examples/demo.py
覆盖：登记 → 责任人 → 用途变更 → 核查 → 限期整改 → 管理端总览 → 复核销号
      → 归档不可改 / 地址冲突 / 证件过期 / 幂等重放 / 并发冲突 / 片区脱敏 / 重启持久化
"""

import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.errors import ServiceError
from src.service import HousingSafetyService, Operator

NEXT_WEEK = (date.today() + timedelta(days=7)).isoformat()
NEXT_YEAR = (date.today() + timedelta(days=365)).isoformat()

admin = Operator("admin-01", "admin")
grid_east = Operator("grid-east-01", "grid", ["城东片区"])
grid_west = Operator("grid-west-01", "grid", ["城西片区"])


def show(title, payload=None):
    print(f"\n=== {title} ===")
    if payload is not None:
        import json
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def expect_reject(title, fn):
    try:
        fn()
    except ServiceError as exc:
        show(f"{title} → 已拒绝 [{exc.code}]", exc.to_dict()["error"])
    else:
        raise AssertionError(f"{title} 应被拒绝")


def main():
    tmpdir = tempfile.mkdtemp(prefix="housing-demo-")
    db_path = os.path.join(tmpdir, "demo.db")
    svc = HousingSafetyService(db_path)

    # 1. 登记房屋（带幂等键；重复提交返回同一记录）
    data = {"zone": "城东片区", "address": "幸福小区 3栋 2单元 501室", "owner_name": "李房主"}
    prop = svc.register_property(data, grid_east, idempotency_key="demo-reg-1")
    again = svc.register_property(data, grid_east, idempotency_key="demo-reg-1")
    show("1. 登记房屋（幂等键重放 → 同一记录）", {"id": prop["id"], "重放id相同": again["id"] == prop["id"]})

    # 2. 重复登记同一地址 → 地址冲突拒绝
    expect_reject("2. 中介重复登记同一房屋",
                  lambda: svc.register_property(
                      {"zone": "城东片区", "address": "幸福小区3栋2单元501室", "owner_name": "中介小王"},
                      grid_west))

    # 3. 责任人：证件过期 / 中介缺授权 → 字段级拒绝
    expect_reject("3a. 房东证件过期",
                  lambda: svc.register_party(prop["id"], {
                      "role": "landlord", "name": "张三", "phone": "13812345678",
                      "id_card": "11010119900307771X", "cert_expiry": "2020-01-01"}, grid_east))
    expect_reject("3b. 中介缺少授权书",
                  lambda: svc.register_party(prop["id"], {
                      "role": "agent", "name": "某中介", "phone": "13911112222",
                      "id_card": "11010119900307772X", "cert_expiry": NEXT_YEAR}, grid_east))
    party = svc.register_party(prop["id"], {
        "role": "landlord", "name": "张三", "phone": "13812345678",
        "id_card": "11010119900307771X", "cert_expiry": NEXT_YEAR}, grid_east)
    show("3c. 房东登记成功", {"id": party["id"], "name": party["name"]})

    # 4. 居住单元用途变更留痕
    unit = svc.register_unit(prop["id"], {"label": "北屋", "usage": "客厅"}, grid_east)
    svc.update_unit(unit["id"], {"usage": "隔断间"}, expected_version=1, operator=grid_east)
    show("4. 客厅改隔断间（用途变更已留痕）", {"unit": unit["id"]})

    # 5. 核查发现隐患 → 限期整改
    insp = svc.create_inspection({"property_id": prop["id"], "unit_id": unit["id"],
                                  "items": [{"item": "隔断住人", "result": "fail"},
                                            {"item": "私拉电线", "result": "fail"}],
                                  "risk_level": "high"}, grid_east)
    insp = svc.rectify_inspection(insp["id"], NEXT_WEEK, expected_version=1, operator=grid_east)
    show("5. 核查 → 限期整改", {"status": insp["status"], "deadline": insp["deadline"]})

    # 6. 并行修改冲突
    svc.update_property(prop["id"], {"owner_name": "李房主(已核实)"}, expected_version=1, operator=grid_east)
    expect_reject("6. 另一网格员基于旧版本修改",
                  lambda: svc.update_property(prop["id"], {"owner_name": "别人"},
                                              expected_version=1, operator=grid_west))

    # 7. 片区权限脱敏
    show("7a. 本片区网格员看到的联系方式",
         svc.get_property(prop["id"], grid_east)["parties"][0]["phone"])
    show("7b. 跨片区网格员看到的联系方式",
         svc.get_property(prop["id"], grid_west)["parties"][0]["phone"])

    # 8. 管理端风险总览
    overview = svc.risk_overview(admin)
    show("8. 管理端总览（当前风险/期限/责任人）", overview["risks"])

    # 9. 复核销号 → 归档；归档后不可再改
    closed = svc.close_inspection(insp["id"], "隔断已拆除，现场复核合格",
                                  expected_version=2, operator=grid_east)
    show("9. 复核销号（closed_at 即隐患解除证明）",
         {"status": closed["status"], "archived": closed["archived"], "closed_at": closed["closed_at"]})
    expect_reject("9b. 归档记录再次被整改",
                  lambda: svc.rectify_inspection(insp["id"], NEXT_WEEK, expected_version=3,
                                                 operator=grid_east))

    # 10. 历史变更
    history = svc.property_history(prop["id"], admin)
    show("10. 历史变更时间线",
         [(r["entity_type"], r["change_type"], r["version"], r["changed_by"])
          for r in history["revisions"]])

    # 11. 模拟重启：归档记录与数据继续可查
    svc.close()
    svc = HousingSafetyService(db_path)
    closed_list = svc.list_inspections(admin, status="CLOSED")
    show("11. 服务重启后：归档记录仍可查",
         {"归档记录数": len(closed_list), "closed_at": closed_list[0]["closed_at"]})
    svc.close()
    print(f"\n演示完成（数据文件 {db_path} 已随临时目录保留至进程结束）")


if __name__ == "__main__":
    main()
