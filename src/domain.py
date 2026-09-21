"""领域逻辑：版本化写入、字段级校验、幂等、并发冲突与按片区脱敏。

约定：
- 所有写操作在 Store.write() 事务内完成，校验失败整体回滚；
- 变更类操作必须携带 expected_version，不一致返回 409 并带上当前版本；
- 支持 Idempotency-Key 的写接口，重复提交同一材料返回首次结果；
- 查询按操作员片区权限对联系方式脱敏。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from .store import Store


class DomainError(Exception):
    """业务错误：携带 HTTP 状态码、错误码与字段级原因。"""

    def __init__(self, code, message, status=422, fields=None, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.fields = fields or []
        self.extra = extra or {}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _today() -> dt.date:
    return dt.date.today()


def _pick(new, old):
    return old if new is None else new


def normalize_address(address: str) -> str:
    """地址归一化：去除全部空白并统一小写，用于冲突检测。"""
    return re.sub(r"\s+", "", address).lower()


def mask_phone(phone: str) -> str:
    if len(phone) >= 7:
        return phone[:3] + "****" + phone[-4:]
    return "***"


def mask_id_number(id_number: str) -> str:
    if len(id_number) > 6:
        return id_number[:4] + "*" * (len(id_number) - 6) + id_number[-2:]
    return "***"


def _hash_payload(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Domain:
    def __init__(self, store: Store):
        self.store = store

    # ---------------------------------------------------------------- 基础

    def get_operator(self, operator_id: str):
        row = self.store.one("SELECT * FROM operators WHERE operator_id = ?", (operator_id,))
        if row is None:
            return None
        return {
            "operator_id": row["operator_id"],
            "name": row["name"],
            "role": row["role"],
            "zones": json.loads(row["zones"]),
        }

    @staticmethod
    def _can_view_contact(operator: dict, zone: str) -> bool:
        return operator["role"] == "admin" or zone in operator["zones"]

    @staticmethod
    def _check_version(current: int, expected: int, what: str) -> None:
        if current != expected:
            raise DomainError(
                "version_conflict",
                f"{what}版本冲突：当前版本为 {current}，提交基于版本 {expected}，请刷新后重试",
                status=409,
                extra={"current_version": current, "expected_version": expected},
            )

    def _run_idempotent(self, endpoint, operator_id, idem_key, payload, fn):
        """在单个写事务内执行 fn；带 Idempotency-Key 时去重并缓存响应。"""
        req_hash = _hash_payload(payload)
        with self.store.write() as conn:
            if idem_key:
                row = conn.execute(
                    "SELECT request_hash, response_body FROM idempotency_keys "
                    "WHERE key = ? AND operator_id = ? AND endpoint = ?",
                    (idem_key, operator_id, endpoint),
                ).fetchone()
                if row is not None:
                    if row["request_hash"] != req_hash:
                        raise DomainError(
                            "idempotency_conflict",
                            "同一 Idempotency-Key 提交了不同的材料，已拒绝",
                            status=409,
                        )
                    return json.loads(row["response_body"])
            body = fn(conn)
            if idem_key:
                conn.execute(
                    "INSERT INTO idempotency_keys(key, operator_id, endpoint, request_hash, response_body, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (idem_key, operator_id, endpoint, req_hash,
                     json.dumps(body, ensure_ascii=False), _now()),
                )
            return body

    # ---------------------------------------------------------------- 校验

    @staticmethod
    def _validate_certificate(id_expires_on: str, field: str) -> None:
        if id_expires_on < _today().isoformat():
            raise DomainError("validation_failed", "证件校验未通过", fields=[
                {"field": field,
                 "reason": f"证件已过期(id_expires_on={id_expires_on})，请先更新证件后再写入"}])

    def _validate_responsibles(self, responsibles: list[dict]) -> None:
        """责任人校验：证件有效期、中介授权、房东唯一。"""
        fields = []
        owners = [r for r in responsibles if r["role"] == "owner"]
        if len(owners) != 1:
            fields.append({"field": "responsibles",
                           "reason": "必须且只能登记一名房东(owner)"})
        today = _today().isoformat()
        for i, r in enumerate(responsibles):
            person = self._current_person(r["person_id"])
            if person is None:
                fields.append({"field": f"responsibles[{i}].person_id",
                               "reason": f"责任人 {r['person_id']} 不存在，请先登记"})
                continue
            if person["id_expires_on"] < today:
                fields.append({"field": f"responsibles[{i}].id_expires_on",
                               "reason": f"责任人 {person['name']} 的证件已过期"
                                         f"(id_expires_on={person['id_expires_on']})，请先更新证件"})
            if r["role"] == "agent":
                if not r.get("authorization_doc"):
                    fields.append({"field": f"responsibles[{i}].authorization_doc",
                                   "reason": "中介登记缺少房东授权材料"})
                elif not r.get("authorization_expires_on"):
                    fields.append({"field": f"responsibles[{i}].authorization_expires_on",
                                   "reason": "缺少房东授权有效期"})
                elif r["authorization_expires_on"] < today:
                    fields.append({"field": f"responsibles[{i}].authorization_expires_on",
                                   "reason": f"房东授权已过期(authorization_expires_on="
                                             f"{r['authorization_expires_on']})"})
        if fields:
            raise DomainError("validation_failed", "责任人校验未通过", fields=fields)

    def _check_address_free(self, address: str, exclude_house_id: str | None = None) -> str:
        address_key = normalize_address(address)
        if exclude_house_id is None:
            row = self.store.one(
                "SELECT house_id FROM houses WHERE address_key = ? AND status = 'active'",
                (address_key,))
        else:
            row = self.store.one(
                "SELECT house_id FROM houses WHERE address_key = ? AND status = 'active' AND house_id <> ?",
                (address_key, exclude_house_id))
        if row is not None:
            raise DomainError("validation_failed", "登记校验未通过", fields=[
                {"field": "address",
                 "reason": f"地址与已登记房屋 {row['house_id']} 冲突，同一房屋不得重复登记"}])
        return address_key

    # ---------------------------------------------------------------- 责任人

    def register_person(self, operator, payload, idem_key):
        return self._run_idempotent(
            "POST /persons", operator["operator_id"], idem_key, payload,
            lambda conn: self._register_person(conn, operator, payload))

    def _register_person(self, conn, operator, p):
        self._validate_certificate(p["id_expires_on"], "id_expires_on")
        person_id = self.store.next_id(conn, "person", "P")
        now = _now()
        conn.execute(
            "INSERT INTO persons(person_id, current_version, created_at) VALUES (?,?,?)",
            (person_id, 1, now))
        conn.execute(
            "INSERT INTO person_versions(person_id, version, name, phone, id_number, id_expires_on,"
            " changed_by, changed_at, change_note) VALUES (?,?,?,?,?,?,?,?,?)",
            (person_id, 1, p["name"], p["phone"], p["id_number"], p["id_expires_on"],
             operator["operator_id"], now, "登记"))
        return self.person_view(person_id, operator)

    def change_person(self, operator, person_id, payload, idem_key):
        return self._run_idempotent(
            f"PUT /persons/{person_id}", operator["operator_id"], idem_key, payload,
            lambda conn: self._change_person(conn, operator, person_id, payload))

    def _change_person(self, conn, operator, person_id, p):
        row = self.store.one("SELECT * FROM persons WHERE person_id = ?", (person_id,))
        if row is None:
            raise DomainError("not_found", f"责任人 {person_id} 不存在", status=404)
        self._check_version(row["current_version"], p["expected_version"], "责任人")
        cur = self._current_person(person_id)
        merged = {
            "name": _pick(p.get("name"), cur["name"]),
            "phone": _pick(p.get("phone"), cur["phone"]),
            "id_number": _pick(p.get("id_number"), cur["id_number"]),
            "id_expires_on": _pick(p.get("id_expires_on"), cur["id_expires_on"]),
        }
        self._validate_certificate(merged["id_expires_on"], "id_expires_on")
        new_version = row["current_version"] + 1
        conn.execute(
            "INSERT INTO person_versions(person_id, version, name, phone, id_number, id_expires_on,"
            " changed_by, changed_at, change_note) VALUES (?,?,?,?,?,?,?,?,?)",
            (person_id, new_version, merged["name"], merged["phone"], merged["id_number"],
             merged["id_expires_on"], operator["operator_id"], _now(), p.get("change_note")))
        conn.execute("UPDATE persons SET current_version = ? WHERE person_id = ?",
                     (new_version, person_id))
        return self.person_view(person_id, operator)

    def _current_person(self, person_id: str):
        row = self.store.one(
            "SELECT p.person_id, p.current_version, v.name, v.phone, v.id_number, v.id_expires_on "
            "FROM persons p JOIN person_versions v "
            "  ON v.person_id = p.person_id AND v.version = p.current_version "
            "WHERE p.person_id = ?", (person_id,))
        return dict(row) if row else None

    def _person_zones(self, person_id: str) -> set:
        """该责任人当前挂靠房屋所在的片区集合，用于跨片区脱敏判定。"""
        rows = self.store.all(
            "SELECT h.zone, v.responsibles FROM houses h JOIN house_versions v "
            "  ON v.house_id = h.house_id AND v.version = h.current_version "
            "WHERE h.status = 'active'")
        zones = set()
        for row in rows:
            if any(r["person_id"] == person_id for r in json.loads(row["responsibles"])):
                zones.add(row["zone"])
        return zones

    def _person_can_view(self, operator, person_id) -> bool:
        if operator["role"] == "admin":
            return True
        return bool(self._person_zones(person_id) & set(operator["zones"]))

    @staticmethod
    def _contact_view(person: dict, can_view: bool) -> dict:
        return {
            "person_id": person["person_id"],
            "version": person["current_version"],
            "name": person["name"],
            "phone": person["phone"] if can_view else mask_phone(person["phone"]),
            "id_number": person["id_number"] if can_view else mask_id_number(person["id_number"]),
            "id_expires_on": person["id_expires_on"],
            "contact_masked": not can_view,
        }

    def person_view(self, person_id, operator):
        cur = self._current_person(person_id)
        if cur is None:
            raise DomainError("not_found", f"责任人 {person_id} 不存在", status=404)
        return self._contact_view(cur, self._person_can_view(operator, person_id))

    def person_history(self, person_id, operator):
        if self._current_person(person_id) is None:
            raise DomainError("not_found", f"责任人 {person_id} 不存在", status=404)
        can_view = self._person_can_view(operator, person_id)
        rows = self.store.all(
            "SELECT * FROM person_versions WHERE person_id = ? ORDER BY version", (person_id,))
        return [{
            "version": r["version"],
            "name": r["name"],
            "phone": r["phone"] if can_view else mask_phone(r["phone"]),
            "id_number": r["id_number"] if can_view else mask_id_number(r["id_number"]),
            "id_expires_on": r["id_expires_on"],
            "changed_by": r["changed_by"],
            "changed_at": r["changed_at"],
            "change_note": r["change_note"],
        } for r in rows]

    # ---------------------------------------------------------------- 房屋

    def register_house(self, operator, payload, idem_key):
        return self._run_idempotent(
            "POST /houses", operator["operator_id"], idem_key, payload,
            lambda conn: self._register_house(conn, operator, payload))

    def _register_house(self, conn, operator, p):
        self._validate_responsibles(p["responsibles"])
        address_key = self._check_address_free(p["address"])
        house_id = self.store.next_id(conn, "house", "H")
        now = _now()
        conn.execute(
            "INSERT INTO houses(house_id, current_version, address_key, zone, status, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (house_id, 1, address_key, p["zone"], "active", now))
        conn.execute(
            "INSERT INTO house_versions(house_id, version, address, zone, responsibles,"
            " changed_by, changed_at, change_note) VALUES (?,?,?,?,?,?,?,?)",
            (house_id, 1, p["address"], p["zone"],
             json.dumps(p["responsibles"], ensure_ascii=False),
             operator["operator_id"], now, p.get("change_note") or "登记"))
        for u in p.get("units") or []:
            self._insert_unit(conn, operator, house_id, u, now)
        return self.house_view(house_id, operator)

    def change_house(self, operator, house_id, payload, idem_key):
        return self._run_idempotent(
            f"PUT /houses/{house_id}", operator["operator_id"], idem_key, payload,
            lambda conn: self._change_house(conn, operator, house_id, payload))

    def _change_house(self, conn, operator, house_id, p):
        h = self.store.one("SELECT * FROM houses WHERE house_id = ?", (house_id,))
        if h is None:
            raise DomainError("not_found", f"房屋 {house_id} 不存在", status=404)
        if h["status"] != "active":
            raise DomainError("house_inactive", f"房屋 {house_id} 已注销，不能变更", status=409)
        self._check_version(h["current_version"], p["expected_version"], "房屋")
        cur = self.store.one(
            "SELECT * FROM house_versions WHERE house_id = ? AND version = ?",
            (house_id, h["current_version"]))
        address = _pick(p.get("address"), cur["address"])
        zone = _pick(p.get("zone"), cur["zone"])
        responsibles = _pick(p.get("responsibles"), json.loads(cur["responsibles"]))
        self._validate_responsibles(responsibles)
        address_key = self._check_address_free(address, exclude_house_id=house_id)
        new_version = h["current_version"] + 1
        conn.execute(
            "INSERT INTO house_versions(house_id, version, address, zone, responsibles,"
            " changed_by, changed_at, change_note) VALUES (?,?,?,?,?,?,?,?)",
            (house_id, new_version, address, zone,
             json.dumps(responsibles, ensure_ascii=False),
             operator["operator_id"], _now(), p.get("change_note")))
        conn.execute(
            "UPDATE houses SET current_version = ?, address_key = ?, zone = ? WHERE house_id = ?",
            (new_version, address_key, zone, house_id))
        return self.house_view(house_id, operator)

    def _house_responsibles(self, house_id: str, can_view: bool) -> list:
        h = self.store.one("SELECT current_version FROM houses WHERE house_id = ?", (house_id,))
        v = self.store.one(
            "SELECT responsibles FROM house_versions WHERE house_id = ? AND version = ?",
            (house_id, h["current_version"]))
        out = []
        for r in json.loads(v["responsibles"]):
            entry = {"person_id": r["person_id"], "role": r["role"]}
            person = self._current_person(r["person_id"])
            if person is not None:
                entry.update({
                    "name": person["name"],
                    "phone": person["phone"] if can_view else mask_phone(person["phone"]),
                    "id_expires_on": person["id_expires_on"],
                })
            if r["role"] == "agent":
                entry["authorization_doc"] = r.get("authorization_doc")
                entry["authorization_expires_on"] = r.get("authorization_expires_on")
            out.append(entry)
        return out

    def house_view(self, house_id, operator):
        h = self.store.one("SELECT * FROM houses WHERE house_id = ?", (house_id,))
        if h is None:
            raise DomainError("not_found", f"房屋 {house_id} 不存在", status=404)
        v = self.store.one(
            "SELECT * FROM house_versions WHERE house_id = ? AND version = ?",
            (house_id, h["current_version"]))
        can_view = self._can_view_contact(operator, h["zone"])
        units = [self._unit_current_view(row["unit_id"]) for row in
                 self.store.all("SELECT unit_id FROM units WHERE house_id = ? ORDER BY unit_id",
                                (house_id,))]
        return {
            "house_id": house_id,
            "version": h["current_version"],
            "address": v["address"],
            "zone": h["zone"],
            "status": h["status"],
            "responsibles": self._house_responsibles(house_id, can_view),
            "units": units,
            "contact_masked": not can_view,
            "created_at": h["created_at"],
            "updated_by": v["changed_by"],
            "updated_at": v["changed_at"],
        }

    def list_houses(self, operator, zone=None):
        if zone:
            rows = self.store.all(
                "SELECT house_id FROM houses WHERE status = 'active' AND zone = ? ORDER BY house_id",
                (zone,))
        else:
            rows = self.store.all(
                "SELECT house_id FROM houses WHERE status = 'active' ORDER BY house_id")
        return [self.house_view(r["house_id"], operator) for r in rows]

    def house_history(self, house_id, operator):
        h = self.store.one("SELECT * FROM houses WHERE house_id = ?", (house_id,))
        if h is None:
            raise DomainError("not_found", f"房屋 {house_id} 不存在", status=404)
        rows = self.store.all(
            "SELECT * FROM house_versions WHERE house_id = ? ORDER BY version", (house_id,))
        history = []
        for r in rows:
            responsibles = []
            for resp in json.loads(r["responsibles"]):
                person = self._current_person(resp["person_id"])
                responsibles.append({
                    "person_id": resp["person_id"],
                    "role": resp["role"],
                    "name": person["name"] if person else None,
                })
            history.append({
                "version": r["version"],
                "address": r["address"],
                "zone": r["zone"],
                "responsibles": responsibles,
                "changed_by": r["changed_by"],
                "changed_at": r["changed_at"],
                "change_note": r["change_note"],
            })
        return history

    # ---------------------------------------------------------------- 居住单元

    def _insert_unit(self, conn, operator, house_id, u, now, note="登记"):
        unit_id = self.store.next_id(conn, "unit", "U")
        conn.execute(
            "INSERT INTO units(unit_id, house_id, current_version, created_at) VALUES (?,?,?,?)",
            (unit_id, house_id, 1, now))
        conn.execute(
            "INSERT INTO unit_versions(unit_id, version, room_label, purpose, capacity,"
            " changed_by, changed_at, change_note) VALUES (?,?,?,?,?,?,?,?)",
            (unit_id, 1, u["room_label"], u["purpose"], u["capacity"],
             operator["operator_id"], now, note))
        return unit_id

    def add_unit(self, operator, house_id, payload, idem_key):
        return self._run_idempotent(
            f"POST /houses/{house_id}/units", operator["operator_id"], idem_key, payload,
            lambda conn: self._add_unit(conn, operator, house_id, payload))

    def _add_unit(self, conn, operator, house_id, p):
        h = self.store.one("SELECT * FROM houses WHERE house_id = ?", (house_id,))
        if h is None:
            raise DomainError("not_found", f"房屋 {house_id} 不存在", status=404)
        if h["status"] != "active":
            raise DomainError("house_inactive", f"房屋 {house_id} 已注销，不能新增单元", status=409)
        unit_id = self._insert_unit(conn, operator, house_id, p, _now(), note="新增单元")
        return self.unit_view(unit_id, operator)

    def change_unit(self, operator, unit_id, payload, idem_key):
        return self._run_idempotent(
            f"PUT /units/{unit_id}", operator["operator_id"], idem_key, payload,
            lambda conn: self._change_unit(conn, operator, unit_id, payload))

    def _change_unit(self, conn, operator, unit_id, p):
        u = self.store.one("SELECT * FROM units WHERE unit_id = ?", (unit_id,))
        if u is None:
            raise DomainError("not_found", f"居住单元 {unit_id} 不存在", status=404)
        self._check_version(u["current_version"], p["expected_version"], "居住单元")
        cur = self.store.one(
            "SELECT * FROM unit_versions WHERE unit_id = ? AND version = ?",
            (unit_id, u["current_version"]))
        new_version = u["current_version"] + 1
        conn.execute(
            "INSERT INTO unit_versions(unit_id, version, room_label, purpose, capacity,"
            " changed_by, changed_at, change_note) VALUES (?,?,?,?,?,?,?,?)",
            (unit_id, new_version,
             _pick(p.get("room_label"), cur["room_label"]),
             _pick(p.get("purpose"), cur["purpose"]),
             _pick(p.get("capacity"), cur["capacity"]),
             operator["operator_id"], _now(), p.get("change_note")))
        conn.execute("UPDATE units SET current_version = ? WHERE unit_id = ?",
                     (new_version, unit_id))
        return self.unit_view(unit_id, operator)

    def _unit_current_view(self, unit_id: str) -> dict:
        u = self.store.one("SELECT * FROM units WHERE unit_id = ?", (unit_id,))
        v = self.store.one(
            "SELECT * FROM unit_versions WHERE unit_id = ? AND version = ?",
            (unit_id, u["current_version"]))
        return {
            "unit_id": unit_id,
            "house_id": u["house_id"],
            "version": u["current_version"],
            "room_label": v["room_label"],
            "purpose": v["purpose"],
            "capacity": v["capacity"],
        }

    def unit_view(self, unit_id, operator):
        if self.store.one("SELECT unit_id FROM units WHERE unit_id = ?", (unit_id,)) is None:
            raise DomainError("not_found", f"居住单元 {unit_id} 不存在", status=404)
        return self._unit_current_view(unit_id)

    def unit_history(self, unit_id, operator):
        if self.store.one("SELECT unit_id FROM units WHERE unit_id = ?", (unit_id,)) is None:
            raise DomainError("not_found", f"居住单元 {unit_id} 不存在", status=404)
        rows = self.store.all(
            "SELECT * FROM unit_versions WHERE unit_id = ? ORDER BY version", (unit_id,))
        return [{
            "version": r["version"],
            "room_label": r["room_label"],
            "purpose": r["purpose"],
            "capacity": r["capacity"],
            "changed_by": r["changed_by"],
            "changed_at": r["changed_at"],
            "change_note": r["change_note"],
        } for r in rows]

    # ---------------------------------------------------------------- 检查项

    def register_check_item(self, operator, payload, idem_key):
        return self._run_idempotent(
            "POST /check-items", operator["operator_id"], idem_key, payload,
            lambda conn: self._register_check_item(conn, operator, payload))

    def _register_check_item(self, conn, operator, p):
        clash = self.store.one(
            "SELECT c.item_id FROM check_items c JOIN check_item_versions v "
            "  ON v.item_id = c.item_id AND v.version = c.current_version "
            "WHERE v.code = ?", (p["code"],))
        if clash is not None:
            raise DomainError("validation_failed", "检查项校验未通过", fields=[
                {"field": "code", "reason": f"检查项编码已被 {clash['item_id']} 使用"}])
        item_id = self.store.next_id(conn, "check_item", "CI")
        now = _now()
        conn.execute(
            "INSERT INTO check_items(item_id, current_version, created_at) VALUES (?,?,?)",
            (item_id, 1, now))
        conn.execute(
            "INSERT INTO check_item_versions(item_id, version, code, title, category, severity,"
            " changed_by, changed_at, change_note) VALUES (?,?,?,?,?,?,?,?,?)",
            (item_id, 1, p["code"], p["title"], p["category"], p["severity"],
             operator["operator_id"], now, "登记"))
        return self.check_item_view(item_id)

    def change_check_item(self, operator, item_id, payload, idem_key):
        return self._run_idempotent(
            f"PUT /check-items/{item_id}", operator["operator_id"], idem_key, payload,
            lambda conn: self._change_check_item(conn, operator, item_id, payload))

    def _change_check_item(self, conn, operator, item_id, p):
        row = self.store.one("SELECT * FROM check_items WHERE item_id = ?", (item_id,))
        if row is None:
            raise DomainError("not_found", f"检查项 {item_id} 不存在", status=404)
        self._check_version(row["current_version"], p["expected_version"], "检查项")
        cur = self.store.one(
            "SELECT * FROM check_item_versions WHERE item_id = ? AND version = ?",
            (item_id, row["current_version"]))
        new_version = row["current_version"] + 1
        conn.execute(
            "INSERT INTO check_item_versions(item_id, version, code, title, category, severity,"
            " changed_by, changed_at, change_note) VALUES (?,?,?,?,?,?,?,?,?)",
            (item_id, new_version, cur["code"],
             _pick(p.get("title"), cur["title"]),
             _pick(p.get("category"), cur["category"]),
             _pick(p.get("severity"), cur["severity"]),
             operator["operator_id"], _now(), p.get("change_note")))
        conn.execute("UPDATE check_items SET current_version = ? WHERE item_id = ?",
                     (new_version, item_id))
        return self.check_item_view(item_id)

    def _current_check_item(self, item_id: str):
        row = self.store.one(
            "SELECT c.item_id, c.current_version, v.code, v.title, v.category, v.severity "
            "FROM check_items c JOIN check_item_versions v "
            "  ON v.item_id = c.item_id AND v.version = c.current_version "
            "WHERE c.item_id = ?", (item_id,))
        return dict(row) if row else None

    def check_item_view(self, item_id):
        cur = self._current_check_item(item_id)
        if cur is None:
            raise DomainError("not_found", f"检查项 {item_id} 不存在", status=404)
        return {
            "item_id": cur["item_id"],
            "version": cur["current_version"],
            "code": cur["code"],
            "title": cur["title"],
            "category": cur["category"],
            "severity": cur["severity"],
        }

    def list_check_items(self):
        rows = self.store.all("SELECT item_id FROM check_items ORDER BY item_id")
        return [self.check_item_view(r["item_id"]) for r in rows]

    def check_item_history(self, item_id):
        if self._current_check_item(item_id) is None:
            raise DomainError("not_found", f"检查项 {item_id} 不存在", status=404)
        rows = self.store.all(
            "SELECT * FROM check_item_versions WHERE item_id = ? ORDER BY version", (item_id,))
        return [{
            "version": r["version"],
            "code": r["code"],
            "title": r["title"],
            "category": r["category"],
            "severity": r["severity"],
            "changed_by": r["changed_by"],
            "changed_at": r["changed_at"],
            "change_note": r["change_note"],
        } for r in rows]

    # ---------------------------------------------------------------- 核查

    def _pin_results(self, results_in: list[dict]) -> list:
        """把核查结果定版到检查项当前版本；归档后即使检查项升级也不受影响。"""
        fields = []
        results = []
        seen = set()
        for i, r in enumerate(results_in):
            item = self._current_check_item(r["item_id"])
            if item is None:
                fields.append({"field": f"results[{i}].item_id",
                               "reason": f"检查项 {r['item_id']} 不存在"})
                continue
            if item["item_id"] in seen:
                fields.append({"field": f"results[{i}].item_id",
                               "reason": f"检查项 {r['item_id']} 重复出现"})
                continue
            seen.add(item["item_id"])
            results.append({
                "item_id": item["item_id"],
                "item_version": item["current_version"],
                "result": r["result"],
                "note": r.get("note"),
            })
        if fields:
            raise DomainError("validation_failed", "核查结果校验未通过", fields=fields)
        return results

    def create_inspection(self, operator, payload, idem_key):
        return self._run_idempotent(
            "POST /inspections", operator["operator_id"], idem_key, payload,
            lambda conn: self._create_inspection(conn, operator, payload))

    def _create_inspection(self, conn, operator, p):
        h = self.store.one("SELECT * FROM houses WHERE house_id = ?", (p["house_id"],))
        if h is None:
            raise DomainError("not_found", f"房屋 {p['house_id']} 不存在", status=404)
        if h["status"] != "active":
            raise DomainError("house_inactive", f"房屋 {p['house_id']} 已注销，不能核查", status=409)
        if p.get("unit_id"):
            u = self.store.one(
                "SELECT unit_id FROM units WHERE unit_id = ? AND house_id = ?",
                (p["unit_id"], p["house_id"]))
            if u is None:
                raise DomainError("validation_failed", "核查校验未通过", fields=[
                    {"field": "unit_id", "reason": f"居住单元 {p['unit_id']} 不存在或不属于该房屋"}])
        results = self._pin_results(p["results"])
        inspection_id = self.store.next_id(conn, "inspection", "IN")
        conn.execute(
            "INSERT INTO inspections(inspection_id, house_id, unit_id, inspector_id, version,"
            " status, results, created_at, archived_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (inspection_id, p["house_id"], p.get("unit_id"), operator["operator_id"], 1,
             "open", json.dumps(results, ensure_ascii=False), _now(), None))
        return self.inspection_view(inspection_id, operator)

    def correct_inspection(self, operator, inspection_id, payload, idem_key):
        return self._run_idempotent(
            f"PUT /inspections/{inspection_id}", operator["operator_id"], idem_key, payload,
            lambda conn: self._correct_inspection(conn, operator, inspection_id, payload))

    def _correct_inspection(self, conn, operator, inspection_id, p):
        insp = self.store.one(
            "SELECT * FROM inspections WHERE inspection_id = ?", (inspection_id,))
        if insp is None:
            raise DomainError("not_found", f"检查记录 {inspection_id} 不存在", status=404)
        if insp["status"] == "archived":
            raise DomainError("inspection_archived",
                              "已归档的检查记录不可被新版本覆盖", status=409)
        self._check_version(insp["version"], p["expected_version"], "检查记录")
        results = self._pin_results(p["results"])
        conn.execute(
            "UPDATE inspections SET results = ?, version = ? WHERE inspection_id = ?",
            (json.dumps(results, ensure_ascii=False), insp["version"] + 1, inspection_id))
        return self.inspection_view(inspection_id, operator)

    def archive_inspection(self, operator, inspection_id, idem_key):
        return self._run_idempotent(
            f"POST /inspections/{inspection_id}/archive", operator["operator_id"], idem_key, {},
            lambda conn: self._archive_inspection(conn, operator, inspection_id))

    def _archive_inspection(self, conn, operator, inspection_id):
        insp = self.store.one(
            "SELECT * FROM inspections WHERE inspection_id = ?", (inspection_id,))
        if insp is None:
            raise DomainError("not_found", f"检查记录 {inspection_id} 不存在", status=404)
        if insp["status"] == "archived":
            raise DomainError("inspection_archived", "检查记录已归档，不可重复归档", status=409)
        conn.execute(
            "UPDATE inspections SET status = 'archived', archived_at = ? WHERE inspection_id = ?",
            (_now(), inspection_id))
        return self.inspection_view(inspection_id, operator)

    def inspection_view(self, inspection_id, operator):
        insp = self.store.one(
            "SELECT * FROM inspections WHERE inspection_id = ?", (inspection_id,))
        if insp is None:
            raise DomainError("not_found", f"检查记录 {inspection_id} 不存在", status=404)
        results = []
        for r in json.loads(insp["results"]):
            iv = self.store.one(
                "SELECT code, title, category, severity FROM check_item_versions "
                "WHERE item_id = ? AND version = ?",
                (r["item_id"], r["item_version"]))
            results.append({
                **r,
                "code": iv["code"] if iv else None,
                "title": iv["title"] if iv else None,
                "severity": iv["severity"] if iv else None,
            })
        return {
            "inspection_id": inspection_id,
            "house_id": insp["house_id"],
            "unit_id": insp["unit_id"],
            "inspector_id": insp["inspector_id"],
            "version": insp["version"],
            "status": insp["status"],
            "results": results,
            "created_at": insp["created_at"],
            "archived_at": insp["archived_at"],
        }

    # ---------------------------------------------------------------- 限期整改与复核销号

    def issue_rectification(self, operator, payload, idem_key):
        return self._run_idempotent(
            "POST /rectifications", operator["operator_id"], idem_key, payload,
            lambda conn: self._issue_rectification(conn, operator, payload))

    def _issue_rectification(self, conn, operator, p):
        insp = self.store.one(
            "SELECT * FROM inspections WHERE inspection_id = ?", (p["inspection_id"],))
        if insp is None:
            raise DomainError("not_found", f"检查记录 {p['inspection_id']} 不存在", status=404)
        results = json.loads(insp["results"])
        match = [r for r in results if r["item_id"] == p["item_id"]]
        if not match:
            raise DomainError("validation_failed", "整改校验未通过", fields=[
                {"field": "item_id", "reason": "该检查记录不包含此检查项"}])
        if match[0]["result"] != "fail":
            raise DomainError("validation_failed", "整改校验未通过", fields=[
                {"field": "item_id", "reason": "该检查项核查结果为合格，无需整改"}])
        dup = self.store.one(
            "SELECT rect_id FROM rectifications "
            "WHERE inspection_id = ? AND item_id = ? AND status = 'open'",
            (p["inspection_id"], p["item_id"]))
        if dup is not None:
            raise DomainError("duplicate_rectification",
                              "该隐患已存在未完成的整改单，不得重复下发",
                              status=409, extra={"rect_id": dup["rect_id"]})
        if p["deadline"] <= _today().isoformat():
            raise DomainError("validation_failed", "整改校验未通过", fields=[
                {"field": "deadline", "reason": "整改期限必须晚于今日"}])
        rect_id = self.store.next_id(conn, "rectification", "R")
        now = _now()
        conn.execute(
            "INSERT INTO rectifications(rect_id, inspection_id, house_id, item_id, item_version,"
            " hazard, deadline, status, created_by, created_at, closed_at, closed_by, review_note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rect_id, p["inspection_id"], insp["house_id"], p["item_id"], match[0]["item_version"],
             p["hazard"], p["deadline"], "open", operator["operator_id"], now, None, None, None))
        self._add_event(conn, rect_id, "issued", operator, p["hazard"])
        return self.rectification_view(rect_id, operator)

    def review_rectification(self, operator, rect_id, payload, idem_key):
        return self._run_idempotent(
            f"POST /rectifications/{rect_id}/review", operator["operator_id"], idem_key, payload,
            lambda conn: self._review_rectification(conn, operator, rect_id, payload))

    def _review_rectification(self, conn, operator, rect_id, p):
        rect = self.store.one(
            "SELECT * FROM rectifications WHERE rect_id = ?", (rect_id,))
        if rect is None:
            raise DomainError("not_found", f"整改单 {rect_id} 不存在", status=404)
        if rect["status"] == "closed":
            raise DomainError("rectification_closed",
                              "整改单已复核销号，不可重复复核", status=409)
        if p["passed"]:
            now = _now()
            conn.execute(
                "UPDATE rectifications SET status = 'closed', closed_at = ?, closed_by = ?,"
                " review_note = ? WHERE rect_id = ?",
                (now, operator["operator_id"], p.get("note"), rect_id))
            self._add_event(conn, rect_id, "closed", operator, p.get("note") or "复核通过，予以销号")
        else:
            self._add_event(conn, rect_id, "review_rejected", operator,
                            p.get("note") or "复核未通过，继续整改")
            if p.get("new_deadline"):
                if p["new_deadline"] <= _today().isoformat():
                    raise DomainError("validation_failed", "复核校验未通过", fields=[
                        {"field": "new_deadline", "reason": "新的整改期限必须晚于今日"}])
                conn.execute(
                    "UPDATE rectifications SET deadline = ? WHERE rect_id = ?",
                    (p["new_deadline"], rect_id))
                self._add_event(conn, rect_id, "deadline_extended", operator,
                                f"整改期限调整为 {p['new_deadline']}")
        return self.rectification_view(rect_id, operator)

    def _add_event(self, conn, rect_id, event, operator, note):
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM rectification_events WHERE rect_id = ?",
            (rect_id,)).fetchone()
        conn.execute(
            "INSERT INTO rectification_events(rect_id, seq, event, actor, at, note) "
            "VALUES (?,?,?,?,?,?)",
            (rect_id, row["max_seq"] + 1, event, operator["operator_id"], _now(), note))

    def rectification_view(self, rect_id, operator):
        r = self.store.one("SELECT * FROM rectifications WHERE rect_id = ?", (rect_id,))
        if r is None:
            raise DomainError("not_found", f"整改单 {rect_id} 不存在", status=404)
        h = self.store.one("SELECT * FROM houses WHERE house_id = ?", (r["house_id"],))
        can_view = self._can_view_contact(operator, h["zone"])
        item = self.store.one(
            "SELECT code, title FROM check_item_versions WHERE item_id = ? AND version = ?",
            (r["item_id"], r["item_version"]))
        events = self.store.all(
            "SELECT seq, event, actor, at, note FROM rectification_events "
            "WHERE rect_id = ? ORDER BY seq", (rect_id,))
        days_remaining = (dt.date.fromisoformat(r["deadline"]) - _today()).days
        return {
            "rect_id": rect_id,
            "inspection_id": r["inspection_id"],
            "house_id": r["house_id"],
            "zone": h["zone"],
            "item": {"item_id": r["item_id"], "item_version": r["item_version"],
                     "code": item["code"] if item else None,
                     "title": item["title"] if item else None},
            "hazard": r["hazard"],
            "deadline": r["deadline"],
            "days_remaining": days_remaining,
            "overdue": r["status"] == "open" and days_remaining < 0,
            "status": r["status"],
            "created_by": r["created_by"],
            "created_at": r["created_at"],
            "closed_at": r["closed_at"],
            "closed_by": r["closed_by"],
            "review_note": r["review_note"],
            "responsibles": self._house_responsibles(r["house_id"], can_view),
            "events": [dict(e) for e in events],
        }

    def list_rectifications(self, operator, status=None, zone=None, house_id=None):
        sql = ("SELECT r.rect_id FROM rectifications r JOIN houses h ON h.house_id = r.house_id "
               "WHERE 1=1")
        args = []
        if status:
            sql += " AND r.status = ?"
            args.append(status)
        if zone:
            sql += " AND h.zone = ?"
            args.append(zone)
        if house_id:
            sql += " AND r.house_id = ?"
            args.append(house_id)
        sql += " ORDER BY r.deadline, r.rect_id"
        rows = self.store.all(sql, tuple(args))
        return [self.rectification_view(r["rect_id"], operator) for r in rows]

    # ---------------------------------------------------------------- 管理端

    def admin_dashboard(self):
        admin = {"operator_id": "admin", "role": "admin", "zones": []}
        open_rects = [
            self.rectification_view(r["rect_id"], admin)
            for r in self.store.all(
                "SELECT rect_id FROM rectifications WHERE status = 'open' ORDER BY deadline")
        ]
        recent_changes = self.store.all(
            "SELECT * FROM ("
            "  SELECT 'house' AS kind, house_id AS entity_id, version, changed_by, changed_at,"
            "         change_note FROM house_versions"
            "  UNION ALL"
            "  SELECT 'unit', unit_id, version, changed_by, changed_at, change_note FROM unit_versions"
            "  UNION ALL"
            "  SELECT 'person', person_id, version, changed_by, changed_at, change_note FROM person_versions"
            "  UNION ALL"
            "  SELECT 'check_item', item_id, version, changed_by, changed_at, change_note"
            "  FROM check_item_versions"
            ") ORDER BY changed_at DESC, entity_id DESC LIMIT 50")
        stats = {
            "active_houses": self.store.one(
                "SELECT COUNT(*) AS c FROM houses WHERE status = 'active'")["c"],
            "open_rectifications": len(open_rects),
            "overdue_rectifications": sum(1 for r in open_rects if r["overdue"]),
            "archived_inspections": self.store.one(
                "SELECT COUNT(*) AS c FROM inspections WHERE status = 'archived'")["c"],
        }
        return {
            "generated_at": _now(),
            "stats": stats,
            "open_rectifications": open_rects,
            "recent_changes": [dict(r) for r in recent_changes],
        }
