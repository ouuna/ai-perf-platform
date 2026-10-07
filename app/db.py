"""SQLite 存储层：任务、方案、指标、报告的数据模型与访问。

使用标准库 sqlite3，参数化查询防注入，单一连接管理。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS metric_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    interface TEXT,
    ts REAL NOT NULL,
    users REAL,
    rps REAL,
    p50_ms REAL,
    p95_ms REAL,
    p99_ms REAL,
    error_rate REAL,
    error_count INTEGER,
    cpu_percent REAL,
    mem_percent REAL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    format TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_metric_task_ts ON metric_points(task_id, ts);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    """线程安全的 SQLite 访问封装。"""

    def __init__(self, path: str | None = None) -> None:
        self._path = path or get_settings().db_path
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    # ---- tasks ----

    def create_task(self, requirement: str, status: str = "created") -> int:
        cur = self.execute(
            "INSERT INTO tasks(requirement, status, created_at, updated_at) VALUES (?,?,?,?)",
            (requirement, status, _now(), _now()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def update_task_status(self, task_id: int, status: str) -> None:
        self.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
            (status, _now(), task_id),
        )

    def get_task(self, task_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM tasks WHERE id=?", (task_id,))
        return rows[0] if rows else None

    # ---- test_plans ----

    def save_plan(self, task_id: int, plan: dict[str, Any]) -> int:
        cur = self.execute(
            "INSERT INTO test_plans(task_id, plan_json, created_at) VALUES (?,?,?)",
            (task_id, json.dumps(plan, ensure_ascii=False), _now()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def get_plan(self, task_id: int) -> dict[str, Any] | None:
        rows = self.query(
            "SELECT plan_json FROM test_plans WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        return json.loads(rows[0]["plan_json"]) if rows else None

    # ---- metric_points ----

    def insert_metric_points(self, points: list[dict[str, Any]]) -> None:
        seq = [
            (
                p["task_id"],
                p.get("interface"),
                p["ts"],
                p.get("users"),
                p.get("rps"),
                p.get("p50_ms"),
                p.get("p95_ms"),
                p.get("p99_ms"),
                p.get("error_rate"),
                p.get("error_count"),
                p.get("cpu_percent"),
                p.get("mem_percent"),
            )
            for p in points
        ]
        self.executemany(
            "INSERT INTO metric_points(task_id, interface, ts, users, rps, p50_ms, p95_ms, "
            "p99_ms, error_rate, error_count, cpu_percent, mem_percent) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            seq,
        )

    def get_metric_points(self, task_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM metric_points WHERE task_id=? ORDER BY ts ASC", (task_id,))

    # ---- reports ----

    def save_report(self, task_id: int, fmt: str, content: str) -> int:
        cur = self.execute(
            "INSERT INTO reports(task_id, format, content, created_at) VALUES (?,?,?,?)",
            (task_id, fmt, content, _now()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def get_report(self, task_id: int, fmt: str) -> str | None:
        rows = self.query(
            "SELECT content FROM reports WHERE task_id=? AND format=? ORDER BY id DESC LIMIT 1",
            (task_id, fmt),
        )
        return rows[0]["content"] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_db: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    with _db_lock:
        if _db is None:
            _db = Database()
        return _db
