"""SQLite 持久化层：模式定义与事务助手。

所有实体落盘保存，服务重启后归档记录与未完成整改继续可查。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
    id            TEXT PRIMARY KEY,
    zone          TEXT NOT NULL,               -- 所属片区（网格）
    address       TEXT NOT NULL,
    address_key   TEXT NOT NULL UNIQUE,        -- 归一化地址，用于重复登记冲突检测
    owner_name    TEXT NOT NULL,
    building_type TEXT,
    status        TEXT NOT NULL DEFAULT 'active',
    version       INTEGER NOT NULL DEFAULT 1,  -- 乐观锁版本号
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    id          TEXT PRIMARY KEY,
    property_id TEXT NOT NULL REFERENCES properties(id),
    label       TEXT NOT NULL,                 -- 房间编号，如 “南卧-01”
    usage       TEXT NOT NULL,                 -- 用途：卧室/客厅/隔断间/厨房/...
    capacity    INTEGER NOT NULL DEFAULT 1,    -- 核定居住人数
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (property_id, label)
);

CREATE TABLE IF NOT EXISTS parties (
    id               TEXT PRIMARY KEY,
    property_id      TEXT NOT NULL REFERENCES properties(id),
    role             TEXT NOT NULL,            -- landlord/agent/tenant
    name             TEXT NOT NULL,
    phone            TEXT NOT NULL,
    id_card          TEXT NOT NULL,
    cert_expiry      TEXT NOT NULL,            -- 证件有效期 YYYY-MM-DD
    authorization_no TEXT,                     -- 中介授权书编号（中介必填）
    version          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parties_property ON parties(property_id);

CREATE TABLE IF NOT EXISTS inspections (
    id          TEXT PRIMARY KEY,
    property_id TEXT NOT NULL REFERENCES properties(id),
    unit_id     TEXT REFERENCES units(id),
    inspector   TEXT NOT NULL,                 -- 核查网格员
    items       TEXT NOT NULL,                 -- JSON 检查项清单
    result      TEXT NOT NULL,                 -- pass / hazard
    risk_level  TEXT NOT NULL,                 -- none/low/medium/high
    status      TEXT NOT NULL,                 -- OPEN/RECTIFYING/CLOSED
    deadline    TEXT,                          -- 限期整改期限
    review_note TEXT,                          -- 复核结论（销号凭证）
    archived    INTEGER NOT NULL DEFAULT 0,    -- 归档后不可变
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    closed_at   TEXT                           -- 销号时间（证明隐患何时解除）
);
CREATE INDEX IF NOT EXISTS idx_inspections_property ON inspections(property_id);
CREATE INDEX IF NOT EXISTS idx_inspections_status ON inspections(status);

-- 已归档的检查记录不可被新版本覆盖：数据库触发器兜底（服务层同样校验）
CREATE TRIGGER IF NOT EXISTS trg_inspections_archived_no_update
BEFORE UPDATE ON inspections
WHEN OLD.archived = 1
BEGIN
    SELECT RAISE(ABORT, 'ARCHIVED_IMMUTABLE');
END;

CREATE TRIGGER IF NOT EXISTS trg_inspections_archived_no_delete
BEFORE DELETE ON inspections
WHEN OLD.archived = 1
BEGIN
    SELECT RAISE(ABORT, 'ARCHIVED_IMMUTABLE');
END;

-- 全量变更历史：每次写入一行快照，房间用途变化等均可追溯
CREATE TABLE IF NOT EXISTS revisions (
    entity_type TEXT NOT NULL,                 -- property/unit/party/inspection
    entity_id   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    change_type TEXT NOT NULL,                 -- register/update/inspect/rectify/close
    changes     TEXT,                          -- JSON 字段级差异 {field: {from, to}}
    snapshot    TEXT NOT NULL,                 -- JSON 该版本完整快照
    changed_by  TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id, version)
);

-- 幂等键：重复提交相同材料返回首次结果，不产生重复记录
CREATE TABLE IF NOT EXISTS idempotency (
    key           TEXT PRIMARY KEY,
    endpoint      TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    status_code   INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""


class Database:
    """单连接 + 可重入锁：写操作串行化，版本判断与写入在同一事务内完成。"""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None  # 手动管理事务边界
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        """写事务：BEGIN IMMEDIATE 保证检查-写入原子性，异常自动回滚。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def fetchone(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def fetchall(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self):
        with self._lock:
            self._conn.close()
