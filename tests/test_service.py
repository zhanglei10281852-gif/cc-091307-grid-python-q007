"""房屋安全登记服务的端到端测试。

覆盖：登记/变更/核查/限期整改/复核销号全流程，证件过期、授权缺失、
地址冲突的字段级拒绝，归档不可覆盖，并发版本冲突，幂等重放，
按片区脱敏，管理端视图，以及重启后的持久化。
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from src.app import create_app

GRID1 = {"X-Operator-Id": "op-grid-1"}   # 负责片区 Z-01
GRID2 = {"X-Operator-Id": "op-grid-2"}   # 负责片区 Z-02
ADMIN = {"X-Operator-Id": "op-admin"}

FUTURE = (dt.date.today() + dt.timedelta(days=3650)).isoformat()
PAST = (dt.date.today() - dt.timedelta(days=1)).isoformat()
DEADLINE = (dt.date.today() + dt.timedelta(days=15)).isoformat()
DEADLINE_LATER = (dt.date.today() + dt.timedelta(days=30)).isoformat()


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "service.db"))
    with TestClient(app) as c:
        yield c


def add_person(client, headers=GRID1, **over):
    payload = {
        "name": "张三",
        "phone": "13800001111",
        "id_number": "110101199001011234",
        "id_expires_on": FUTURE,
    }
    payload.update(over)
    r = client.post("/persons", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["person_id"]


def add_house(client, headers=GRID1, idem_key=None, **over):
    owner = over.pop("owner", None) or add_person(client, headers)
    payload = {
        "address": "幸福路1号101室",
        "zone": "Z-01",
        "responsibles": [{"person_id": owner, "role": "owner"}],
        "units": [{"room_label": "主卧", "purpose": "卧室", "capacity": 2}],
    }
    payload.update(over)
    h = dict(headers)
    if idem_key:
        h["Idempotency-Key"] = idem_key
    return client.post("/houses", json=payload, headers=h)


def add_check_item(client, **over):
    payload = {"code": "FIRE-EXT", "title": "灭火器配备", "category": "消防", "severity": "high"}
    payload.update(over)
    r = client.post("/check-items", json=payload, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["item_id"]


def make_inspection(client, house_id, item_id, result="fail", **over):
    payload = {"house_id": house_id,
               "results": [{"item_id": item_id, "result": result, "note": "现场核查"}]}
    payload.update(over)
    r = client.post("/inspections", json=payload, headers=GRID1)
    assert r.status_code == 200, r.text
    return r.json()


def make_rectification(client, inspection_id, item_id, **over):
    payload = {"inspection_id": inspection_id, "item_id": item_id,
               "hazard": "灭火器过期未检", "deadline": DEADLINE}
    payload.update(over)
    return client.post("/rectifications", json=payload, headers=GRID1)


# ---------------------------------------------------------------- 登记与版本化

def test_register_house_and_version_history(client):
    r = add_house(client)
    assert r.status_code == 200, r.text
    house = r.json()
    assert house["version"] == 1
    assert house["units"][0]["purpose"] == "卧室"

    r = client.put(f"/houses/{house['house_id']}", headers=GRID1,
                   json={"expected_version": 1, "address": "幸福路1号102室",
                         "change_note": "门牌更正"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2

    history = client.get(f"/houses/{house['house_id']}/history", headers=GRID1).json()
    assert [h["version"] for h in history] == [1, 2]
    assert history[0]["address"] == "幸福路1号101室"
    assert history[1]["change_note"] == "门牌更正"


def test_unit_purpose_change_keeps_history(client):
    house = add_house(client).json()
    unit_id = house["units"][0]["unit_id"]
    r = client.put(f"/units/{unit_id}", headers=GRID1,
                   json={"expected_version": 1, "purpose": "隔断间", "capacity": 4})
    assert r.status_code == 200, r.text

    history = client.get(f"/units/{unit_id}/history", headers=GRID1).json()
    assert [h["purpose"] for h in history] == ["卧室", "隔断间"]
    assert history[1]["capacity"] == 4


# ---------------------------------------------------------------- 写入校验（字段级拒绝）

def test_expired_certificate_rejected(client):
    r = client.post("/persons", headers=GRID1,
                    json={"name": "李四", "phone": "13900002222",
                          "id_number": "110101199001011235", "id_expires_on": PAST})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["fields"][0]["field"] == "id_expires_on"
    assert "过期" in err["fields"][0]["reason"]

    # 变更时把证件改成已过期同样被拒绝
    person_id = add_person(client)
    r = client.put(f"/persons/{person_id}", headers=GRID1,
                   json={"expected_version": 1, "id_expires_on": PAST})
    assert r.status_code == 422
    assert r.json()["error"]["fields"][0]["field"] == "id_expires_on"


def test_missing_or_expired_authorization_rejected(client):
    owner = add_person(client, name="房东")
    agent = add_person(client, name="中介", phone="13700003333")

    # 中介无授权材料
    r = add_house(client, responsibles=[
        {"person_id": owner, "role": "owner"},
        {"person_id": agent, "role": "agent"},
    ])
    assert r.status_code == 422
    fields = {f["field"] for f in r.json()["error"]["fields"]}
    assert "responsibles[1].authorization_doc" in fields

    # 授权已过期
    r = add_house(client, responsibles=[
        {"person_id": owner, "role": "owner"},
        {"person_id": agent, "role": "agent",
         "authorization_doc": "授权书.pdf", "authorization_expires_on": PAST},
    ])
    assert r.status_code == 422
    fields = {f["field"] for f in r.json()["error"]["fields"]}
    assert "responsibles[1].authorization_expires_on" in fields

    # 授权齐全且在有效期内
    r = add_house(client, responsibles=[
        {"person_id": owner, "role": "owner"},
        {"person_id": agent, "role": "agent",
         "authorization_doc": "授权书.pdf", "authorization_expires_on": FUTURE},
    ])
    assert r.status_code == 200, r.text


def test_address_conflict_rejected(client):
    first = add_house(client).json()
    # 同一地址（空白/大小写差异）被重复登记
    r = add_house(client, address=" 幸福路 1号101室 ")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["fields"][0]["field"] == "address"
    assert first["house_id"] in err["fields"][0]["reason"]

    # 不同地址不受影响
    assert add_house(client, address="幸福路2号201室").status_code == 200


def test_house_requires_exactly_one_owner(client):
    p1, p2 = add_person(client), add_person(client, phone="13700004444")
    r = add_house(client, responsibles=[{"person_id": p1, "role": "tenant"},
                                        {"person_id": p2, "role": "tenant"}])
    assert r.status_code == 422
    assert r.json()["error"]["fields"][0]["field"] == "responsibles"


# ---------------------------------------------------------------- 并发与幂等

def test_parallel_changes_return_version_conflict(client):
    house = add_house(client).json()
    url = f"/houses/{house['house_id']}"
    # 两个网格员都基于版本 1 提交变更：第一个成功，第二个收到冲突版本
    r1 = client.put(url, headers=GRID1, json={"expected_version": 1, "zone": "Z-02"})
    assert r1.status_code == 200, r1.text
    r2 = client.put(url, headers=GRID2, json={"expected_version": 1, "zone": "Z-03"})
    assert r2.status_code == 409
    err = r2.json()["error"]
    assert err["code"] == "version_conflict"
    assert err["current_version"] == 2
    assert err["expected_version"] == 1


def test_idempotent_replay_of_same_material(client):
    key = "register-req-001"
    owner = add_person(client)
    r1 = add_house(client, idem_key=key, owner=owner)
    assert r1.status_code == 200, r1.text
    # 相同材料 + 相同 Key 重放：返回首次结果，不产生重复登记
    r2 = add_house(client, idem_key=key, owner=owner)
    assert r2.status_code == 200
    assert r2.json()["house_id"] == r1.json()["house_id"]
    houses = client.get("/houses", headers=GRID1).json()
    assert len(houses) == 1

    # 同一 Key 提交不同材料 → 拒绝
    r3 = add_house(client, idem_key=key, owner=owner, address="幸福路9号909室")
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "idempotency_conflict"


# ---------------------------------------------------------------- 核查、归档与检查项版本

def test_archived_inspection_not_overwritten_by_new_versions(client):
    item_id = add_check_item(client)
    house = add_house(client).json()
    insp = make_inspection(client, house["house_id"], item_id)
    assert insp["results"][0]["item_version"] == 1

    r = client.post(f"/inspections/{insp['inspection_id']}/archive", headers=GRID1)
    assert r.status_code == 200
    assert r.json()["status"] == "archived"

    # 归档后禁止更正（不可被新版本覆盖）
    r = client.put(f"/inspections/{insp['inspection_id']}", headers=GRID1,
                   json={"expected_version": 1,
                         "results": [{"item_id": item_id, "result": "pass"}]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "inspection_archived"

    # 检查项标准升级为新版本，归档记录仍引用旧版本原文
    r = client.put(f"/check-items/{item_id}", headers=ADMIN,
                   json={"expected_version": 1, "title": "灭火器配备（2026版）"})
    assert r.status_code == 200
    assert r.json()["version"] == 2

    archived = client.get(f"/inspections/{insp['inspection_id']}", headers=GRID1).json()
    assert archived["results"][0]["item_version"] == 1
    assert archived["results"][0]["title"] == "灭火器配备"

    history = client.get(f"/check-items/{item_id}/history", headers=GRID1).json()
    assert [h["version"] for h in history] == [1, 2]


def test_open_inspection_can_be_corrected_with_version(client):
    item_id = add_check_item(client)
    house = add_house(client).json()
    insp = make_inspection(client, house["house_id"], item_id, result="pass")
    r = client.put(f"/inspections/{insp['inspection_id']}", headers=GRID1,
                   json={"expected_version": 1,
                         "results": [{"item_id": item_id, "result": "fail", "note": "复查不合格"}]})
    assert r.status_code == 200
    assert r.json()["version"] == 2
    assert r.json()["results"][0]["result"] == "fail"


# ---------------------------------------------------------------- 限期整改与复核销号

def test_rectification_full_lifecycle(client):
    item_id = add_check_item(client)
    house = add_house(client).json()
    insp = make_inspection(client, house["house_id"], item_id)

    r = make_rectification(client, insp["inspection_id"], item_id)
    assert r.status_code == 200, r.text
    rect = r.json()
    assert rect["status"] == "open"
    assert rect["deadline"] == DEADLINE
    assert rect["events"][0]["event"] == "issued"

    url = f"/rectifications/{rect['rect_id']}/review"
    # 复核不通过：保持 open，可顺延期限，事件留痕
    r = client.post(url, headers=GRID1,
                    json={"passed": False, "note": "现场仍未整改", "new_deadline": DEADLINE_LATER})
    assert r.status_code == 200
    rect = r.json()
    assert rect["status"] == "open"
    assert rect["deadline"] == DEADLINE_LATER
    assert [e["event"] for e in rect["events"]] == [
        "issued", "review_rejected", "deadline_extended"]

    # 复核通过：销号，记录解除时间与经办人
    r = client.post(url, headers=GRID1, json={"passed": True, "note": "已更换灭火器"})
    assert r.status_code == 200
    rect = r.json()
    assert rect["status"] == "closed"
    assert rect["closed_at"] is not None
    assert rect["closed_by"] == "op-grid-1"
    assert rect["events"][-1]["event"] == "closed"

    # 已销号不可重复复核
    r = client.post(url, headers=GRID1, json={"passed": True})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "rectification_closed"


def test_rectification_validation(client):
    item_id = add_check_item(client)
    item_ok = add_check_item(client, code="PASSAGE", title="疏散通道畅通")
    house = add_house(client).json()
    insp = make_inspection(client, house["house_id"], item_id)
    insp["inspection_id"]
    insp2 = make_inspection(client, house["house_id"], item_ok, result="pass")

    # 期限必须晚于今日
    r = make_rectification(client, insp["inspection_id"], item_id, deadline=PAST)
    assert r.status_code == 422
    assert r.json()["error"]["fields"][0]["field"] == "deadline"

    # 合格项无需整改
    r = make_rectification(client, insp2["inspection_id"], item_ok)
    assert r.status_code == 422
    assert r.json()["error"]["fields"][0]["field"] == "item_id"

    # 同一隐患不得重复下发整改单
    assert make_rectification(client, insp["inspection_id"], item_id).status_code == 200
    r = make_rectification(client, insp["inspection_id"], item_id)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "duplicate_rectification"


# ---------------------------------------------------------------- 片区权限脱敏

def test_contact_masked_by_zone_permission(client):
    owner = add_person(client, phone="13800001111", id_number="110101199001011234")
    house = add_house(client, owner=owner).json()

    # 本片区网格员：明文
    view = client.get(f"/houses/{house['house_id']}", headers=GRID1).json()
    assert view["responsibles"][0]["phone"] == "13800001111"
    assert view["contact_masked"] is False

    # 其他片区网格员：脱敏
    view = client.get(f"/houses/{house['house_id']}", headers=GRID2).json()
    assert view["responsibles"][0]["phone"] == "138****1111"
    assert view["contact_masked"] is True

    # 管理端：明文
    view = client.get(f"/houses/{house['house_id']}", headers=ADMIN).json()
    assert view["responsibles"][0]["phone"] == "13800001111"

    # 责任人查询同样按片区脱敏
    person = client.get(f"/persons/{owner}", headers=GRID2).json()
    assert person["id_number"].startswith("1101")
    assert "*" in person["id_number"]
    person = client.get(f"/persons/{owner}", headers=GRID1).json()
    assert person["id_number"] == "110101199001011234"


# ---------------------------------------------------------------- 管理端

def test_admin_dashboard(client):
    item_id = add_check_item(client)
    house = add_house(client).json()
    insp = make_inspection(client, house["house_id"], item_id)
    client.post(f"/inspections/{insp['inspection_id']}/archive", headers=GRID1)
    make_rectification(client, insp["inspection_id"], item_id)

    r = client.get("/admin/dashboard", headers=ADMIN)
    assert r.status_code == 200
    dash = r.json()
    assert dash["stats"]["open_rectifications"] == 1
    assert dash["stats"]["archived_inspections"] == 1

    risk = dash["open_rectifications"][0]
    assert risk["hazard"] == "灭火器过期未检"
    assert risk["deadline"] == DEADLINE
    assert risk["overdue"] is False
    assert risk["responsibles"][0]["phone"] == "13800001111"  # 管理端明文

    kinds = {c["kind"] for c in dash["recent_changes"]}
    assert {"house", "person", "check_item"} <= kinds

    # 网格员无权访问管理端
    assert client.get("/admin/dashboard", headers=GRID1).status_code == 403
    # 检查项仅管理端可维护
    assert client.post("/check-items", headers=GRID1,
                       json={"code": "X", "title": "x", "category": "c",
                             "severity": "low"}).status_code == 403


def test_unknown_operator_rejected(client):
    assert client.get("/houses").status_code == 401
    assert client.get("/houses", headers={"X-Operator-Id": "nobody"}).status_code == 401


# ---------------------------------------------------------------- 重启持久化

def test_restart_keeps_archived_and_open_rectifications(tmp_path):
    db = str(tmp_path / "service.db")
    with TestClient(create_app(db)) as c1:
        item_id = add_check_item(c1)
        house = add_house(c1).json()
        insp = make_inspection(c1, house["house_id"], item_id)
        c1.post(f"/inspections/{insp['inspection_id']}/archive", headers=GRID1)
        rect = make_rectification(c1, insp["inspection_id"], item_id).json()

    # 模拟服务重启：同一数据文件创建新应用实例
    with TestClient(create_app(db)) as c2:
        archived = c2.get(f"/inspections/{insp['inspection_id']}", headers=GRID1).json()
        assert archived["status"] == "archived"
        assert archived["archived_at"] is not None

        open_rects = c2.get("/rectifications?status=open", headers=GRID1).json()
        assert [r["rect_id"] for r in open_rects] == [rect["rect_id"]]
        assert open_rects[0]["deadline"] == DEADLINE

        history = c2.get(f"/houses/{house['house_id']}/history", headers=GRID1).json()
        assert len(history) == 1
