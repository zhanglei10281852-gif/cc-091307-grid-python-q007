"""SQLite 持久化层：表结构、连接与通用读写。

所有实体采用「当前表 + 版本表」结构：当前表保存指针与检索字段，
版本表保存每一次变更的完整快照，历史不可变。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
  name  TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS operators (
  operator_id TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  role        TEXT NOT NULL,
  zones       TEXT NOT NULL            -- JSON array，网格员负责的片区编码
);

CREATE TABLE IF NOT EXISTS persons (
  person_id       TEXT PRIMARY KEY,
  current_version INTEGER NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person_versions (
  person_id     TEXT NOT NULL,
  version       INTEGER NOT NULL,
  name          TEXT NOT NULL,
  phone         TEXT NOT NULL,
  id_number     TEXT NOT NULL,
  id_expires_on TEXT NOT NULL,         -- ISO date，证件有效期
  changed_by    TEXT NOT NULL,
  changed_at    TEXT NOT NULL,
  change_note   TEXT,
  PRIMARY KEY (person_id, version)
);

CREATE TABLE IF NOT EXISTS houses (
  house_id        TEXT PRIMARY KEY,
  current_version INTEGER NOT NULL,
  address_key     TEXT NOT NULL,       -- 归一化地址，用于冲突检测
  zone            TEXT NOT NULL,
  status          TEXT NOT NULL,       -- active | deregistered
  created_at      TEXT NOT NULL
);
-- 同一地址只允许存在一套在用房屋，防止房东/中介/租客重复登记
CREATE UNIQUE INDEX IF NOT EXISTS idx_houses_active_address
  ON houses(address_key) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS house_versions (
  house_id     TEXT NOT NULL,
  version      INTEGER NOT NULL,
  address      TEXT NOT NULL,
  zone         TEXT NOT NULL,
  responsibles TEXT NOT NULL,          -- JSON [{person_id, role, authorization_*}]
  changed_by   TEXT NOT NULL,
  changed_at   TEXT NOT NULL,
  change_note  TEXT,
  PRIMARY KEY (house_id, version)
);

CREATE TABLE IF NOT EXISTS units (
  unit_id         TEXT PRIMARY KEY,
  house_id        TEXT NOT NULL,
  current_version INTEGER NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unit_versions (
  unit_id    TEXT NOT NULL,
  version    INTEGER NOT NULL,
  room_label TEXT NOT NULL,
  purpose    TEXT NOT NULL,            -- 房间用途，变更留痕
  capacity   INTEGER NOT NULL,
  changed_by TEXT NOT NULL,
  changed_at TEXT NOT NULL,
  change_note TEXT,
  PRIMARY KEY (unit_id, version)
);

CREATE TABLE IF NOT EXISTS check_items (
  item_id         TEXT PRIMARY KEY,
  current_version INTEGER NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_item_versions (
  item_id    TEXT NOT NULL,
  version    INTEGER NOT NULL,
  code       TEXT NOT NULL,
  title      TEXT NOT NULL,
  category   TEXT NOT NULL,
  severity   TEXT NOT NULL,
  changed_by TEXT NOT NULL,
  changed_at TEXT NOT NULL,
  change_note TEXT,
  PRIMARY KEY (item_id, version)
);

CREATE TABLE IF NOT EXISTS inspections (
  inspection_id TEXT PRIMARY KEY,
  house_id      TEXT NOT NULL,
  unit_id       TEXT,
  inspector_id  TEXT NOT NULL,
  version       INTEGER NOT NULL,      -- 归档前允许更正，乐观锁
  status        TEXT NOT NULL,         -- open | archived
  results       TEXT NOT NULL,         -- JSON [{item_id, item_version, result, note}]
  created_at    TEXT NOT NULL,
  archived_at   TEXT
);

CREATE TABLE IF NOT EXISTS rectifications (
  rect_id       TEXT PRIMARY KEY,
  inspection_id TEXT NOT NULL,
  house_id      TEXT NOT NULL,
  item_id       TEXT NOT NULL,
  item_version  INTEGER NOT NULL,      -- 下发时定版的检查项版本
  hazard        TEXT NOT NULL,
  deadline      TEXT NOT NULL,         -- ISO date，限期整改截止日
  status        TEXT NOT NULL,         -- open | closed
  created_by    TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  closed_at     TEXT,                  -- 复核销号时间，证明隐患何时解除
  closed_by     TEXT,
  review_note   TEXT
);

CREATE TABLE IF NOT EXISTS rectification_events (
  rect_id TEXT NOT NULL,
  seq     INTEGER NOT NULL,
  event   TEXT NOT NULL,               -- issued | review_rejected | deadline_extended | closed
  actor   TEXT NOT NULL,
  at      TEXT NOT NULL,
  note    TEXT,
  PRIMARY KEY (rect_id, seq)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  key           TEXT NOT NULL,
  operator_id   TEXT NOT NULL,
  endpoint      TEXT NOT NULL,
  request_hash  TEXT NOT NULL,
  response_body TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (key, operator_id, endpoint)
);
"""

SEED_OPERATORS = [
    ("op-admin", "管理端", "admin", []),
    ("op-grid-1", "网格员甲", "grid_worker", ["Z-01"]),
    ("op-grid-2", "网格员乙", "grid_worker", ["Z-02"]),
]


class Store:
    """单连接 + 写锁的 SQLite 访问层；写操作统一走 BEGIN IMMEDIATE 事务。"""

    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._seed()

    def _seed(self) -> None:
        with self.write() as conn:
            for operator_id, name, role, zones in SEED_OPERATORS:
                conn.execute(
                    "INSERT OR IGNORE INTO operators(operator_id, name, role, zones) VALUES (?,?,?,?)",
                    (operator_id, name, role, json.dumps(zones)),
                )

    @contextmanager
    def write(self):
        """写事务：持锁 + BEGIN IMMEDIATE，保证校验与写入的原子性。"""
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def one(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def next_id(self, conn: sqlite3.Connection, name: str, prefix: str) -> str:
        conn.execute(
            "INSERT INTO counters(name, value) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET value = value + 1",
            (name,),
        )
        value = conn.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()["value"]
        return f"{prefix}-{value:06d}"

    def close(self) -> None:
        with self._lock:
            self._conn.close()
