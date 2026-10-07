"""Collector：解析 Locust 指标 CSV + 采集压测机资源指标，统一为时间序列。

Locust 指标来自 `--csv-full-history` 的 stats_history CSV（按秒采样），
压测机资源来自 psutil（压测进程 CPU/内存），按时间戳对齐后入库。
"""

from __future__ import annotations

import csv
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from app.db import get_db

if TYPE_CHECKING:
    from app.db import Database


@dataclass
class MetricPoint:
    """统一的时间序列点。"""

    task_id: int
    interface: str | None
    ts: float
    users: float | None = None
    rps: float | None = None
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    error_rate: float | None = None
    error_count: int | None = None
    cpu_percent: float | None = None
    mem_percent: float | None = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "interface": self.interface,
            "ts": self.ts,
            "users": self.users,
            "rps": self.rps,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "error_rate": self.error_rate,
            "error_count": self.error_count,
            "cpu_percent": self.cpu_percent,
            "mem_percent": self.mem_percent,
        }


class HostMonitor:
    """压测机自监控：后台线程周期性采样 CPU/内存。

    用于判断"压测机是否成为瓶颈"，避免结论失真。
    """

    def __init__(self, interval: float = 1.0) -> None:
        self._interval = interval
        self._samples: list[tuple[float, float, float]] = []  # (ts, cpu, mem)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        proc = psutil.Process()
        while not self._stop.is_set():
            try:
                cpu = proc.cpu_percent(interval=None)
                mem = proc.memory_info().rss / (1024 * 1024)  # MB
                self._samples.append((time.time(), cpu, mem))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
            self._stop.wait(self._interval)

    @property
    def samples(self) -> list[tuple[float, float, float]]:
        return list(self._samples)


def parse_stats_history(csv_path: Path) -> list[dict]:
    """解析 Locust stats_history CSV，返回按时间排序的原始行。"""
    if not csv_path.exists():
        return []
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows


def collect_from_run(
    task_id: int,
    csv_dir: Path,
    host_samples: list[tuple[float, float, float]] | None = None,
) -> list[MetricPoint]:
    """从一次压测的 CSV 目录收集统一时间序列点。

    - Locust 指标：stats_stats_history.csv（每接口 + Aggregated）
    - 压测机资源：host_samples（ts, cpu, mem）按最近时间戳对齐到接口点
    """
    stats_path = Path(csv_dir) / "stats_stats_history.csv"
    rows = parse_stats_history(stats_path)
    points: list[MetricPoint] = []

    # 构建时间戳 -> 资源样本的映射（取最近一次采样）
    resource_by_ts: dict[float, tuple[float, float]] = {}
    if host_samples:
        for ts, cpu, mem in host_samples:
            resource_by_ts[round(ts)] = (cpu, mem)

    for row in rows:
        name = row.get("Name", "")
        try:
            ts = float(row.get("Timestamp", 0))
        except (TypeError, ValueError):
            continue
        users = _safe_float(row.get("User Count"))
        rps = _safe_float(row.get("Requests/s"))
        p50 = _safe_float(row.get("50%"))
        p95 = _safe_float(row.get("95%"))
        p99 = _safe_float(row.get("99%"))
        req_count = _safe_int(row.get("Request Count"))
        fail_count = _safe_int(row.get("Failure Count"))

        error_rate = (fail_count / req_count) if req_count and req_count > 0 else 0.0

        cpu, mem = resource_by_ts.get(round(ts), (None, None))

        points.append(
            MetricPoint(
                task_id=task_id,
                interface=None if name == "Aggregated" else name,
                ts=ts,
                users=users,
                rps=rps,
                p50_ms=p50,
                p95_ms=p95,
                p99_ms=p99,
                error_rate=error_rate,
                error_count=fail_count,
                cpu_percent=cpu,
                mem_percent=mem,
            )
        )

    return points


def save_points(points: list[MetricPoint], db: Database | None = None) -> None:
    """把时间序列点写入 SQLite。可传入自定义 db 实例，否则用全局单例。"""
    if not points:
        return
    target = db if db is not None else get_db()
    target.insert_metric_points([p.to_dict() for p in points])


def _safe_float(value: str | None) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: str | None) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
