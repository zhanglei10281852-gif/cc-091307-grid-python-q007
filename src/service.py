"""社区房屋租赁安全登记 —— 领域服务层。

需求映射：
- 房屋 / 居住单元 / 责任人 / 检查项：版本化登记与变更，每次写入 revisions 留痕；
- 核查 → 限期整改 → 复核销号：状态机 OPEN → RECTIFYING → CLOSED（归档），
  closed_at 证明隐患何时解除；
- 证件过期 / 授权缺失 / 地址冲突：拒绝写入并返回字段级原因（ValidationFailed.fields）；
- 已归档检查记录：服务层拒绝 + 数据库触发器兜底，不可被新版本覆盖；
- 并行修改：expected_version 乐观锁，冲突返回当前版本号与当前快照；
- 幂等：Idempotency-Key 重放返回首次响应；相同材料重复登记自动去重；
- 片区权限：非本片区查询时联系方式脱敏。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from datetime import date, datetime, timezone

from .db import Database
from .errors import (
    ArchivedImmutable,
    IdempotencyKeyReused,
    NotFound,
    PartyMaterialMismatch,
    PermissionDenied,
    ValidationFailed,
    VersionConflict,
)
from .masking import mask_id_card, mask_phone

ROLES = ("landlord", "agent", "tenant")
ROLE_LABELS = {"landlord": "房东", "agent": "中介", "tenant": "租客"}
UNIT_USAGES = ("卧室", "客厅", "隔断间", "厨房", "卫生间", "阳台", "储藏室", "其他")
RISK_LEVELS = ("none", "low", "medium", "high")
PROPERTY_STATUSES = ("active", "inactive")

STATUS_OPEN = "OPEN"                # 待整改
STATUS_RECTIFYING = "RECTIFYING"    # 限期整改中
STATUS_CLOSED = "CLOSED"            # 复核销号（归档）

_PHONE_RE = re.compile(r"^1\d{10}$")
_ID_CARD_RE = re.compile(r"^\d{17}[\dXx]$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> date:
    return date.today()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def normalize_address(address: str) -> str:
    """地址归一化：全角转半角、去空白、小写，用于重复登记冲突检测。"""
    text = unicodedata.normalize("NFKC", str(address))
    text = re.sub(r"\s+", "", text)
    return text.lower()


def _parse_date(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _diff(old: dict, new: dict, fields) -> dict:
    return {f: {"from": old.get(f), "to": new.get(f)} for f in fields if old.get(f) != new.get(f)}


class Operator:
    """当前操作人（生产环境由网关/登录态解析，此处经请求头传入）。"""

    def __init__(self, operator_id: str, role: str = "grid", zones=()):
        self.id = operator_id or "anonymous"
        self.role = role
        self.zones = frozenset(zones)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can_view_contacts(self, zone: str) -> bool:
        """管理端或本片区网格员可见完整联系方式，其余脱敏。"""
        return self.is_admin or zone in self.zones


class HousingSafetyService:
    """房屋安全登记领域服务。所有写方法均为事务边界。"""

    def __init__(self, db_path: str = "data/housing.db"):
        self.db = Database(db_path)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _idempotent(self, endpoint, key, payload, fn):
        """幂等执行写操作：相同幂等键 + 相同材料 → 返回首次响应，不重复写入。

        仅成功响应会被记录；校验失败是确定性的，重试会得到相同错误。
        """
        if not key:
            with self.db.transaction() as conn:
                return fn(conn)
        request_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM idempotency WHERE key = ?", (key,)).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash or row["endpoint"] != endpoint:
                    raise IdempotencyKeyReused(key)
                return json.loads(row["response_body"])
            result = fn(conn)
            conn.execute(
                "INSERT INTO idempotency (key, endpoint, request_hash, status_code, response_body, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (key, endpoint, request_hash, 200,
                 json.dumps(result, ensure_ascii=False, default=str), _now()),
            )
            return result

    @staticmethod
    def _require_version(current: dict, expected_version, label: str):
        if expected_version is None:
            raise ValidationFailed(
                {"expected_version": f"必须提供预期版本号（当前版本 {current['version']}）"})
        try:
            expected = int(expected_version)
        except (TypeError, ValueError):
            raise ValidationFailed({"expected_version": "预期版本号必须为整数"})
        if expected != current["version"]:
            raise VersionConflict(label, current)

    def _record_revision(self, conn, entity_type, entity_id, version, change_type, changes, snapshot, operator):
        conn.execute(
            "INSERT INTO revisions (entity_type, entity_id, version, change_type, changes, snapshot, changed_by, changed_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (entity_type, entity_id, version, change_type,
             json.dumps(changes, ensure_ascii=False) if changes else None,
             json.dumps(snapshot, ensure_ascii=False, default=str),
             operator.id, _now()),
        )

    def _must_get_property(self, conn, pid) -> dict:
        row = conn.execute("SELECT * FROM properties WHERE id = ?", (pid,)).fetchone()
        if row is None:
            raise NotFound("房屋", pid)
        return dict(row)

    # ------------------------------------------------------------------
    # 房屋登记 / 变更 / 查询
    # ------------------------------------------------------------------

    def register_property(self, data: dict, operator: Operator, idempotency_key=None) -> dict:
        data = dict(data or {})
        return self._idempotent(
            "POST /properties", idempotency_key, data,
            lambda conn: self._tx_register_property(conn, data, operator),
        )

    def _tx_register_property(self, conn, data, operator):
        errors = {}
        zone = str(data.get("zone") or "").strip()
        address = str(data.get("address") or "").strip()
        owner_name = str(data.get("owner_name") or "").strip()
        building_type = str(data.get("building_type") or "").strip() or None
        if not zone:
            errors["zone"] = "所属片区不能为空"
        if not address:
            errors["address"] = "房屋地址不能为空"
        if not owner_name:
            errors["owner_name"] = "房屋所有权人不能为空"
        if errors:
            raise ValidationFailed(errors)
        address_key = normalize_address(address)
        clash = conn.execute(
            "SELECT id, owner_name FROM properties WHERE address_key = ?", (address_key,)).fetchone()
        if clash is not None:
            raise ValidationFailed({
                "address": f"地址冲突：该地址已由记录 {clash['id']}（登记人 {clash['owner_name']}）登记，"
                           "同一套房屋不可由房东/中介/租客重复登记"
            })
        pid = _new_id("prop")
        now = _now()
        try:
            conn.execute(
                "INSERT INTO properties (id, zone, address, address_key, owner_name, building_type, status,"
                " version, created_at, updated_at) VALUES (?,?,?,?,?,?,'active',1,?,?)",
                (pid, zone, address, address_key, owner_name, building_type, now, now),
            )
        except sqlite3.IntegrityError:
            raise ValidationFailed({"address": "地址冲突：该地址已存在登记记录"})
        snapshot = dict(conn.execute("SELECT * FROM properties WHERE id = ?", (pid,)).fetchone())
        self._record_revision(conn, "property", pid, 1, "register", None, snapshot, operator)
        return self._property_view(snapshot, operator)

    def update_property(self, pid, data: dict, expected_version, operator: Operator) -> dict:
        data = dict(data or {})
        with self.db.transaction() as conn:
            old = self._must_get_property(conn, pid)
            self._require_version(old, expected_version, "房屋")
            merged = dict(old)
            for field in ("zone", "address", "owner_name", "building_type", "status"):
                if field in data and data[field] is not None:
                    merged[field] = str(data[field]).strip()
            errors = {}
            if not merged["zone"]:
                errors["zone"] = "所属片区不能为空"
            if not merged["address"]:
                errors["address"] = "房屋地址不能为空"
            if not merged["owner_name"]:
                errors["owner_name"] = "房屋所有权人不能为空"
            if merged["status"] not in PROPERTY_STATUSES:
                errors["status"] = f"状态必须为 {'/'.join(PROPERTY_STATUSES)}"
            if errors:
                raise ValidationFailed(errors)
            merged["address_key"] = normalize_address(merged["address"])
            clash = conn.execute(
                "SELECT id FROM properties WHERE address_key = ? AND id != ?",
                (merged["address_key"], pid)).fetchone()
            if clash is not None:
                raise ValidationFailed({"address": f"地址冲突：该地址已由记录 {clash['id']} 登记"})
            new_version = old["version"] + 1
            cur = conn.execute(
                "UPDATE properties SET zone=?, address=?, address_key=?, owner_name=?, building_type=?,"
                " status=?, version=?, updated_at=? WHERE id=? AND version=?",
                (merged["zone"], merged["address"], merged["address_key"], merged["owner_name"],
                 merged.get("building_type"), merged["status"], new_version, _now(), pid, old["version"]))
            if cur.rowcount != 1:  # 兜底：版本在判断后被并发修改
                raise VersionConflict("房屋", self._must_get_property(conn, pid))
            changes = _diff(old, merged, ("zone", "address", "owner_name", "building_type", "status"))
            snapshot = dict(conn.execute("SELECT * FROM properties WHERE id = ?", (pid,)).fetchone())
            self._record_revision(conn, "property", pid, new_version, "update", changes, snapshot, operator)
            return self._property_view(snapshot, operator)

    def get_property(self, pid, operator: Operator) -> dict:
        row = self.db.fetchone("SELECT * FROM properties WHERE id = ?", (pid,))
        if row is None:
            raise NotFound("房屋", pid)
        return self._property_view(dict(row), operator)

    def list_properties(self, operator: Operator, zone=None) -> list:
        if zone:
            rows = self.db.fetchall("SELECT * FROM properties WHERE zone = ? ORDER BY created_at, id", (zone,))
        else:
            rows = self.db.fetchall("SELECT * FROM properties ORDER BY created_at, id")
        items = []
        for row in rows:
            prop = dict(row)
            open_count = self.db.fetchone(
                "SELECT COUNT(*) AS c FROM inspections WHERE property_id = ? AND status IN ('OPEN','RECTIFYING')",
                (prop["id"],))["c"]
            items.append({
                "id": prop["id"], "zone": prop["zone"], "address": prop["address"],
                "owner_name": prop["owner_name"], "status": prop["status"],
                "version": prop["version"], "open_inspections": open_count,
                "updated_at": prop["updated_at"],
            })
        return items

    def _property_view(self, prop: dict, operator: Operator) -> dict:
        reveal = operator.can_view_contacts(prop["zone"])
        units = [dict(r) for r in self.db.fetchall(
            "SELECT * FROM units WHERE property_id = ? ORDER BY created_at, id", (prop["id"],))]
        parties = [self._party_view(dict(r), reveal) for r in self.db.fetchall(
            "SELECT * FROM parties WHERE property_id = ? ORDER BY created_at, id", (prop["id"],))]
        inspections = [self._inspection_brief(dict(r)) for r in self.db.fetchall(
            "SELECT * FROM inspections WHERE property_id = ? ORDER BY created_at, id", (prop["id"],))]
        out = {k: v for k, v in prop.items() if k != "address_key"}
        out["units"] = units
        out["parties"] = parties
        out["inspections"] = inspections
        return out

    # ------------------------------------------------------------------
    # 居住单元登记 / 变更（用途变化留痕）
    # ------------------------------------------------------------------

    def register_unit(self, pid, data: dict, operator: Operator, idempotency_key=None) -> dict:
        data = dict(data or {})
        return self._idempotent(
            "POST /units", idempotency_key, {"property_id": pid, "body": data},
            lambda conn: self._tx_register_unit(conn, pid, data, operator),
        )

    def _tx_register_unit(self, conn, pid, data, operator):
        self._must_get_property(conn, pid)
        errors = {}
        label = str(data.get("label") or "").strip()
        usage = str(data.get("usage") or "").strip()
        if not label:
            errors["label"] = "居住单元编号不能为空"
        if usage not in UNIT_USAGES:
            errors["usage"] = f"用途必须为：{'/'.join(UNIT_USAGES)}"
        try:
            capacity = int(data.get("capacity", 1))
            if capacity < 0:
                raise ValueError
        except (TypeError, ValueError):
            capacity = None
            errors["capacity"] = "核定居住人数必须为非负整数"
        if errors:
            raise ValidationFailed(errors)
        dup = conn.execute(
            "SELECT id FROM units WHERE property_id = ? AND label = ?", (pid, label)).fetchone()
        if dup is not None:
            raise ValidationFailed({"label": f"居住单元 {label} 已登记（记录 {dup['id']}）"})
        uid = _new_id("unit")
        now = _now()
        conn.execute(
            "INSERT INTO units (id, property_id, label, usage, capacity, version, created_at, updated_at)"
            " VALUES (?,?,?,?,?,1,?,?)",
            (uid, pid, label, usage, capacity, now, now))
        snapshot = dict(conn.execute("SELECT * FROM units WHERE id = ?", (uid,)).fetchone())
        self._record_revision(conn, "unit", uid, 1, "register", None, snapshot, operator)
        return snapshot

    def update_unit(self, uid, data: dict, expected_version, operator: Operator) -> dict:
        data = dict(data or {})
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM units WHERE id = ?", (uid,)).fetchone()
            if row is None:
                raise NotFound("居住单元", uid)
            old = dict(row)
            self._require_version(old, expected_version, "居住单元")
            merged = dict(old)
            for field in ("label", "usage", "capacity"):
                if field in data and data[field] is not None:
                    merged[field] = data[field]
            errors = {}
            merged["label"] = str(merged["label"] or "").strip()
            merged["usage"] = str(merged["usage"] or "").strip()
            if not merged["label"]:
                errors["label"] = "居住单元编号不能为空"
            if merged["usage"] not in UNIT_USAGES:
                errors["usage"] = f"用途必须为：{'/'.join(UNIT_USAGES)}"
            try:
                merged["capacity"] = int(merged["capacity"])
                if merged["capacity"] < 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors["capacity"] = "核定居住人数必须为非负整数"
            if errors:
                raise ValidationFailed(errors)
            dup = conn.execute(
                "SELECT id FROM units WHERE property_id = ? AND label = ? AND id != ?",
                (old["property_id"], merged["label"], uid)).fetchone()
            if dup is not None:
                raise ValidationFailed({"label": f"居住单元 {merged['label']} 已登记（记录 {dup['id']}）"})
            new_version = old["version"] + 1
            cur = conn.execute(
                "UPDATE units SET label=?, usage=?, capacity=?, version=?, updated_at=?"
                " WHERE id=? AND version=?",
                (merged["label"], merged["usage"], merged["capacity"], new_version, _now(),
                 uid, old["version"]))
            if cur.rowcount != 1:
                fresh = conn.execute("SELECT * FROM units WHERE id = ?", (uid,)).fetchone()
                raise VersionConflict("居住单元", dict(fresh))
            changes = _diff(old, merged, ("label", "usage", "capacity"))
            snapshot = dict(conn.execute("SELECT * FROM units WHERE id = ?", (uid,)).fetchone())
            self._record_revision(conn, "unit", uid, new_version, "update", changes, snapshot, operator)
            return snapshot

    # ------------------------------------------------------------------
    # 责任人登记 / 变更（证件过期、授权缺失拒绝写入）
    # ------------------------------------------------------------------

    def register_party(self, pid, data: dict, operator: Operator, idempotency_key=None) -> dict:
        data = dict(data or {})
        return self._idempotent(
            "POST /parties", idempotency_key, {"property_id": pid, "body": data},
            lambda conn: self._tx_register_party(conn, pid, data, operator),
        )

    def _tx_register_party(self, conn, pid, data, operator):
        prop = self._must_get_property(conn, pid)
        merged = {
            "role": str(data.get("role") or "").strip(),
            "name": str(data.get("name") or "").strip(),
            "phone": str(data.get("phone") or "").strip(),
            "id_card": str(data.get("id_card") or "").strip(),
            "cert_expiry": str(data.get("cert_expiry") or "").strip(),
            "authorization_no": (str(data["authorization_no"]).strip()
                                 if data.get("authorization_no") is not None else None),
        }
        errors = {}
        self._validate_party(merged, errors)
        if errors:
            raise ValidationFailed(errors)
        # 相同材料重复提交 → 幂等去重；材料不一致 → 提示走变更接口
        existing = conn.execute(
            "SELECT * FROM parties WHERE property_id = ? AND role = ? AND id_card = ?",
            (pid, merged["role"], merged["id_card"])).fetchone()
        if existing is not None:
            existing = dict(existing)
            same = all(str(existing[f] or "") == str(merged.get(f) or "")
                       for f in ("name", "phone", "cert_expiry", "authorization_no"))
            if same:
                view = self._party_view(existing, operator.can_view_contacts(prop["zone"]))
                view["deduplicated"] = True
                return view
            raise PartyMaterialMismatch(existing["id"])
        party_id = _new_id("party")
        now = _now()
        conn.execute(
            "INSERT INTO parties (id, property_id, role, name, phone, id_card, cert_expiry,"
            " authorization_no, version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,?)",
            (party_id, pid, merged["role"], merged["name"], merged["phone"], merged["id_card"],
             merged["cert_expiry"], merged["authorization_no"] or None, now, now))
        snapshot = dict(conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone())
        self._record_revision(conn, "party", party_id, 1, "register", None, snapshot, operator)
        return self._party_view(snapshot, operator.can_view_contacts(prop["zone"]))

    def update_party(self, party_id, data: dict, expected_version, operator: Operator) -> dict:
        data = dict(data or {})
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
            if row is None:
                raise NotFound("责任人", party_id)
            old = dict(row)
            self._require_version(old, expected_version, "责任人")
            merged = dict(old)
            for field in ("role", "name", "phone", "id_card", "cert_expiry", "authorization_no"):
                if field in data and data[field] is not None:
                    merged[field] = str(data[field]).strip()
            errors = {}
            self._validate_party(merged, errors)
            if errors:
                raise ValidationFailed(errors)
            new_version = old["version"] + 1
            cur = conn.execute(
                "UPDATE parties SET role=?, name=?, phone=?, id_card=?, cert_expiry=?,"
                " authorization_no=?, version=?, updated_at=? WHERE id=? AND version=?",
                (merged["role"], merged["name"], merged["phone"], merged["id_card"],
                 merged["cert_expiry"], merged.get("authorization_no") or None,
                 new_version, _now(), party_id, old["version"]))
            if cur.rowcount != 1:
                fresh = conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
                raise VersionConflict("责任人", dict(fresh))
            changes = _diff(old, merged,
                            ("role", "name", "phone", "id_card", "cert_expiry", "authorization_no"))
            snapshot = dict(conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone())
            self._record_revision(conn, "party", party_id, new_version, "update", changes, snapshot, operator)
            prop = self._must_get_property(conn, old["property_id"])
            return self._party_view(snapshot, operator.can_view_contacts(prop["zone"]))

    @staticmethod
    def _validate_party(party: dict, errors: dict):
        if party.get("role") not in ROLES:
            errors["role"] = "责任人类型必须为 landlord（房东）/agent（中介）/tenant（租客）"
        if not (party.get("name") or "").strip():
            errors["name"] = "责任人姓名不能为空"
        if not _PHONE_RE.match(str(party.get("phone") or "")):
            errors["phone"] = "联系电话须为 11 位手机号"
        if not _ID_CARD_RE.match(str(party.get("id_card") or "")):
            errors["id_card"] = "证件号码须为 18 位身份证号"
        expiry_raw = str(party.get("cert_expiry") or "").strip()
        if not expiry_raw:
            errors["cert_expiry"] = "证件有效期不能为空"
        else:
            expiry = _parse_date(expiry_raw)
            if expiry is None:
                errors["cert_expiry"] = "证件有效期格式应为 YYYY-MM-DD"
            elif expiry < _today():
                errors["cert_expiry"] = f"证件已于 {expiry_raw} 过期，禁止登记"
        if party.get("role") == "agent" and not (party.get("authorization_no") or "").strip():
            errors["authorization_no"] = "中介责任人缺少授权书编号，禁止登记"

    @staticmethod
    def _party_view(party: dict, reveal: bool) -> dict:
        out = dict(party)
        if not reveal:
            out["phone"] = mask_phone(out["phone"])
            out["id_card"] = mask_id_card(out["id_card"])
        return out

    # ------------------------------------------------------------------
    # 核查 → 限期整改 → 复核销号
    # ------------------------------------------------------------------

    def create_inspection(self, data: dict, operator: Operator, idempotency_key=None) -> dict:
        data = dict(data or {})
        return self._idempotent(
            "POST /inspections", idempotency_key, data,
            lambda conn: self._tx_create_inspection(conn, data, operator),
        )

    def _tx_create_inspection(self, conn, data, operator):
        errors = {}
        pid = str(data.get("property_id") or "").strip()
        if not pid:
            errors["property_id"] = "房屋编号不能为空"
            prop = None
        else:
            prop = self._must_get_property(conn, pid)
        unit_id = data.get("unit_id") or None
        if unit_id and prop is not None:
            unit = conn.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
            if unit is None:
                raise NotFound("居住单元", unit_id)
            if unit["property_id"] != prop["id"]:
                errors["unit_id"] = "居住单元不属于该房屋"
        items = data.get("items")
        items_error = self._validate_items(items)
        if items_error:
            errors["items"] = items_error
        if errors:
            raise ValidationFailed(errors)
        hazard = any(it["result"] == "fail" for it in items)
        result = "hazard" if hazard else "pass"
        risk_level = data.get("risk_level") or ("medium" if hazard else "none")
        if risk_level not in RISK_LEVELS:
            raise ValidationFailed({"risk_level": f"风险等级必须为 {'/'.join(RISK_LEVELS)}"})
        iid = _new_id("insp")
        now = _now()
        if hazard:
            status, archived, closed_at = STATUS_OPEN, 0, None
        else:
            # 核查无隐患：直接归档，记录不可再变
            status, archived, closed_at = STATUS_CLOSED, 1, now
        conn.execute(
            "INSERT INTO inspections (id, property_id, unit_id, inspector, items, result, risk_level,"
            " status, deadline, review_note, archived, version, created_at, updated_at, closed_at)"
            " VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?,1,?,?,?)",
            (iid, prop["id"], unit_id, operator.id, json.dumps(items, ensure_ascii=False),
             result, risk_level, status, archived, now, now, closed_at))
        snapshot = dict(conn.execute("SELECT * FROM inspections WHERE id = ?", (iid,)).fetchone())
        self._record_revision(conn, "inspection", iid, 1, "inspect", None, snapshot, operator)
        return self._inspection_view(snapshot)

    @staticmethod
    def _validate_items(items):
        if not isinstance(items, list) or not items:
            return "检查项不能为空"
        for i, item in enumerate(items):
            if not isinstance(item, dict) or not str(item.get("item") or "").strip():
                return f"第 {i + 1} 个检查项缺少名称"
            if item.get("result") not in ("pass", "fail"):
                return f"第 {i + 1} 个检查项结果必须为 pass/fail"
        return None

    def rectify_inspection(self, iid, deadline, expected_version, operator: Operator) -> dict:
        """限期整改：仅 OPEN 状态可下达，期限必须晚于今日。"""
        with self.db.transaction() as conn:
            old = self._must_get_inspection(conn, iid)
            if old["archived"]:
                raise ArchivedImmutable(iid)
            self._require_version(old, expected_version, "检查记录")
            errors = {}
            if old["status"] != STATUS_OPEN:
                errors["status"] = f"当前状态为 {old['status']}，仅待整改（OPEN）状态可下达限期整改"
            if not deadline:
                errors["deadline"] = "整改期限不能为空"
            else:
                ddl = _parse_date(deadline)
                if ddl is None:
                    errors["deadline"] = "整改期限格式应为 YYYY-MM-DD"
                elif ddl <= _today():
                    errors["deadline"] = "整改期限必须晚于今日"
            if errors:
                raise ValidationFailed(errors)
            new_version = old["version"] + 1
            cur = conn.execute(
                "UPDATE inspections SET status=?, deadline=?, version=?, updated_at=?"
                " WHERE id=? AND version=?",
                (STATUS_RECTIFYING, str(deadline), new_version, _now(), iid, old["version"]))
            if cur.rowcount != 1:
                raise VersionConflict("检查记录", self._must_get_inspection(conn, iid))
            changes = {"status": {"from": old["status"], "to": STATUS_RECTIFYING},
                       "deadline": {"from": old["deadline"], "to": str(deadline)}}
            snapshot = dict(conn.execute("SELECT * FROM inspections WHERE id = ?", (iid,)).fetchone())
            self._record_revision(conn, "inspection", iid, new_version, "rectify", changes, snapshot, operator)
            return self._inspection_view(snapshot)

    def close_inspection(self, iid, review_note, expected_version, operator: Operator) -> dict:
        """复核销号：记录复核结论与销号时间后归档，归档记录不可再变。"""
        with self.db.transaction() as conn:
            old = self._must_get_inspection(conn, iid)
            if old["archived"]:
                raise ArchivedImmutable(iid)
            self._require_version(old, expected_version, "检查记录")
            errors = {}
            if old["status"] not in (STATUS_OPEN, STATUS_RECTIFYING):
                errors["status"] = f"当前状态为 {old['status']}，不可复核销号"
            if not str(review_note or "").strip():
                errors["review_note"] = "复核销号必须填写复核结论"
            if errors:
                raise ValidationFailed(errors)
            new_version = old["version"] + 1
            now = _now()
            cur = conn.execute(
                "UPDATE inspections SET status=?, review_note=?, archived=1, closed_at=?,"
                " version=?, updated_at=? WHERE id=? AND version=?",
                (STATUS_CLOSED, str(review_note).strip(), now, new_version, now, iid, old["version"]))
            if cur.rowcount != 1:
                raise VersionConflict("检查记录", self._must_get_inspection(conn, iid))
            changes = {"status": {"from": old["status"], "to": STATUS_CLOSED},
                       "review_note": {"from": old["review_note"], "to": str(review_note).strip()}}
            snapshot = dict(conn.execute("SELECT * FROM inspections WHERE id = ?", (iid,)).fetchone())
            self._record_revision(conn, "inspection", iid, new_version, "close", changes, snapshot, operator)
            return self._inspection_view(snapshot)

    def get_inspection(self, iid, operator: Operator) -> dict:
        row = self.db.fetchone("SELECT * FROM inspections WHERE id = ?", (iid,))
        if row is None:
            raise NotFound("检查记录", iid)
        return self._inspection_view(dict(row))

    def list_inspections(self, operator: Operator, property_id=None, status=None) -> list:
        sql = "SELECT * FROM inspections"
        conds, params = [], []
        if property_id:
            conds.append("property_id = ?")
            params.append(property_id)
        if status:
            conds.append("status = ?")
            params.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at, id"
        return [self._inspection_view(dict(r)) for r in self.db.fetchall(sql, params)]

    def _must_get_inspection(self, conn, iid) -> dict:
        row = conn.execute("SELECT * FROM inspections WHERE id = ?", (iid,)).fetchone()
        if row is None:
            raise NotFound("检查记录", iid)
        return dict(row)

    @staticmethod
    def _inspection_view(row: dict) -> dict:
        out = dict(row)
        out["items"] = json.loads(out["items"])
        out["archived"] = bool(out["archived"])
        return out

    @staticmethod
    def _inspection_brief(row: dict) -> dict:
        return {
            "id": row["id"], "status": row["status"], "result": row["result"],
            "risk_level": row["risk_level"], "deadline": row["deadline"],
            "archived": bool(row["archived"]), "closed_at": row["closed_at"],
            "version": row["version"],
        }

    # ------------------------------------------------------------------
    # 历史变更 / 管理端风险总览
    # ------------------------------------------------------------------

    def property_history(self, pid, operator: Operator) -> dict:
        prop = self.db.fetchone("SELECT * FROM properties WHERE id = ?", (pid,))
        if prop is None:
            raise NotFound("房屋", pid)
        reveal = operator.can_view_contacts(prop["zone"])
        rows = self.db.fetchall(
            """
            SELECT * FROM revisions
            WHERE (entity_type = 'property' AND entity_id = ?)
               OR (entity_type = 'unit' AND entity_id IN (SELECT id FROM units WHERE property_id = ?))
               OR (entity_type = 'party' AND entity_id IN (SELECT id FROM parties WHERE property_id = ?))
               OR (entity_type = 'inspection' AND entity_id IN (SELECT id FROM inspections WHERE property_id = ?))
            ORDER BY changed_at, rowid
            """,
            (pid, pid, pid, pid))
        revisions = []
        for row in rows:
            r = dict(row)
            changes = json.loads(r["changes"]) if r["changes"] else None
            snapshot = json.loads(r["snapshot"])
            if not reveal and r["entity_type"] == "party":
                # 非本片区：历史快照中的联系方式同样脱敏
                snapshot["phone"] = mask_phone(snapshot.get("phone"))
                snapshot["id_card"] = mask_id_card(snapshot.get("id_card"))
                if changes:
                    for field in ("phone", "id_card"):
                        if field in changes:
                            changes[field] = {"from": "***", "to": "***"}
            revisions.append({
                "entity_type": r["entity_type"], "entity_id": r["entity_id"],
                "version": r["version"], "change_type": r["change_type"],
                "changes": changes, "snapshot": snapshot,
                "changed_by": r["changed_by"], "changed_at": r["changed_at"],
            })
        return {"property_id": pid, "revisions": revisions}

    def risk_overview(self, operator: Operator) -> dict:
        """管理端总览：当前风险、整改期限、责任人（含联系方式）、逾期情况。"""
        if not operator.is_admin:
            raise PermissionDenied("风险总览仅管理端可查看")
        rows = self.db.fetchall(
            """
            SELECT i.id AS inspection_id, i.property_id, i.status, i.risk_level, i.deadline,
                   i.inspector, i.created_at,
                   p.zone, p.address, p.owner_name
            FROM inspections i
            JOIN properties p ON p.id = i.property_id
            WHERE i.status IN ('OPEN', 'RECTIFYING')
            """)
        today = _today()
        risks = []
        for row in rows:
            r = dict(row)
            deadline_date = _parse_date(r["deadline"]) if r["deadline"] else None
            overdue = bool(deadline_date and deadline_date < today)
            party = self.db.fetchone(
                """
                SELECT * FROM parties
                WHERE property_id = ? AND role IN ('landlord', 'agent')
                ORDER BY CASE role WHEN 'landlord' THEN 0 ELSE 1 END, created_at
                LIMIT 1
                """,
                (r["property_id"],))
            responsible = None
            if party is not None:
                party = dict(party)
                responsible = {
                    "id": party["id"], "role": party["role"],
                    "role_label": ROLE_LABELS[party["role"]],
                    "name": party["name"], "phone": party["phone"],
                }
            risks.append({
                "inspection_id": r["inspection_id"],
                "property_id": r["property_id"],
                "zone": r["zone"], "address": r["address"],
                "owner_name": r["owner_name"],
                "status": r["status"], "risk_level": r["risk_level"],
                "deadline": r["deadline"], "overdue": overdue,
                "days_remaining": (deadline_date - today).days if deadline_date else None,
                "responsible": responsible,
                "inspector": r["inspector"],
                "created_at": r["created_at"],
            })
        risks.sort(key=lambda x: (not x["overdue"], x["deadline"] is None, x["deadline"] or ""))
        summary = {
            "open": sum(1 for x in risks if x["status"] == STATUS_OPEN),
            "rectifying": sum(1 for x in risks if x["status"] == STATUS_RECTIFYING),
            "overdue": sum(1 for x in risks if x["overdue"]),
        }
        return {"generated_at": _now(), "summary": summary, "risks": risks}


# 兼容包骨架中的入口命名
Service = HousingSafetyService
